#!/usr/bin/env python3
"""
Skelbiu.lt collector for the Lithuania Flip Scanner (v0.4).

What it does
------------
- Reads a list of Skelbiu.lt *public* search URLs from sources.json.
- Fetches each search page politely and extracts candidate listings.
- Writes raw rows to raw_listings.csv, de-duplicated by URL across runs.

What it deliberately does NOT do
--------------------------------
- No auto-buying. This only flags listings worth inspecting.
- No CAPTCHA / login / paywall / anti-bot bypassing.
- No scraping of phone numbers, contact details, or other personal data.
- It checks robots.txt, rate-limits itself, and caches pages between runs.

Run
---
    python3 collectors/skelbiu_collector.py --sources sources.json --output raw_listings.csv

Offline / testing
-----------------
A source "url" may be a local file (a path or a file:// URL). In that case the
file is read directly (no network, no robots check). This is how the test suite
and the offline demo work.

Assumed page structure
----------------------
We assume each listing on a Skelbiu search-results page is one container element
carrying the CSS class in ``SELECTORS["item"]``, holding a title link, a price,
a location, an optional date, and one or more images. The exact class names live
in ``SELECTORS`` below so they are trivial to update if the site markup changes.
The parser is tolerant: a card missing a field yields a blank, never a crash.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

# Allow `python3 collectors/skelbiu_collector.py ...` to import repo-root modules.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seen_state  # noqa: E402  (after sys.path tweak)


# An honest, identifiable User-Agent. Update the URL to your own fork.
DEFAULT_USER_AGENT = (
    "LTFlipScanner/0.4 (+https://github.com/dylankazakovas93-dev/LTflip; "
    "resale-validation bot; respects robots.txt)"
)
DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_CACHE_TTL_MINUTES = 360
DEFAULT_CACHE_DIR = ".cache"

# CSS class tokens used to locate fields inside a listing card.
# Update these (only these) if Skelbiu changes its markup.
SELECTORS = {
    "item": "standard-list-item",
    "fields": {
        "title": "standard-list-title",         # an <a href> -> title + listing URL
        "description": "standard-list-description",
        "price": "standard-list-price",
        "location": "standard-list-location",
        "date": "standard-list-date",
    },
}

# Columns written to raw_listings.csv.
RAW_FIELDS = [
    "source", "url", "search_name", "category", "title", "description",
    "location", "asking_price_eur", "posted_at", "image_urls", "collected_at",
]


class _ListingHTMLParser(HTMLParser):
    """Tolerant single-pass extractor for repeated listing cards.

    Cards are delimited by the container class ``selectors["item"]``. Nesting is
    tracked by counting the container's own tag, so inner elements never confuse
    the boundaries.
    """

    def __init__(self, selectors: Dict[str, Any]):
        super().__init__(convert_charrefs=True)
        self._sel = selectors
        self.items: List[Dict[str, Any]] = []
        self._cur: Optional[Dict[str, Any]] = None
        self._container_tag: Optional[str] = None
        self._depth = 0
        self._cap_field: Optional[str] = None
        self._cap_tag: Optional[str] = None
        self._buf: List[str] = []

    @staticmethod
    def _attrs(attrs):
        d = {k: (v or "") for k, v in attrs}
        return set(d.get("class", "").split()), d

    def handle_starttag(self, tag, attrs):
        classes, d = self._attrs(attrs)

        # Not inside a card yet: open one when the container class shows up.
        if self._cur is None:
            if self._sel["item"] in classes:
                self._cur = {
                    "url": "", "title": "", "description": "",
                    "location": "", "price_text": "", "posted_at": "",
                    "image_urls": [],
                }
                self._container_tag = tag
                self._depth = 1
            return

        # Inside a card.
        if tag == self._container_tag:
            self._depth += 1

        if tag == "img" and d.get("src"):
            self._cur["image_urls"].append(d["src"])

        if self._cap_field is None:
            for field, token in self._sel["fields"].items():
                if token in classes:
                    self._cap_field = field
                    self._cap_tag = tag
                    self._buf = []
                    if field == "title" and d.get("href"):
                        self._cur["url"] = d["href"]
                    break

    def handle_startendtag(self, tag, attrs):
        # Route void elements like <img/> through the normal handler.
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._cur is not None and self._cap_field is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._cur is None:
            return

        if self._cap_field is not None and tag == self._cap_tag:
            text = " ".join(" ".join(self._buf).split())
            mapping = {
                "title": "title", "description": "description",
                "location": "location", "price": "price_text", "date": "posted_at",
            }
            self._cur[mapping[self._cap_field]] = text
            self._cap_field = self._cap_tag = None
            self._buf = []

        if tag == self._container_tag:
            self._depth -= 1
            if self._depth <= 0:
                self.items.append(self._cur)
                self._cur = None
                self._container_tag = None


def parse_price_eur(text: str) -> Any:
    """Extract a numeric euro price from text like '120 €' or '1 200,50 €'.

    Returns a float, or '' when no number is present (e.g. 'Kaina sutartinė').
    """
    if not text:
        return ""
    t = text.replace("\xa0", " ")
    t = re.sub(r"(?<=\d)\s(?=\d)", "", t)   # drop spaces used as thousands separators
    t = t.replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group()) if m else ""


def extract_cards(html: str, selectors: Dict[str, Any] = SELECTORS) -> List[Dict[str, Any]]:
    """Return the raw listing cards found on a page (including any missing fields)."""
    parser = _ListingHTMLParser(selectors)
    parser.feed(html)
    parser.close()
    return parser.items


def analyze_cards(html: str, selectors: Dict[str, Any] = SELECTORS) -> Dict[str, Any]:
    """Count how complete the listing cards on a page are (for --self-check)."""
    cards = extract_cards(html, selectors)
    total = len(cards)
    with_title = sum(1 for c in cards if c.get("title", "").strip())
    with_price = sum(1 for c in cards if parse_price_eur(c.get("price_text", "")) != "")
    with_location = sum(1 for c in cards if c.get("location", "").strip())
    with_url = sum(1 for c in cards if c.get("url", "").strip())

    warnings: List[str] = []
    if total == 0:
        warnings.append("no listing cards found - selectors may be stale or page changed")
    else:
        if with_title / total < 0.5:
            warnings.append("more than half of cards are missing a title")
        if with_price / total < 0.5:
            warnings.append("more than half of cards are missing a price")

    return {
        "cards": total,
        "with_title": with_title,
        "with_price": with_price,
        "with_location": with_location,
        "with_url": with_url,
        "warnings": warnings,
    }


def parse_listings(
    html: str,
    *,
    source: str = "Skelbiu",
    search_name: str = "",
    base_url: str = "",
    category_hint: str = "",
    selectors: Dict[str, Any] = SELECTORS,
) -> List[Dict[str, Any]]:
    """Parse a search-results page into a list of raw listing rows."""
    collected_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for it in extract_cards(html, selectors):
        href = (it.get("url") or "").strip()
        if not href:
            continue  # without a URL we can't dedupe or revisit it
        url = urljoin(base_url, href) if base_url else href
        images = [urljoin(base_url, u) if base_url else u for u in it["image_urls"]]
        rows.append({
            "source": source,
            "url": url,
            "search_name": search_name,
            "category": category_hint,
            "title": (it.get("title") or "").strip(),
            "description": (it.get("description") or "").strip(),
            "location": (it.get("location") or "").strip(),
            "asking_price_eur": parse_price_eur(it.get("price_text", "")),
            "posted_at": (it.get("posted_at") or "").strip(),
            "image_urls": " ".join(images),
            "collected_at": collected_at,
        })
    return rows


def dedupe_by_url(rows: List[Dict[str, Any]], seen: Optional[set] = None) -> List[Dict[str, Any]]:
    """Keep the first row for each URL; drop blanks and repeats."""
    seen = set() if seen is None else set(seen)
    out = []
    for r in rows:
        u = r.get("url", "")
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(r)
    return out


# --- Fetching (network) -----------------------------------------------------

def _is_local(url: str) -> bool:
    return url.startswith("file://") or not re.match(r"^https?://", url)


def _local_path(url: str) -> str:
    return url[len("file://"):] if url.startswith("file://") else url


def _robots_allows(url: str, user_agent: str, cache: Dict[str, Any]) -> bool:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    rp = cache.get(base)
    if rp is None:
        rp = RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            cache[base] = False   # can't read robots.txt -> be polite, refuse
            return False
        cache[base] = rp
    if rp is False:
        return False
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return False


def _polite_delay(state: Dict[str, float], delay_seconds: float) -> None:
    wait = delay_seconds - (time.time() - state.get("last_request", 0.0))
    if wait > 0:
        time.sleep(wait)
    state["last_request"] = time.time()


def fetch_html(
    url: str,
    *,
    cache_dir: str,
    cache_ttl_seconds: float,
    delay_seconds: float,
    user_agent: str,
    robots_cache: Dict[str, Any],
    state: Dict[str, float],
) -> Optional[str]:
    """Return page HTML, honouring local files, cache, robots.txt and delays."""
    if _is_local(url):
        p = Path(_local_path(url))
        if not p.exists():
            print(f"  [missing local file] {url}")
            return None
        return p.read_text(encoding="utf-8", errors="replace")

    cache_path = Path(cache_dir) / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".html")
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < cache_ttl_seconds:
        return cache_path.read_text(encoding="utf-8", errors="replace")

    if not _robots_allows(url, user_agent, robots_cache):
        print(f"  [robots.txt] disallowed or unreadable, skipping: {url}")
        return None

    _polite_delay(state, delay_seconds)
    req = Request(url, headers={"User-Agent": user_agent, "Accept-Language": "lt,en;q=0.8"})
    try:
        with urlopen(req, timeout=20) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
    except Exception as exc:  # network error, HTTP error, timeout, etc.
        print(f"  [fetch error] {url}: {exc}")
        return None

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(html, encoding="utf-8")
    return html


def _page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    # Best-effort pagination. Skelbiu's exact paging scheme may differ; verify
    # before relying on this and keep max_pages_per_source small.
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page}"


def fetch_with_status(
    url: str,
    *,
    user_agent: str,
    delay_seconds: float,
    robots_cache: Dict[str, Any],
    state: Dict[str, float],
) -> tuple:
    """Live fetch for diagnostics. Returns (status_str, html_or_None).

    Bypasses the cache so --self-check sees the real page, but still honours
    robots.txt and the polite delay.
    """
    if _is_local(url):
        p = Path(_local_path(url))
        if not p.exists():
            return ("missing-file", None)
        return ("local-file", p.read_text(encoding="utf-8", errors="replace"))

    if not _robots_allows(url, user_agent, robots_cache):
        return ("robots-blocked", None)

    _polite_delay(state, delay_seconds)
    req = Request(url, headers={"User-Agent": user_agent, "Accept-Language": "lt,en;q=0.8"})
    try:
        with urlopen(req, timeout=20) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
        return (str(status), html)
    except HTTPError as exc:
        return (f"HTTP {exc.code}", None)
    except Exception as exc:  # URLError, timeout, etc.
        return (f"error: {exc}", None)


def self_check(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch each source and report card counts / field coverage / warnings."""
    user_agent = config.get("user_agent", DEFAULT_USER_AGENT)
    delay = float(config.get("request_delay_seconds", DEFAULT_DELAY_SECONDS))
    robots_cache: Dict[str, Any] = {}
    state: Dict[str, float] = {}

    reports = []
    for src in config.get("sources", []):
        name = src.get("name", "")
        url = src.get("url", "")
        if not url:
            continue
        status, html = fetch_with_status(
            url, user_agent=user_agent, delay_seconds=delay,
            robots_cache=robots_cache, state=state,
        )
        report = {"name": name, "url": url, "status": status,
                  "cards": 0, "with_title": 0, "with_price": 0,
                  "with_location": 0, "with_url": 0, "warnings": []}
        if html is None:
            report["warnings"].append(f"fetch failed ({status})")
        else:
            report.update(analyze_cards(html))
        reports.append(report)
    return reports


