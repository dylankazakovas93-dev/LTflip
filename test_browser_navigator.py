"""Tests for the v0.7 local browser navigator + run_local_scan.

These never launch a real browser: the browser is injected as a fetch callable
that returns fixture HTML, so Playwright is not required to run the suite.

Run with:  pytest -q
"""

import csv
from pathlib import Path

import browser_navigator as nav
import run_local_scan
from collectors.skelbiu_collector import RAW_FIELDS, write_rows
from enrich_candidates import enrich_rows
from listing_scanner import load_config

FIXTURES = Path(__file__).parent / "tests" / "fixtures"
PAGE1 = (FIXTURES / "skelbiu_search_sample.html").read_text(encoding="utf-8")
PAGE2 = (FIXTURES / "skelbiu_search_page2.html").read_text(encoding="utf-8")
BLOCKED = (FIXTURES / "skelbiu_blocked.html").read_text(encoding="utf-8")

LENS_SRC = {"source": "Skelbiu", "search_name": "objektyvas", "query": "objektyvas", "category": "lens"}


def _fake_fetch(page_map):
    """Return a fetch_html(src, page) that serves fixture HTML keyed by page no."""
    def fetch(src, page):
        return page_map.get(page)
    return fetch


# --- URL building -----------------------------------------------------------

def test_build_search_url_from_query_and_explicit_url():
    assert nav.build_search_url({"query": "Canon EF"}, 1) == \
        "https://www.skelbiu.lt/skelbimai/?keywords=Canon+EF"
    assert nav.build_search_url({"query": "Canon EF"}, 2) == \
        "https://www.skelbiu.lt/skelbimai/?keywords=Canon+EF&page=2"
    # Explicit URL wins over query.
    assert nav.build_search_url({"url": "https://x/y"}, 1) == "https://x/y"
    assert nav.build_search_url({"url": "https://x/y?a=1"}, 3) == "https://x/y?a=1&page=3"


# --- Extraction from fixture ------------------------------------------------

def test_extraction_from_fixture():
    rows, reason = nav.run_source(LENS_SRC, _fake_fetch({1: PAGE1}), max_pages=1)
    assert reason is None
    # 5 cards on the page, one a duplicate -> 4 unique.
    assert len(rows) == 4
    first = rows[0]
    assert "Canon EF 85mm" in first["title"]
    assert first["asking_price_eur"] == 120.0
    assert first["location"] == "Vilnius"
    assert first["url"].endswith("/skelbimai/123-canon-ef-85mm.html")
    assert first["image_urls"]          # at least one image extracted
    assert first["search_name"] == "objektyvas"


# --- Pagination across fixture pages + cross-page dedupe --------------------

def test_pagination_and_dedupe_across_pages():
    rows, reason = nav.run_source(LENS_SRC, _fake_fetch({1: PAGE1, 2: PAGE2}), max_pages=2)
    assert reason is None
    urls = [r["url"] for r in rows]
    # No URL appears twice even though Canon (123) is on both pages.
    assert len(urls) == len(set(urls))
    # Page-2-only listings are present.
    assert any("200-tamron-70-300" in u for u in urls)
    assert any("201-fujifilm-xf-35" in u for u in urls)


def test_pagination_respects_max_pages():
    # max_pages=1 must not fetch page 2 even if it exists.
    rows, _ = nav.run_source(LENS_SRC, _fake_fetch({1: PAGE1, 2: PAGE2}), max_pages=1)
    assert not any("200-tamron" in r["url"] for r in rows)


def test_pagination_stops_when_no_new_results():
    empty = "<html><body><div class='results-list'></div></body></html>"
    rows, reason = nav.run_source(LENS_SRC, _fake_fetch({1: PAGE1, 2: empty}), max_pages=5)
    assert reason is None
    assert len(rows) == 4   # only page 1's unique listings


def test_max_total_listings_caps_collection():
    rows, _ = nav.run_source(LENS_SRC, _fake_fetch({1: PAGE1, 2: PAGE2}),
                             max_pages=2, max_total=2)
    assert len(rows) == 2


# --- Block / CAPTCHA detection ---------------------------------------------

def test_detect_block_on_captcha_page():
    assert nav.detect_block(BLOCKED) is not None
    assert nav.detect_block(PAGE1) is None


def test_run_source_stops_on_block():
    rows, reason = nav.run_source(LENS_SRC, _fake_fetch({1: BLOCKED, 2: PAGE1}), max_pages=2)
    assert reason is not None          # block reported
    assert rows == []                  # nothing scraped from a blocked page


def test_collect_stops_on_block_and_reports():
    config = {
        "sources": [LENS_SRC],
        "allowed_sources": ["Skelbiu"],
        "max_pages_per_source": 2,
        "stop_on_block": True,
    }
    factory = lambda **kw: (_fake_fetch({1: BLOCKED}), lambda: None)
    result = nav.collect(config, "/tmp/_ltflip_blocked_out.csv", fetch_factory=factory)
    assert result["blocked"] is not None
    assert result["count"] == 0


# --- Raw CSV compatibility with enrichment ---------------------------------

def test_raw_csv_is_enrichment_compatible(tmp_path):
    rows, _ = nav.run_source(LENS_SRC, _fake_fetch({1: PAGE1, 2: PAGE2}), max_pages=2)
    raw_path = tmp_path / "raw_listings.csv"
    write_rows(str(raw_path), rows)

    # Header matches what the collector/enricher expects.
    with open(raw_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == RAW_FIELDS
        read_rows = list(reader)

    candidates, skipped, review = enrich_rows(read_rows, load_config(None))
    assert candidates                              # produced scanner-ready rows
    assert any(c["category"] == "lens" for c in candidates)


# --- run_local_scan dry-run path (no browser) ------------------------------

def test_run_local_scan_dry_run(tmp_path):
    # Build a raw_listings.csv from fixtures, then run the dry-run workflow.
    rows, _ = nav.run_source(LENS_SRC, _fake_fetch({1: PAGE1, 2: PAGE2}), max_pages=2)
    raw_path = tmp_path / "raw_listings.csv"
    write_rows(str(raw_path), rows)

    def _boom(**kwargs):  # must NOT be called in dry-run
        raise AssertionError("browser must not launch during --dry-run")

    summary = run_local_scan.run_local_scan(
        raw_path=str(raw_path),
        candidate_path=str(tmp_path / "candidate.csv"),
        review_path=str(tmp_path / "review.csv"),
        state_path=str(tmp_path / ".seen.json"),
        dry_run=True,
        fetch_factory=_boom,
    )
    assert summary["dry_run"] is True
    assert summary["blocked"] is None
    assert summary["raw"] == len(rows)
    assert summary["candidates"] >= 1
    assert summary["review_queue"] >= 1
    assert summary["research_alerts"] >= 1
    assert (tmp_path / "review.csv").exists()
