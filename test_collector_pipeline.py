"""Tests for the v0.4 collector -> enrichment -> notifier pipeline.

Run with:  pytest -q
"""

from pathlib import Path

import pytest

from collectors.skelbiu_collector import (
    dedupe_by_url,
    parse_listings,
    parse_price_eur,
)
from enrich_candidates import (
    TEMPLATE_FIELDS,
    detect_category,
    enrich_rows,
    photo_quality_from_images,
)
from listing_scanner import load_config, scan_row
from notifier import build_alerts

FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "skelbiu_search_sample.html"
BASE_URL = "https://www.skelbiu.lt/skelbimai/"


def parse_fixture(search_name=""):
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_listings(html, source="Skelbiu", search_name=search_name, base_url=BASE_URL)


# --- Collector: parse a Skelbiu-like HTML fixture --------------------------

def test_fixture_parses_into_listings():
    rows = parse_fixture(search_name="objektyvas")
    # 5 cards in the fixture (one is a deliberate duplicate).
    assert len(rows) == 5

    first = rows[0]
    assert first["source"] == "Skelbiu"
    assert first["search_name"] == "objektyvas"
    assert first["url"] == "https://www.skelbiu.lt/skelbimai/123-canon-ef-85mm.html"
    assert "Canon EF 85mm" in first["title"]
    assert "veikia puikiai" in first["description"]
    assert first["location"] == "Vilnius"
    assert first["asking_price_eur"] == 120.0      # nested <span>120</span> &euro;
    assert first["posted_at"] == "2026-06-17"
    # Three images, made absolute against the base URL.
    assert first["image_urls"].split()[0] == "https://www.skelbiu.lt/img/123-1.jpg"
    assert len(first["image_urls"].split()) == 3


def test_price_parsing_variants():
    assert parse_price_eur("120 €") == 120.0
    assert parse_price_eur("1 200 €") == 1200.0          # space thousands separator
    assert parse_price_eur("130,50 €") == 130.5          # comma decimal
    assert parse_price_eur("Kaina sutartinė") == ""      # no number


# --- Collector: de-duplicate by URL ----------------------------------------

def test_duplicate_urls_are_removed():
    rows = parse_fixture()
    urls = [r["url"] for r in rows]
    assert len(urls) != len(set(urls))   # the raw parse still contains the duplicate

    deduped = dedupe_by_url(rows)
    assert len(deduped) == len(set(urls))
    assert len({r["url"] for r in deduped}) == len(deduped)


def test_dedupe_respects_already_seen_urls():
    rows = parse_fixture()
    seen = {rows[0]["url"]}                # pretend we collected this one last run
    deduped = dedupe_by_url(rows, seen=seen)
    assert all(r["url"] != rows[0]["url"] for r in deduped)


# --- Enrichment: classification + early reject -----------------------------

def test_detect_category():
    assert detect_category("canon ef 85mm objektyvas") == "lens"
    assert detect_category("boss dd-7 delay pedalas") == "music_gear"
    assert detect_category("lego star wars ucs sealed") == "lego_collectible"
    assert detect_category("nieko bendro daiktas") == "general"
    assert detect_category("no keyword match", hint="lens") == "lens"  # source hint fallback


def test_photo_quality_from_images():
    assert photo_quality_from_images("a.jpg b.jpg c.jpg") == "good"
    assert photo_quality_from_images("a.jpg") == "medium"
    assert photo_quality_from_images("") == "unknown"


def test_enrichment_rejects_broken_keywords():
    config = load_config(None)
    rows = parse_fixture()
    candidates, skipped = enrich_rows(rows, config)

    titles = [c["title"] for c in candidates]
    # The Sigma listing says "Neveikia ... remontui ... dalimis" -> rejected early.
    assert not any("Sigma" in t for t in titles)
    assert any("neveikia" in hits or "remontui" in hits or "dalimis" in hits
               for _, hits in skipped)


def test_enrichment_sets_inspect_flag_by_location():
    config = load_config(None)
    rows = parse_fixture()
    candidates, _ = enrich_rows(rows, config)
    by_title = {c["title"]: c for c in candidates}

    canon = next(c for t, c in by_title.items() if "Canon" in t)
    assert canon["can_inspect_in_person"] == "yes"   # Vilnius is allowed
    assert canon["category"] == "lens"
    assert canon["photo_quality"] == "good"

    lego = next(c for t, c in by_title.items() if "LEGO" in t)
    assert lego["can_inspect_in_person"] == "no"     # Klaipėda not in allowed list
    assert lego["category"] == "lego_collectible"


# --- Compatibility: candidate rows feed straight into the scanner ----------

def test_candidate_rows_are_scanner_compatible():
    config = load_config(None)
    rows = parse_fixture()
    candidates, _ = enrich_rows(rows, config)
    assert candidates, "expected at least one candidate"

    canon = next(c for c in candidates if "Canon" in c["title"])
    # Exact header compatibility with the scanner template.
    assert list(canon.keys()) == TEMPLATE_FIELDS

    # With sold comps filled in, a clean lens should score and pass.
    scored = scan_row(
        dict(canon, comp_low_eur="360", comp_median_eur="390", liquidity_sold_count="8"),
        config,
    )
    assert scored.category == "lens"
    assert scored.asking_price_eur == 120.0
    assert scored.decision in ("INSPECT", "PRIORITY_INSPECT")


# --- Notifier: only INSPECT / PRIORITY_INSPECT -----------------------------

def test_notifier_only_inspect_and_priority():
    rows = [
        {"decision": "PASS", "title": "PassRejected", "net_profit_eur": "10",
         "net_roi_pct": "5", "location": "Vilnius", "asking_price_eur": "30",
         "expected_resale_eur": "70", "url": "u1"},
        {"decision": "WATCHLIST", "title": "WatchOnly", "net_profit_eur": "40",
         "net_roi_pct": "30", "location": "Kaunas", "asking_price_eur": "50",
         "expected_resale_eur": "120", "url": "u2"},
        {"decision": "INSPECT", "title": "InspectCanon", "net_profit_eur": "130",
         "net_roi_pct": "71", "location": "Vilnius", "asking_price_eur": "120",
         "expected_resale_eur": "360", "url": "u3"},
        {"decision": "PRIORITY_INSPECT", "title": "PriorityLens", "net_profit_eur": "260",
         "net_roi_pct": "110", "location": "Vilnius", "asking_price_eur": "150",
         "expected_resale_eur": "520", "url": "u4"},
    ]
    messages = build_alerts(rows)
    joined = "\n".join(messages)

    assert len(messages) == 2
    assert "InspectCanon" in joined and "PriorityLens" in joined
    assert "PassRejected" not in joined and "WatchOnly" not in joined
    # Strongest (PRIORITY_INSPECT) is listed first.
    assert "PriorityLens" in messages[0]
    assert "PRIORITY_INSPECT" in messages[0]
