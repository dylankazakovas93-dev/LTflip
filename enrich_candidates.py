#!/usr/bin/env python3
"""
Enrich raw collector output into scanner-ready candidate listings (v0.4).

Pipeline position:
    collector -> raw_listings.csv -> [THIS STEP] -> candidate_listings.csv -> scanner

What it does for each raw row:
- Classifies the likely item category: lens / music_gear / lego_collectible / general.
- Rejects obvious broken/repair/unknown-condition listings early (same Lithuanian
  keyword list the scanner uses), so they never reach the scanner.
- Sets can_inspect_in_person = yes only when the location is an allowed pickup city
  (Vilnius/Kaunas by default), otherwise no.
- Estimates photo_quality from how many images were collected, else 'unknown'.
- Leaves comp_low_eur / comp_median_eur blank: YOU fill these from eBay *sold*
  comps before scoring. Asking prices are not comps.

Output columns exactly match input_template.csv, so the result drops straight
into listing_scanner.py.

Run:
    python3 enrich_candidates.py raw_listings.csv --output candidate_listings.csv
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

from listing_scanner import has_any, load_config, norm_text


# Columns the scanner reads (identical to input_template.csv).
TEMPLATE_FIELDS = [
    "url", "category", "title", "description", "location", "asking_price_eur",
    "expected_resale_eur", "comp_low_eur", "comp_median_eur", "liquidity_sold_count",
    "platform_fee_pct", "risk_reserve_pct", "buy_transport_cost_eur",
    "resale_shipping_cost_eur", "packaging_cost_eur", "misc_cost_eur",
    "can_inspect_in_person", "photo_quality",
]

# Keyword hints for category classification (checked in order).
CATEGORY_KEYWORDS = {
    "lens": [
        "objektyvas", "objektyvai", "lens", "canon ef", "sony e", "sony fe",
        "nikon", "sigma", "tamron", "zeiss", "fujinon", "mm f", "f/1", "f/2",
        "f1.8", "f2.8", "telephoto", "wide angle",
    ],
    "music_gear": [
        "pedal", "pedalas", "gitara", "guitar", "stratocaster", "telecaster",
        "amp", "stiprintuvas", "fuzz", "overdrive", "delay", "reverb", "boss ",
        "synth", "sintezatorius", "mikrofonas", "efektai", "effektai", "looper",
    ],
    "lego_collectible": [
        "lego", "minifig", "minifigure", "sealed", "collectible", "kolekcin",
        "funko", "retro zaislas",
    ],
}


def detect_category(text: str, hint: str = "") -> str:
    """Classify the item. Title/search keywords win; fall back to the source hint."""
    for category, keywords in CATEGORY_KEYWORDS.items():
        if has_any(text, keywords):
            return category
    if hint in ("lens", "music_gear", "lego_collectible", "general"):
        return hint
    return "general"


def photo_quality_from_images(image_field: str) -> str:
    """3+ images -> good, 1-2 -> medium, none -> unknown."""
    count = len([u for u in (image_field or "").split() if u.strip()])
    if count >= 3:
        return "good"
    if count >= 1:
        return "medium"
    return "unknown"


def enrich_rows(
    rows: List[Dict[str, Any]], config: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], List[str]]]]:
    """Return (candidate_rows, skipped) where skipped is (row, matched_keywords)."""
    reject_words = config.get("reject_keywords_lt", [])
    allowed = [a.lower() for a in config.get("allowed_locations", [])]

    candidates: List[Dict[str, Any]] = []
    skipped: List[Tuple[Dict[str, Any], List[str]]] = []

    for row in rows:
        title = (row.get("title") or "").strip()
        description = (row.get("description") or "").strip()
        search_name = row.get("search_name") or ""
        hint = norm_text(row.get("category"))
        text = norm_text(" ".join([search_name, title, description]))

        hits = has_any(text, reject_words)
        if hits:
            skipped.append((row, hits))
            continue

        location = (row.get("location") or "").strip()
        can_inspect = "yes" if any(a in location.lower() for a in allowed) else "no"

        candidate = {field: "" for field in TEMPLATE_FIELDS}
        candidate.update({
            "url": row.get("url", ""),
            "category": detect_category(text, hint),
            "title": title,
            "description": description,
            "location": location,
            "asking_price_eur": row.get("asking_price_eur", ""),
            "can_inspect_in_person": can_inspect,
            "photo_quality": photo_quality_from_images(row.get("image_urls", "")),
        })
        candidates.append(candidate)

    return candidates, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description="Normalize raw collector rows into scanner-ready candidates.")
    ap.add_argument("input_csv", help="Raw listings CSV (from the collector)")
    ap.add_argument("--output", default="candidate_listings.csv", help="Output CSV path")
    ap.add_argument("--config", help="Optional config JSON (defaults to config.json if present)")
    args = ap.parse_args()

    config_path = args.config
    if config_path is None and Path("config.json").exists():
        config_path = "config.json"
    config = load_config(config_path)

    with open(args.input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    candidates, skipped = enrich_rows(rows, config)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TEMPLATE_FIELDS)
        writer.writeheader()
        for c in candidates:
            writer.writerow(c)

    print(f"Wrote {args.output}: {len(candidates)} candidates "
          f"({len(skipped)} rejected early on broken/condition keywords).")
    print("Reminder: fill comp_low_eur / comp_median_eur from eBay SOLD comps before scoring.")


if __name__ == "__main__":
    main()
