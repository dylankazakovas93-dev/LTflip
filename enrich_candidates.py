#!/usr/bin/env python3
r"""
Enrich raw collector output into scanner-ready candidates + a comp review queue (v0.5).

Pipeline position:
    collector -> raw_listings.csv -> [THIS STEP] -> candidate_listings.csv -> scanner
                                                  \-> comp_review_queue.csv (research these)

For each raw row this step:
- Classifies the likely category: lens / music_gear / lego_collectible / general.
- Rejects obvious broken/repair/unknown-condition listings early (same Lithuanian
  keyword list the scanner uses), so they never reach the scanner.
- Sets can_inspect_in_person = yes only for an allowed pickup city (Vilnius/Kaunas).
- Estimates photo_quality from how many images were collected.
- Makes a CONSERVATIVE model_guess (or "Unknown" - it never pretends certainty).
- Leaves comp_low_eur / comp_median_eur blank: YOU fill these from eBay *sold*
  comps. Asking prices are not comps.

candidate_listings.csv columns match input_template.csv exactly, so it drops
straight into listing_scanner.py. comp_review_queue.csv is a worklist of
promising local listings that still need sold-comp research, each with a ready
eBay *sold* search link.

Run:
    python3 enrich_candidates.py raw_listings.csv --output candidate_listings.csv
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote_plus

from listing_scanner import has_any, load_config, norm_text, parse_float


# Columns the scanner reads (identical to input_template.csv).
TEMPLATE_FIELDS = [
    "url", "category", "title", "description", "location", "asking_price_eur",
    "expected_resale_eur", "comp_low_eur", "comp_median_eur", "liquidity_sold_count",
    "platform_fee_pct", "risk_reserve_pct", "buy_transport_cost_eur",
    "resale_shipping_cost_eur", "packaging_cost_eur", "misc_cost_eur",
    "can_inspect_in_person", "photo_quality",
]

# Columns for the comp research worklist.
REVIEW_FIELDS = [
    "source", "url", "title", "location", "asking_price_eur", "category",
    "model_guess", "suggested_ebay_sold_search",
    "comp_low_eur", "comp_median_eur", "liquidity_sold_count",
]

TARGET_CATEGORIES = {"lens", "music_gear", "lego_collectible"}

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


# --- Conservative model guessing -------------------------------------------
#
# The goal is NOT to be clever. We only emit a guess when we can pull a brand
# plus a concrete model token (a focal length, or a model code). Otherwise we
# return "Unknown" so a human knows to look closer. Never overclaim.

CAMERA_BRANDS = [
    "Canon", "Nikon", "Sony", "Fujifilm", "Fujinon", "Olympus", "Panasonic",
    "Pentax", "Leica", "Sigma", "Tamron", "Tokina", "Samyang", "Viltrox", "Zeiss",
]
# Lens mount tokens, longest first (matched case-sensitively against the title).
LENS_MOUNTS = ["EF-S", "EF-M", "EF", "RF", "FE", "MFT", "Z", "X", "E", "L"]
LENS_SUFFIXES = ["USM", "STM", "IS", "VR", "DG", "HSM", "OS"]
MUSIC_BRANDS = [
    "Boss", "Strymon", "MXR", "Electro-Harmonix", "EHX", "TC Electronic",
    "Roland", "Yamaha", "Fender", "Ibanez", "Behringer", "Korg", "Nord",
    "Marshall", "Line 6", "Digitech", "Zoom", "Walrus", "JHS",
]


def _first_brand(title_low: str, brands: List[str]):
    """The brand that appears earliest in the (lowercased) title, if any."""
    best, best_pos = None, None
    for b in brands:
        m = re.search(r"(?<![a-z])" + re.escape(b.lower()) + r"(?![a-z])", title_low)
        if m and (best_pos is None or m.start() < best_pos):
            best, best_pos = b, m.start()
    return best


def _token_present(title: str, token: str) -> bool:
    return re.search(r"(?<![A-Za-z])" + re.escape(token) + r"(?![A-Za-z])", title) is not None


def _guess_lens(title: str) -> str:
    low = title.lower()
    brand = _first_brand(low, CAMERA_BRANDS)
    if not brand:
        return ""

    m = (re.search(r"\b(\d{1,3}-\d{1,3})\s*mm\b", low)
         or re.search(r"\b(\d{1,3})\s*mm\b", low)
         or re.search(r"\b(\d{1,3}-\d{1,3})\b", low))   # a range is almost always a focal length
    if not m:
        return ""
    focal = m.group(1) + "mm"

    am = re.search(r"\bf\s*/?\s*(\d{1,2}(?:\.\d)?)", low)
    aperture = "f/" + am.group(1) if am else ""

    mount = next((mt for mt in LENS_MOUNTS if _token_present(title, mt)), "")
    # A different camera brand mentioned => third-party lens compatibility.
    system = _first_brand(low.replace(brand.lower(), "", 1), CAMERA_BRANDS)

    parts = [brand]
    if mount:
        parts.append(mount)
    parts.append(focal)
    if aperture:
        parts.append(aperture)
    if system and not mount:
        parts.append(system)
    parts += [sfx for sfx in LENS_SUFFIXES if _token_present(title, sfx)]
    return " ".join(parts)


def _guess_music(title: str) -> str:
    brand = _first_brand(title.lower(), MUSIC_BRANDS)
    if not brand:
        return ""
    m = re.search(r"(?<![A-Za-z0-9])([A-Z]{1,4}-?\d{1,3}[A-Z]?)(?![A-Za-z0-9])", title)
    if not m:
        return ""
    return f"{brand} {m.group(1)}"


def _guess_lego(title: str) -> str:
    if "lego" not in title.lower():
        return ""
    m = re.search(r"\b(\d{4,5})\b", title)   # LEGO set numbers are 4-5 digits
    return f"LEGO {m.group(1)}" if m else ""


def guess_model(title: str, category: str = "") -> str:
    """Best-effort, conservative model string, or 'Unknown'."""
    title = (title or "").strip()
    if not title:
        return "Unknown"
    for guesser in (_guess_lens, _guess_music, _guess_lego):
        guess = guesser(title)
        if guess:
            return guess
    return "Unknown"


# Lithuanian/filler words to drop when falling back to title-based search terms.
_SEARCH_STOPWORDS = {
    "objektyvas", "objektyvai", "pedalas", "pedalai", "geros", "būklės", "bukles",
    "su", "be", "ir", "naudotas", "naudota", "dėžute", "dezute", "kaip", "nauja",
    "sealed", "digital", "delay",
}


def build_sold_search(model_guess: str, title: str, category: str = "") -> str:
    """A ready eBay link filtered to SOLD/Completed items (never asking prices)."""
    if model_guess and model_guess != "Unknown":
        query = model_guess
    else:
        tokens = [t for t in re.split(r"\s+", title or "")
                  if t and t.lower() not in _SEARCH_STOPWORDS]
        query = " ".join(tokens[:6]) or (title or "")
    return ("https://www.ebay.com/sch/i.html?_nkw=" + quote_plus(query)
            + "&LH_Sold=1&LH_Complete=1")


def enrich_rows(
    rows: List[Dict[str, Any]], config: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Tuple[Dict[str, Any], List[str]]], List[Dict[str, Any]]]:
    """Return (candidate_rows, skipped, review_rows).

    skipped is a list of (row, matched_reject_keywords).
    review_rows are promising local listings that still need sold-comp research.
    """
    reject_words = config.get("reject_keywords_lt", [])
    allowed = [a.lower() for a in config.get("allowed_locations", [])]

    candidates: List[Dict[str, Any]] = []
    skipped: List[Tuple[Dict[str, Any], List[str]]] = []
    review: List[Dict[str, Any]] = []

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
        category = detect_category(text, hint)

        candidate = {field: "" for field in TEMPLATE_FIELDS}
        candidate.update({
            "url": row.get("url", ""),
            "category": category,
            "title": title,
            "description": description,
            "location": location,
            "asking_price_eur": row.get("asking_price_eur", ""),
            "can_inspect_in_person": can_inspect,
            "photo_quality": photo_quality_from_images(row.get("image_urls", "")),
        })
        candidates.append(candidate)

        # Promising = locally inspectable, a target category, and a real price.
        model = guess_model(title, category)
        price = parse_float(row.get("asking_price_eur"), 0)
        if can_inspect == "yes" and category in TARGET_CATEGORIES and price > 0:
            review.append({
                "source": row.get("source") or "Skelbiu",
                "url": candidate["url"],
                "title": title,
                "location": location,
                "asking_price_eur": row.get("asking_price_eur", ""),
                "category": category,
                "model_guess": model,
                "suggested_ebay_sold_search": build_sold_search(model, title, category),
                "comp_low_eur": "",
                "comp_median_eur": "",
                "liquidity_sold_count": "",
            })

    return candidates, skipped, review


def _write_csv(path: str, fields: List[str], rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fields})


def main() -> None:
    ap = argparse.ArgumentParser(description="Normalize raw collector rows into scanner-ready candidates.")
    ap.add_argument("input_csv", help="Raw listings CSV (from the collector)")
    ap.add_argument("--output", default="candidate_listings.csv", help="Candidate CSV path")
    ap.add_argument("--review-queue", default="comp_review_queue.csv", help="Comp review worklist CSV path")
    ap.add_argument("--config", help="Optional config JSON (defaults to config.json if present)")
    args = ap.parse_args()

    config_path = args.config
    if config_path is None and Path("config.json").exists():
        config_path = "config.json"
    config = load_config(config_path)

    with open(args.input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    candidates, skipped, review = enrich_rows(rows, config)

    _write_csv(args.output, TEMPLATE_FIELDS, candidates)
    _write_csv(args.review_queue, REVIEW_FIELDS, review)

    print(f"Wrote {args.output}: {len(candidates)} candidates "
          f"({len(skipped)} rejected early on broken/condition keywords).")
    print(f"Wrote {args.review_queue}: {len(review)} listings to research comps for.")
    print("Reminder: fill comp_low_eur / comp_median_eur from eBay SOLD comps before scoring.")


if __name__ == "__main__":
    main()
