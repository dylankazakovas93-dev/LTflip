#!/usr/bin/env python3
"""
Lithuania Flip Scanner v0.1

Purpose:
- Score local Lithuanian pickup listings for resale arbitrage.
- Designed for high-ticket/collectible categories: camera lenses, music gear, Lego/collectibles, etc.
- This is a decision engine, not an auto-buyer. It flags "worth inspecting" opportunities.

Input:
- CSV file with listing fields. Start with input_template.csv.

Core rules:
- Minimum expected NET profit: default €100
- Minimum NET ROI: default 60%
- Minimum gross price spread: default 100% versus asking price
  Example: asking €100, conservative resale comp must be >= €200 before costs.

Definitions:
- all_in_cost = asking_price + buy_transport_cost + repair_risk_reserve + resale_shipping_cost + packaging_cost + misc_cost
- platform_fee = expected_resale_price * platform_fee_pct
- net_profit = expected_resale_price - platform_fee - all_in_cost
- net_roi = net_profit / all_in_cost
- gross_spread_pct = (expected_resale_price - asking_price) / asking_price

Important:
- This tool does NOT prove an item works. It only decides if a listing is worth inspecting.
- Never buy without in-person testing for lenses/music gear/electronics.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Any


# Lower number = stronger opportunity. Used to sort results, strongest first.
DECISION_ORDER = {"PRIORITY_INSPECT": 0, "INSPECT": 1, "WATCHLIST": 2, "PASS": 3}


DEFAULT_CONFIG = {
    "min_net_profit_eur": 100.0,
    "min_net_roi": 0.60,
    "min_gross_spread_pct": 1.00,
    "max_purchase_price_eur": 250.0,
    "allowed_locations": ["vilnius", "kaunas"],
    "platform_fee_pct_default": 0.13,
    "risk_reserve_pct_default": 0.10,
    "min_liquidity_sold_count": 3,
    "prefer_local_pickup_only": True,
    "reject_keywords_lt": [
        "neveikia", "neveikiantis", "neveikianti", "sugedęs", "sugedusi",
        "defektas", "defektuotas", "remontui", "dalimis", "reikia taisyti",
        "neįsijungia", "neisijungia", "skilęs", "skilusi", "sudužęs",
        "suduzęs", "subraižytas", "subraižyta", "grybelis", "pelėsis",
        "pelesis", "dulkės viduje", "dulkes viduje", "nežinau ar veikia",
        "nezinau ar veikia", "be testavimo", "netestuotas", "netestuota",
        "tik dalimis", "atsargines dalys", "for parts", "broken", "not working"
    ],
    "caution_keywords_lt": [
        "be garantijos", "naudotas", "naudota", "senas", "sena", "reikia valyti",
        "yra pabraižymų", "yra pabraizymu", "be dėžutės", "be dezutes",
        "nėra pakrovėjo", "nera pakrovejo", "nežinau", "nezinau"
    ],
    "positive_keywords_lt": [
        "veikia puikiai", "pilnai veikiantis", "pilnai veikianti", "geros būklės",
        "geros bukles", "be defektų", "be defektu", "mažai naudotas",
        "mazai naudotas", "su dėžute", "su dezute", "su dokumentais",
        "pirkta lietuvoje", "galima išbandyti", "galima isbandyti", "testuoti vietoje"
    ],
    "category_rules": {
        "lens": {
            "extra_reject_keywords": ["haze", "fungus", "grybelis", "pelėsis", "pelesis", "aperture stuck"],
            "inspection_required": True
        },
        "music_gear": {
            "extra_reject_keywords": ["crackling", "netestuotas", "neveikia", "kontaktuoja blogai"],
            "inspection_required": True
        },
        "lego_collectible": {
            "extra_reject_keywords": ["nepilnas", "trūksta detalių", "truksta detaliu", "be instrukcijos", "maišas lego", "maisas lego"],
            "inspection_required": True
        },
        "general": {
            "extra_reject_keywords": [],
            "inspection_required": True
        }
    }
}


@dataclass
class ScanResult:
    decision: str
    score: float
    reasons: List[str]
    warnings: List[str]
    title: str
    category: str
    location: str
    asking_price_eur: float
    expected_resale_eur: float
    all_in_cost_eur: float
    platform_fee_eur: float
    net_profit_eur: float
    net_roi_pct: float
    gross_spread_pct: float
    liquidity_sold_count: int
    url: str


def norm_text(s: Any) -> str:
    return str(s or "").strip().lower()


def parse_float(x: Any, default: float = 0.0) -> float:
    if x is None:
        return default
    s = str(x).strip().replace("€", "").replace(",", ".")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        nums = re.findall(r"\d+(?:\.\d+)?", s)
        return float(nums[0]) if nums else default


def parse_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(str(x).strip()))
    except Exception:
        return default


def has_any(text: str, words: List[str]) -> List[str]:
    found = []
    for w in words:
        w2 = w.lower()
        if w2 in text:
            found.append(w)
    return found


def choose_expected_resale(row: Dict[str, Any]) -> float:
    """
    Use conservative comp if available.
    Priority:
    1. comp_low_eur
    2. comp_median_eur * 0.85
    3. expected_resale_eur
    """
    comp_low = parse_float(row.get("comp_low_eur"), 0)
    comp_med = parse_float(row.get("comp_median_eur"), 0)
    expected = parse_float(row.get("expected_resale_eur"), 0)
    if comp_low > 0:
        return comp_low
    if comp_med > 0:
        return comp_med * 0.85
    return expected


def scan_row(row: Dict[str, Any], config: Dict[str, Any]) -> ScanResult:
    title = str(row.get("title", "")).strip()
    desc = str(row.get("description", "")).strip()
    text = norm_text(title + " " + desc)
    category = norm_text(row.get("category") or "general")
    location = norm_text(row.get("location"))
    url = str(row.get("url", "")).strip()

    asking = parse_float(row.get("asking_price_eur"), 0)
    expected_resale = choose_expected_resale(row)
    liquidity = parse_int(row.get("liquidity_sold_count"), 0)

    buy_transport = parse_float(row.get("buy_transport_cost_eur"), 0)
    resale_ship = parse_float(row.get("resale_shipping_cost_eur"), 0)
    packaging = parse_float(row.get("packaging_cost_eur"), 0)
    misc = parse_float(row.get("misc_cost_eur"), 0)

    platform_fee_pct = parse_float(row.get("platform_fee_pct"), config["platform_fee_pct_default"])
    # If user passes "13" instead of "0.13", interpret as percentage.
    if platform_fee_pct > 1:
        platform_fee_pct = platform_fee_pct / 100.0

    risk_reserve_pct = parse_float(row.get("risk_reserve_pct"), config["risk_reserve_pct_default"])
    if risk_reserve_pct > 1:
        risk_reserve_pct = risk_reserve_pct / 100.0

    repair_risk_reserve = expected_resale * risk_reserve_pct
    platform_fee = expected_resale * platform_fee_pct
    all_in_cost = asking + buy_transport + resale_ship + packaging + misc + repair_risk_reserve
    net_profit = expected_resale - platform_fee - all_in_cost

    net_roi = net_profit / all_in_cost if all_in_cost > 0 else -999
    gross_spread = (expected_resale - asking) / asking if asking > 0 else -999

    reasons = []
    warnings = []
    hard_reject = False
    score = 0.0

    if asking <= 0 or expected_resale <= 0:
        hard_reject = True
        reasons.append("Missing asking price or resale comp.")

    reject_words = list(config.get("reject_keywords_lt", []))
    cat_rules = config.get("category_rules", {}).get(category, config.get("category_rules", {}).get("general", {}))
    reject_words += cat_rules.get("extra_reject_keywords", [])
    rejected_terms = has_any(text, reject_words)
    if rejected_terms:
        hard_reject = True
        reasons.append("Reject keywords found: " + ", ".join(rejected_terms[:8]))

    caution_terms = has_any(text, config.get("caution_keywords_lt", []))
    if caution_terms:
        warnings.append("Caution keywords: " + ", ".join(caution_terms[:8]))
        score -= min(15, 3 * len(caution_terms))

    positive_terms = has_any(text, config.get("positive_keywords_lt", []))
    if positive_terms:
        reasons.append("Positive condition terms: " + ", ".join(positive_terms[:6]))
        score += min(15, 3 * len(positive_terms))

    allowed_locations = [x.lower() for x in config.get("allowed_locations", [])]
    if config.get("prefer_local_pickup_only", True) and allowed_locations:
        if not any(loc in location for loc in allowed_locations):
            warnings.append(f"Outside preferred pickup area: {location}")
            score -= 20
        else:
            score += 10
            reasons.append("Inside preferred pickup area.")

    can_inspect = norm_text(row.get("can_inspect_in_person")) in ("yes", "y", "true", "1", "taip")
    if not can_inspect:
        hard_reject = True
        reasons.append("Cannot inspect in person.")
    else:
        score += 15
        reasons.append("Can inspect in person.")

    photo_quality = norm_text(row.get("photo_quality"))
    if photo_quality in ("good", "high", "clear", "gera", "aiški", "aiski"):
        score += 8
        reasons.append("Good/clear photos.")
    elif photo_quality in ("poor", "bad", "blurry", "blogos", "prastos"):
        warnings.append("Poor/blurry photos.")
        score -= 10

    if asking > config.get("max_purchase_price_eur", 250):
        warnings.append(f"Asking price above starter max capital rule: €{asking:.2f}")
        score -= 20

    if liquidity >= config.get("min_liquidity_sold_count", 3):
        score += min(15, liquidity * 2)
        reasons.append(f"Liquidity OK: {liquidity} sold comps.")
    else:
        warnings.append(f"Low sold-comp liquidity: {liquidity}.")
        score -= 12

    # Profit rules
    if net_profit >= config["min_net_profit_eur"]:
        score += 25
        reasons.append(f"Net profit clears threshold: €{net_profit:.2f}.")
    else:
        hard_reject = True
        reasons.append(f"Net profit too low: €{net_profit:.2f} < €{config['min_net_profit_eur']:.2f}.")

    if net_roi >= config["min_net_roi"]:
        score += 20
        reasons.append(f"Net ROI clears threshold: {net_roi*100:.1f}%.")
    else:
        hard_reject = True
        reasons.append(f"Net ROI too low: {net_roi*100:.1f}% < {config['min_net_roi']*100:.1f}%.")

    if gross_spread >= config["min_gross_spread_pct"]:
        score += 15
        reasons.append(f"Gross spread clears threshold: {gross_spread*100:.1f}%.")
    else:
        hard_reject = True
        reasons.append(f"Gross spread too low: {gross_spread*100:.1f}% < {config['min_gross_spread_pct']*100:.1f}%.")

    # Decision tiers
    if hard_reject:
        decision = "PASS"
    else:
        if score >= 90 and net_profit >= 150 and net_roi >= 0.80:
            decision = "PRIORITY_INSPECT"
        elif score >= 65:
            decision = "INSPECT"
        else:
            decision = "WATCHLIST"

    return ScanResult(
        decision=decision,
        score=round(score, 1),
        reasons=reasons,
        warnings=warnings,
        title=title,
        category=category,
        location=location,
        asking_price_eur=round(asking, 2),
        expected_resale_eur=round(expected_resale, 2),
        all_in_cost_eur=round(all_in_cost, 2),
        platform_fee_eur=round(platform_fee, 2),
        net_profit_eur=round(net_profit, 2),
        net_roi_pct=round(net_roi * 100, 1),
        gross_spread_pct=round(gross_spread * 100, 1),
        liquidity_sold_count=liquidity,
        url=url,
    )


def sort_results(results: List[ScanResult]) -> List[ScanResult]:
    """Sort strongest opportunities first.

    Order: decision tier, then highest net profit, then highest score.
    """
    return sorted(
        results,
        key=lambda r: (
            DECISION_ORDER.get(r.decision, 9),
            -r.net_profit_eur,
            -r.score,
        ),
    )


def load_config(path: str | None) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        user_cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        # shallow merge; nested category_rules can be replaced if desired
        for k, v in user_cfg.items():
            cfg[k] = v
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="Score Lithuanian local listings for resale arbitrage.")
    parser.add_argument("input_csv", help="Input CSV path")
    parser.add_argument("--config", help="Optional config JSON path")
    parser.add_argument("--output", default="scan_results.csv", help="Output CSV path")
    args = parser.parse_args()

    config = load_config(args.config)

    with open(args.input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    results = sort_results([scan_row(row, config) for row in rows])

    out_fields = [
        "decision", "score", "title", "category", "location", "asking_price_eur",
        "expected_resale_eur", "all_in_cost_eur", "platform_fee_eur", "net_profit_eur",
        "net_roi_pct", "gross_spread_pct", "liquidity_sold_count", "reasons",
        "warnings", "url"
    ]

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for r in results:
            d = asdict(r)
            d["reasons"] = " | ".join(r.reasons)
            d["warnings"] = " | ".join(r.warnings)
            writer.writerow({k: d.get(k, "") for k in out_fields})

    print(f"Wrote {args.output}")
    print()
    for r in results[:10]:
        print(f"{r.decision:16} €{r.net_profit_eur:>7.2f} ROI {r.net_roi_pct:>6.1f}% | {r.title[:70]}")


if __name__ == "__main__":
    main()
