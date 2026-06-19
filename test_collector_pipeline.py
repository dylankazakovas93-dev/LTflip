"""Tests for the v0.4 collector -> enrichment -> notifier pipeline.

Run with:  pytest -q
"""

from pathlib import Path

import pytest

import seen_state
from collectors.skelbiu_collector import (
    analyze_cards,
    dedupe_by_url,
    parse_listings,
    parse_price_eur,
)
from enrich_candidates import (
    REVIEW_FIELDS,
    TEMPLATE_FIELDS,
    build_sold_search,
    detect_category,
    enrich_rows,
    guess_model,
    photo_quality_from_images,
)
from listing_scanner import load_config, scan_row
from notifier import build_alerts, build_telegram_payload, telegram_api_url

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
    candidates, skipped, _ = enrich_rows(rows, config)

    titles = [c["title"] for c in candidates]
    # The Sigma listing says "Neveikia ... remontui ... dalimis" -> rejected early.
    assert not any("Sigma" in t for t in titles)
    assert any("neveikia" in hits or "remontui" in hits or "dalimis" in hits
               for _, hits in skipped)


def test_enrichment_sets_inspect_flag_by_location():
    config = load_config(None)
    rows = parse_fixture()
    candidates, _, _ = enrich_rows(rows, config)
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
    candidates, _, _ = enrich_rows(rows, config)
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


# === v0.5: self-check, seen-state, telegram, model_guess, comp review ======

# --- Self-check: detect zero cards / coverage ------------------------------

def test_self_check_detects_zero_cards():
    report = analyze_cards("<html><body><p>no listings here</p></body></html>")
    assert report["cards"] == 0
    assert any("no listing cards" in w for w in report["warnings"])


def test_self_check_reports_full_coverage_on_fixture():
    html = FIXTURE.read_text(encoding="utf-8")
    report = analyze_cards(html)
    assert report["cards"] == 5
    assert report["with_title"] == 5
    assert report["with_price"] == 5
    assert report["with_url"] == 5
    assert report["warnings"] == []


# --- Seen-state: new detection + suppress repeats --------------------------

def test_seen_state_detects_new_listings():
    state = {"listings": {}, "last_run": {}}
    new, seen, removed = seen_state.diff_and_update(state, ["a", "b"], now="2026-06-18T00:00:00")
    assert new == ["a", "b"]
    assert seen == [] and removed == []
    assert set(state["listings"]) == {"a", "b"}
    assert seen_state.new_urls(state) == {"a", "b"}


def test_seen_state_suppresses_repeat_then_flags_removed():
    state = {"listings": {}, "last_run": {}}
    seen_state.diff_and_update(state, ["a", "b"], now="2026-06-18T00:00:00")

    # Second run: 'a' still listed, 'b' gone, 'c' is brand new.
    new, seen, removed = seen_state.diff_and_update(state, ["a", "c"], now="2026-06-18T01:00:00")
    assert new == ["c"]
    assert seen == ["a"]
    assert removed == ["b"]
    assert seen_state.new_urls(state) == {"c"}


def test_notifier_suppresses_already_seen_listings():
    rows = [
        {"decision": "INSPECT", "title": "FreshDeal", "net_profit_eur": "130",
         "net_roi_pct": "71", "url": "u-new"},
        {"decision": "INSPECT", "title": "OldDeal", "net_profit_eur": "150",
         "net_roi_pct": "80", "url": "u-old"},
    ]
    # Only u-new is "new" since the last collect run.
    messages = build_alerts(rows, new_urls={"u-new"})
    joined = "\n".join(messages)
    assert len(messages) == 1
    assert "FreshDeal" in joined and "OldDeal" not in joined


# --- Telegram: formatting only, no network ---------------------------------

def test_telegram_message_formatting_without_sending():
    payload = build_telegram_payload("hello world", "12345")
    assert payload["chat_id"] == "12345"
    assert payload["text"] == "hello world"

    url = telegram_api_url("BOT:TOKEN")
    assert url == "https://api.telegram.org/botBOT:TOKEN/sendMessage"


# --- model_guess: conservative, does not overclaim -------------------------

def test_model_guess_known_models():
    assert guess_model("Canon EF 85mm f/1.8 USM objektyvas", "lens") == "Canon EF 85mm f/1.8 USM"
    assert guess_model("Sigma 17-50 f2.8 Canon objektyvas", "lens") == "Sigma 17-50mm f/2.8 Canon"
    assert guess_model("Boss DD-7 Digital Delay pedalas", "music_gear") == "Boss DD-7"


def test_model_guess_returns_unknown_when_unsure():
    assert guess_model("nieko bendro daiktas", "general") == "Unknown"
    assert guess_model("LEGO Star Wars UCS sealed dėžutė", "lego_collectible") == "Unknown"
    assert guess_model("", "lens") == "Unknown"


# --- comp_review_queue generation ------------------------------------------

