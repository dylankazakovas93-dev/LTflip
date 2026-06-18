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
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser


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
    parser = _ListingHTMLParser(selectors)
    parser.feed(html)
    parser.close()

    collected_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for it in parser.items:
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


def collect(config: Dict[str, Any], output_path: str) -> List[Dict[str, Any]]:
    sources = config.get("sources", [])
    user_agent = config.get("user_agent", DEFAULT_USER_AGENT)
    delay = float(config.get("request_delay_seconds", DEFAULT_DELAY_SECONDS))
    ttl = float(config.get("cache_ttl_minutes", DEFAULT_CACHE_TTL_MINUTES)) * 60
    cache_dir = config.get("cache_dir", DEFAULT_CACHE_DIR)
    max_pages = int(config.get("max_pages_per_source", 1))

    existing_rows, seen = read_existing(output_path)
    robots_cache: Dict[str, Any] = {}
    state: Dict[str, float] = {}
    new_rows: List[Dict[str, Any]] = []

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
                if not r["url"] or r["url"] in seen:
                    continue
                seen.add(r["url"])
                new_rows.append(r)
                added += 1
            print(f"  {name or base_url} (p{page}): parsed {len(parsed)}, new {added}")

    write_rows(output_path, existing_rows + new_rows)
    print(f"Wrote {output_path}: {len(existing_rows) + len(new_rows)} total "
          f"({len(new_rows)} new this run)")
    return new_rows


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Collect public Skelbiu.lt search results into a raw CSV. "
                    "Decision-support only; not an auto-buyer."
    )
    ap.add_argument("--sources", default="sources.json", help="Sources config JSON")
    ap.add_argument("--output", default="raw_listings.csv", help="Output CSV path")
    args = ap.parse_args()

    config = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    collect(config, args.output)


if __name__ == "__main__":
    main()
