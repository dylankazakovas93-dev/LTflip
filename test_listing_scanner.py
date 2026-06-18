"""Test suite for the Lithuania Flip Scanner.

Run with:  pytest -q
"""

import pytest

from listing_scanner import (
    DEFAULT_CONFIG,
    load_config,
    parse_float,
    scan_row,
    sort_results,
)


def cfg():
    """A fresh copy of the default config for each test."""
    return load_config(None)


def base_row(**overrides):
    """A clean, clearly-passing lens listing. Override fields per test."""
    row = {
        "url": "https://example.com/x",
        "category": "lens",
        "title": "Canon EF 50mm f/1.8 objektyvas",
        "description": "Geros būklės, veikia puikiai, galima išbandyti vietoje.",
        "location": "Vilnius",
        "asking_price_eur": "120",
        "expected_resale_eur": "",
        "comp_low_eur": "360",
        "comp_median_eur": "390",
        "liquidity_sold_count": "8",
        "platform_fee_pct": "0.13",
        "risk_reserve_pct": "0.10",
        "buy_transport_cost_eur": "5",
        "resale_shipping_cost_eur": "18",
        "packaging_cost_eur": "4",
        "misc_cost_eur": "0",
        "can_inspect_in_person": "yes",
        "photo_quality": "good",
    }
    row.update(overrides)
    return row


# --- Reject: broken / repair Lithuanian keywords ---------------------------

@pytest.mark.parametrize("word", ["neveikia", "remontui", "defektas", "dalimis"])
def test_rejects_broken_lt_keywords(word):
    row = base_row(description=f"Daiktas {word}, kaip yra.")
    result = scan_row(row, cfg())
    assert result.decision == "PASS"
    assert any("Reject keywords" in r for r in result.reasons)


# --- Reject: cannot inspect in person --------------------------------------

def test_rejects_when_cannot_inspect():
    row = base_row(can_inspect_in_person="no")
    result = scan_row(row, cfg())
    assert result.decision == "PASS"
    assert any("Cannot inspect in person" in r for r in result.reasons)


# --- Reject: net profit below €100 -----------------------------------------

def test_rejects_low_net_profit():
    # Small-ticket item: good ROI/spread but only ~€24 net profit.
    row = base_row(
        asking_price_eur="30",
        comp_low_eur="70",
        comp_median_eur="",
        buy_transport_cost_eur="0",
        resale_shipping_cost_eur="0",
        packaging_cost_eur="0",
    )
    result = scan_row(row, cfg())
    assert result.net_profit_eur < 100
    assert result.decision == "PASS"
    assert any("Net profit too low" in r for r in result.reasons)


# --- Reject: net ROI below 60% ---------------------------------------------

def test_rejects_low_net_roi():
    # Big-ticket item that clears €100 net but only ~45% ROI.
    row = base_row(
        asking_price_eur="200",
        comp_low_eur="400",
        comp_median_eur="",
        buy_transport_cost_eur="0",
        resale_shipping_cost_eur="0",
        packaging_cost_eur="0",
    )
    result = scan_row(row, cfg())
    assert result.net_profit_eur >= 100
    assert result.net_roi_pct < 60
    assert result.decision == "PASS"
    assert any("Net ROI too low" in r for r in result.reasons)


# --- Reject: gross spread below 100% ---------------------------------------

def test_rejects_low_gross_spread():
    # Zero fees/reserve isolate the spread rule: 80% spread must fail.
    row = base_row(
        asking_price_eur="150",
        comp_low_eur="270",
        comp_median_eur="",
        platform_fee_pct="0",
        risk_reserve_pct="0",
        buy_transport_cost_eur="0",
        resale_shipping_cost_eur="0",
        packaging_cost_eur="0",
    )
    result = scan_row(row, cfg())
    assert result.gross_spread_pct < 100
    assert result.decision == "PASS"
    assert any("Gross spread too low" in r for r in result.reasons)


# --- Pass: clean, high-spread lens listing ---------------------------------

def test_passes_clean_high_spread_lens():
    result = scan_row(base_row(), cfg())
    assert result.decision in ("INSPECT", "PRIORITY_INSPECT")
    assert result.net_profit_eur >= 100
    assert result.net_roi_pct >= 60
    assert result.gross_spread_pct >= 100


# --- Parsing: comma decimals like "130,50" ---------------------------------

def test_parse_comma_decimal():
    assert parse_float("130,50") == pytest.approx(130.50)
    assert parse_float("€ 1 999,99".replace(" ", "")) == pytest.approx(1999.99)
    row = base_row(asking_price_eur="120,50")
    result = scan_row(row, cfg())
    assert result.asking_price_eur == pytest.approx(120.50)


# --- Parsing: platform fee as 13 or 0.13 are equivalent --------------------

def test_platform_fee_13_equals_013():
    as_percent = scan_row(base_row(platform_fee_pct="13"), cfg())
    as_decimal = scan_row(base_row(platform_fee_pct="0.13"), cfg())
    assert as_percent.platform_fee_eur == pytest.approx(as_decimal.platform_fee_eur)
    assert as_percent.net_profit_eur == pytest.approx(as_decimal.net_profit_eur)


# --- Sorting: strongest opportunities first --------------------------------

def test_sorts_strongest_first():
    config = cfg()
    strong = scan_row(base_row(comp_low_eur="500", comp_median_eur="540"), config)
    weak = scan_row(base_row(), config)
    rejected = scan_row(base_row(can_inspect_in_person="no"), config)

    ordered = sort_results([rejected, weak, strong])
    decisions = [r.decision for r in ordered]

    # Rejected (PASS) must come last.
    assert decisions[-1] == "PASS"
    # Among non-rejected, higher net profit comes first.
    passing = [r for r in ordered if r.decision != "PASS"]
    profits = [r.net_profit_eur for r in passing]
    assert profits == sorted(profits, reverse=True)