def test_comp_review_queue_is_generated():
    config = load_config(None)
    rows = parse_fixture()
    _, _, review = enrich_rows(rows, config)

    by_title = {r["title"]: r for r in review}
    # Vilnius, target categories, real prices -> Canon + Boss are queued.
    assert any("Canon" in t for t in by_title)
    assert any("Boss" in t for t in by_title)
    # Klaipėda LEGO is not locally inspectable -> excluded.
    assert not any("LEGO" in t for t in by_title)
    # Broken Sigma was rejected upstream -> excluded.
    assert not any("Sigma" in t for t in by_title)

    canon = next(r for t, r in by_title.items() if "Canon" in t)
    assert list(canon.keys()) == REVIEW_FIELDS
    assert canon["source"] == "Skelbiu"
    assert canon["model_guess"] == "Canon EF 85mm f/1.8 USM"
    assert canon["comp_low_eur"] == "" and canon["comp_median_eur"] == ""
    assert "LH_Sold=1" in canon["suggested_ebay_sold_search"]
    assert "ebay.com" in canon["suggested_ebay_sold_search"]


def test_build_sold_search_uses_sold_filter():
    url = build_sold_search("Unknown", "Sony FE 50mm objektyvas")
    assert "LH_Sold=1" in url and "LH_Complete=1" in url
    assert "objektyvas" not in url   # stopword dropped from fallback query


# === v0.6: two-stage research + action alerts ==============================

from notifier import (  # noqa: E402
    build_research_alerts,
    score_research_item,
    select_research_items,
)


def _review_row(**kw):
    """A strong, clean comp-review row (Vilnius lens, known model, good photos)."""
    base = {
        "source": "Skelbiu",
        "url": "https://skelbiu.lt/123",
        "title": "Canon EF 85mm f/1.8 USM objektyvas",
        "location": "Vilnius",
        "asking_price_eur": "120",
        "category": "lens",
        "model_guess": "Canon EF 85mm f/1.8 USM",
        "photo_quality": "good",
        "suggested_ebay_sold_search": "https://www.ebay.com/sch/i.html?_nkw=Canon+EF+85mm&LH_Sold=1&LH_Complete=1",
        "comp_low_eur": "",
        "comp_median_eur": "",
        "liquidity_sold_count": "",
        "description": "Geros būklės, veikia puikiai, galima išbandyti.",
    }
    base.update(kw)
    return base


def test_research_alert_contains_required_fields():
    config = load_config(None)
    messages = build_research_alerts([_review_row()], config)
    assert len(messages) == 1
    msg = messages[0]
    for needle in ["RESEARCH", "Canon EF 85mm", "Skelbiu", "https://skelbiu.lt/123",
                   "Vilnius", "120", "lens", "model_guess: Canon EF 85mm f/1.8 USM",
                   "LH_Sold=1", "why queued", "NO BUY DECISION"]:
        assert needle in msg, needle


def test_research_scoring_ranks_strong_above_vague():
    config = load_config(None)
    strong = _review_row()
    vague = _review_row(url="u-vague", title="Daiktas", location="Klaipėda",
                        model_guess="Unknown", photo_quality="unknown", description="")
    strong_score, _ = score_research_item(strong, config)
    vague_score, _ = score_research_item(vague, config)
    assert strong_score > vague_score

    # When both qualify, the stronger one is listed first.
    medium = _review_row(url="u-med", location="Kaunas", model_guess="Unknown",
                         photo_quality="medium", description="")
    selected = select_research_items([medium, strong], config)
    assert [r["url"] for r in selected][0] == strong["url"]


def test_research_max_alerts_per_run_is_respected():
    config = load_config(None)
    config["max_research_alerts_per_run"] = 2
    rows = [_review_row(url=f"u{i}") for i in range(3)]
    selected = select_research_items(rows, config)
    assert len(selected) == 2


def test_research_seen_state_suppresses_repeats():
    config = load_config(None)
    rows = [_review_row(url="u-old"), _review_row(url="u-new")]
    # u-old has already been research-alerted previously.
    messages = build_research_alerts(rows, config, already_alerted={"u-old"})
    joined = "\n".join(messages)
    assert "u-new" in joined
    assert "u-old" not in joined


def test_research_only_alerts_target_categories_and_price_range():
    config = load_config(None)
    out_of_range = _review_row(url="u-cheap", asking_price_eur="2")    # below min_asking_price_eur
    wrong_cat = _review_row(url="u-phone", category="general", model_guess="Unknown")
    selected = select_research_items([out_of_range, wrong_cat], config)
    urls = {r["url"] for r in selected}
    assert "u-cheap" not in urls   # filtered by price range
    assert "u-phone" not in urls   # not a target research category


def test_seen_state_alerted_roundtrip():
    state = {"listings": {}, "last_run": {}, "alerted": {}}
    assert seen_state.alerted_urls(state, "research") == set()
    seen_state.mark_alerted(state, "research", ["a", "b"])
    seen_state.mark_alerted(state, "research", ["b", "c"])
    assert seen_state.alerted_urls(state, "research") == {"a", "b", "c"}
    assert seen_state.alerted_urls(state, "action") == set()


def test_both_modes_produce_independent_alerts():
    config = load_config(None)
    # Research stage from a comp-review row.
    research = build_research_alerts([_review_row()], config)
    # Action stage from a scanner result row.
    action = build_alerts([{
        "decision": "INSPECT", "title": "InspectCanon", "net_profit_eur": "130",
        "net_roi_pct": "71", "location": "Vilnius", "asking_price_eur": "120",
        "expected_resale_eur": "360", "url": "u3",
    }])
    assert research and action
    assert "RESEARCH" in research[0] and "NO BUY DECISION" in research[0]
    assert "INSPECT" in action[0] and "RESEARCH" not in action[0]