def print_self_check(reports: List[Dict[str, Any]]) -> bool:
    """Print a self-check report. Returns True if everything looks healthy."""
    healthy = True
    print("Self-check:\n")
    for r in reports:
        print(f"- {r['name'] or r['url']}")
        print(f"    status={r['status']}  cards={r['cards']}  "
              f"title={r['with_title']}  price={r['with_price']}  "
              f"location={r['with_location']}  url={r['with_url']}")
        for w in r["warnings"]:
            healthy = False
            print(f"    !! WARNING: {w}")
    print()
    print("OK - all sources returned usable cards." if healthy
          else "PROBLEMS found - see warnings above.")
    return healthy


# --- Orchestration ----------------------------------------------------------

def read_existing(output_path: str):
    rows, seen = [], set()
    p = Path(output_path)
    if p.exists():
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
                if row.get("url"):
                    seen.add(row["url"])
    return rows, seen


def write_rows(output_path: str, rows: List[Dict[str, Any]]) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in RAW_FIELDS})


def collect(
    config: Dict[str, Any],
    output_path: str,
    *,
    state_path: Optional[str] = None,
    update_state: bool = True,
) -> List[Dict[str, Any]]:
    sources = config.get("sources", [])
    user_agent = config.get("user_agent", DEFAULT_USER_AGENT)
    delay = float(config.get("request_delay_seconds", DEFAULT_DELAY_SECONDS))
    ttl = float(config.get("cache_ttl_minutes", DEFAULT_CACHE_TTL_MINUTES)) * 60
    cache_dir = config.get("cache_dir", DEFAULT_CACHE_DIR)
    max_pages = int(config.get("max_pages_per_source", 1))
    if state_path is None:
        state_path = config.get("seen_state_path", seen_state.DEFAULT_STATE_PATH)

    existing_rows, seen = read_existing(output_path)
    robots_cache: Dict[str, Any] = {}
    state: Dict[str, float] = {}
    new_rows: List[Dict[str, Any]] = []
    observed: set = set()   # every live URL seen this run, for the saved-state diff

    for src in sources:
        name = src.get("name", "")
        base_url = src.get("url", "")
        cat_hint = src.get("category", "")
        if not base_url:
            continue
        for page in range(1, max_pages + 1):
            page_url = _page_url(base_url, page)
            html = fetch_html(
                page_url, cache_dir=cache_dir, cache_ttl_seconds=ttl,
                delay_seconds=delay, user_agent=user_agent,
                robots_cache=robots_cache, state=state,
            )
            if not html:
                continue
            parsed = parse_listings(
                html, source="Skelbiu", search_name=name,
                base_url=page_url, category_hint=cat_hint,
            )
            added = 0
            for r in parsed:
                if not r["url"]:
                    continue
                observed.add(r["url"])
                if r["url"] in seen:
                    continue
                seen.add(r["url"])
                new_rows.append(r)
                added += 1
            print(f"  {name or base_url} (p{page}): parsed {len(parsed)}, new {added}")

    write_rows(output_path, existing_rows + new_rows)
    print(f"Wrote {output_path}: {len(existing_rows) + len(new_rows)} total "
          f"({len(new_rows)} new this run)")

    if update_state:
        st = seen_state.load_state(state_path)
        new, seen_again, removed = seen_state.diff_and_update(st, observed)
        seen_state.save_state(st, state_path)
        print(f"State ({state_path}): {len(new)} new, {len(seen_again)} seen again, "
              f"{len(removed)} no longer listed. Only new listings are alert-eligible.")

    return new_rows


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Collect public Skelbiu.lt search results into a raw CSV. "
                    "Decision-support only; not an auto-buyer."
    )
    ap.add_argument("--sources", default="sources.json", help="Sources config JSON")
    ap.add_argument("--output", default="raw_listings.csv", help="Output CSV path")
    ap.add_argument("--self-check", action="store_true",
                    help="Fetch each source and report card/field coverage, then exit")
    args = ap.parse_args()

    config = json.loads(Path(args.sources).read_text(encoding="utf-8"))

    if args.self_check:
        healthy = print_self_check(self_check(config))
        sys.exit(0 if healthy else 1)

    collect(config, args.output)


if __name__ == "__main__":
    main()
