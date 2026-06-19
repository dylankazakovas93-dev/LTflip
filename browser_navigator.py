#!/usr/bin/env python3
"""
Local Playwright browser navigator for the Lithuania Flip Scanner (v0.7).

Runs on YOUR machine (a Mac), NOT in the cloud. It opens a real local Chromium,
navigates each configured search, paginates, extracts listings, de-duplicates by
URL, and writes raw_listings.csv for the rest of the pipeline.

Hard rules (enforced):
- It does NOT bypass CAPTCHA, login, paywalls, or anti-bot blocks. If a block,
  CAPTCHA, login wall, or access-denied page is detected, it STOPS and reports.
- It does NOT auto-buy and does NOT message sellers.
- It does NOT collect seller phone numbers / emails / personal contact details
  (it only reads the public listing card: title, price, location, photo, link).
- Polite delays between page loads.
- No comps = no buy decision (this only feeds research alerts).

Design note: all logic except the actual browser driving is pure and unit-tested
against HTML fixtures. The browser is injected as a ``fetch_html(src, page)``
callable, so the parsing / pagination / dedupe / block-detection can be tested
without Playwright installed. Playwright is imported lazily, only when a real
browser run starts.

Run on your Mac:
    python3 -m pip install -r requirements.txt
    python3 -m playwright install chromium
    python3 browser_navigator.py --config browser_sources.json --output raw_listings.csv
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from collectors.skelbiu_collector import (
    SELECTORS,
    _page_url,
    parse_listings,
    write_rows,
)

DEFAULT_USER_AGENT = (
    "LTFlipScanner/0.7 (+https://github.com/dylankazakovas93-dev/LTflip; "
    "personal local browser; respects blocks)"
)
SKELBIU_SEARCH_BASE = "https://www.skelbiu.lt/skelbimai/"

# If any of these appear on a page, treat it as a block and STOP (never bypass).
BLOCK_MARKERS = [
    "captcha", "recaptcha", "hcaptcha",
    "are you human", "verify you are human", "checking your browser",
    "attention required", "access denied", "403 forbidden",
    "too many requests", "unusual traffic",
    "please log in to continue", "prisijunkite, kad galėtumėte",
]


def build_search_url(src: Dict[str, Any], page: int = 1) -> str:
    """Build a search URL from an explicit ``url`` or from a ``query`` term."""
    url = src.get("url")
    if not url:
        url = SKELBIU_SEARCH_BASE + "?keywords=" + quote_plus(src.get("query", ""))
    return _page_url(url, page)


def detect_block(html: str) -> Optional[str]:
    """Return the matched block marker if the page looks blocked, else None."""
    low = (html or "").lower()
    for marker in BLOCK_MARKERS:
        if marker in low:
            return marker
    return None


def run_source(
    src: Dict[str, Any],
    fetch_html: Callable[[Dict[str, Any], int], Optional[str]],
    *,
    max_pages: int,
    seen: Optional[set] = None,
    max_total: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Navigate one search across pages. Returns (new_rows, block_reason).

    ``fetch_html(src, page)`` returns the page HTML (the browser does this for
    real; tests pass fixture HTML). Pagination stops when a page yields no new
    listings, the page limit is hit, the global budget is hit, or a block is
    detected (block_reason set, non-None).
    """
    seen = seen if seen is not None else set()
    rows: List[Dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        html = fetch_html(src, page)
        if not html:
            break

        reason = detect_block(html)
        if reason:
            return rows, reason

        parsed = parse_listings(
            html,
            source=src.get("source", "Skelbiu"),
            search_name=src.get("search_name", ""),
            base_url=build_search_url(src, page),
            category_hint=src.get("category", ""),
        )

        added = 0
        for r in parsed:
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append(r)
            added += 1
            if max_total and len(seen) >= max_total:
                return rows, None

        if added == 0:
            break  # no new listings on this page -> end of results

    return rows, None


# --- Real browser fetch (lazy Playwright) ----------------------------------

def make_playwright_fetch(*, headless: bool, user_agent: str, delay_seconds: float):
    """Start a local Chromium and return (fetch_html, close) callables.

    Playwright is imported here, lazily, so this module can be imported (and
    tested) without Playwright installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - only on machines w/o Playwright
        raise SystemExit(
            "Playwright is not installed. Run:\n"
            "    python3 -m pip install -r requirements.txt\n"
            "    python3 -m playwright install chromium"
        ) from exc

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(user_agent=user_agent, locale="lt-LT")
    page = context.new_page()
    state = {"first": True}
    item_selector = "." + SELECTORS["item"]

    def fetch_html(src: Dict[str, Any], page_no: int) -> Optional[str]:
        if not state["first"]:
            time.sleep(delay_seconds)   # polite delay between page loads
        state["first"] = False
        url = build_search_url(src, page_no)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector(item_selector, timeout=8000)
        except Exception:
            pass  # no cards (end of results) or a block; the caller checks both
        return page.content()

    def close() -> None:
        context.close()
        browser.close()
        pw.stop()

    return fetch_html, close


def collect(config: Dict[str, Any], output_path: str,
            fetch_factory: Callable = make_playwright_fetch) -> Dict[str, Any]:
    """Drive the browser over all enabled sources and write raw_listings.csv.

    ``fetch_factory`` is injectable so tests can avoid launching a browser.
    """
    sources = [s for s in config.get("sources", []) if s.get("enabled", True)]
    allowed = config.get("allowed_sources")
    if allowed:
        sources = [s for s in sources if s.get("source") in allowed]

    global_max_pages = int(config.get("max_pages_per_source", 1))
    max_total = int(config.get("max_total_listings", 0)) or None
    delay = float(config.get("delay_between_pages_seconds", 3.0))
    stop_on_block = bool(config.get("stop_on_block", True))
    headless = bool(config.get("headless", False))
    user_agent = config.get("user_agent", DEFAULT_USER_AGENT)

    fetch_html, close = fetch_factory(
        headless=headless, user_agent=user_agent, delay_seconds=delay,
    )
    seen: set = set()
    all_rows: List[Dict[str, Any]] = []
    blocked: Optional[Tuple[str, str]] = None
    try:
        for src in sources:
            max_pages = int(src.get("max_pages", global_max_pages))
            rows, reason = run_source(
                src, fetch_html, max_pages=max_pages, seen=seen, max_total=max_total,
            )
            all_rows.extend(rows)
            print(f"  {src.get('search_name', src.get('source'))}: +{len(rows)} new "
                  f"(total {len(all_rows)})")
            if reason:
                blocked = (src.get("search_name", ""), reason)
                print(f"  !! BLOCK detected ({reason}) on '{src.get('search_name')}'. "
                      f"Stopping — not bypassing.")
                if stop_on_block:
                    break
            if max_total and len(seen) >= max_total:
                print(f"  Reached max_total_listings={max_total}; stopping.")
                break
    finally:
        close()

    write_rows(output_path, all_rows)
    print(f"Wrote {output_path}: {len(all_rows)} listings "
          f"({'BLOCKED: ' + blocked[1] if blocked else 'no block detected'}).")
    return {"rows": all_rows, "count": len(all_rows), "blocked": blocked}


def main() -> None:
    ap = argparse.ArgumentParser(description="Local Playwright navigator for public Skelbiu searches.")
    ap.add_argument("--config", default="browser_sources.json", help="Browser sources config JSON")
    ap.add_argument("--output", default="raw_listings.csv", help="Output CSV path")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    collect(config, args.output)


if __name__ == "__main__":
    main()
