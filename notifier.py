#!/usr/bin/env python3
"""
Two-stage notifier for the Lithuania Flip Scanner (v0.6).

Flips are time-sensitive, so there are two alert stages:

  --mode research : reads comp_review_queue.csv and alerts on promising NEW
                    listings that still need eBay SOLD-comp research. These are
                    NOT buy signals - there is no decision until comps are filled.
  --mode action   : reads scan_results.csv and alerts on listings that already
                    pass the scanner as INSPECT / PRIORITY_INSPECT.
  --mode both     : research first, then action.

Delivery:
- Always prints to the console.
- Also sends to Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set;
  otherwise console only.

Anti-spam:
- Research alerts remember what they have already alerted on (in the seen-state
  file, channel "research"), so the same comp-review listing is not repeated.
- Action alerts use the collector's new-this-run set (only freshly collected
  listings). Use --ignore-seen to alert on everything.

Run:
    python3 notifier.py --mode research --review-queue comp_review_queue.csv
    python3 notifier.py --mode action   scan_results.csv
    python3 notifier.py --mode both
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import seen_state
from listing_scanner import has_any, load_config, norm_text, parse_float

# --- Action alerts (scanner results) ---------------------------------------

ALERT_DECISIONS = ("PRIORITY_INSPECT", "INSPECT")
_ORDER = {"PRIORITY_INSPECT": 0, "INSPECT": 1}


def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, AttributeError):
        return 0.0


def format_alert(row: Dict[str, Any]) -> str:
    decision = (row.get("decision") or "").strip()
    icon = "🔥" if decision == "PRIORITY_INSPECT" else "🔎"
    return (
        f"{icon} {decision} | net €{row.get('net_profit_eur', '?')} "
        f"ROI {row.get('net_roi_pct', '?')}% | {row.get('title', '')}\n"
        f"   {row.get('location', '')} | ask €{row.get('asking_price_eur', '?')} "
        f"-> resale €{row.get('expected_resale_eur', '?')}\n"
        f"   inspect first, never buy untested · {row.get('url', '')}"
    )


def build_alerts(rows: List[Dict[str, Any]], new_urls: Optional[Set[str]] = None) -> List[str]:
    """Formatted alerts for INSPECT / PRIORITY_INSPECT rows, strongest first.

    When ``new_urls`` is given, only listings with those URLs are alerted
    (everything else is treated as already-seen and suppressed).
    """
    picked = [r for r in rows if (r.get("decision") or "").strip().upper() in ALERT_DECISIONS]
    if new_urls is not None:
        picked = [r for r in picked if r.get("url", "") in new_urls]
    picked.sort(key=lambda r: (
        _ORDER.get((r.get("decision") or "").strip().upper(), 9),
        -_to_float(r.get("net_profit_eur")),
    ))
    return [format_alert(r) for r in picked]


# --- Research alerts (comp-review queue) -----------------------------------

def score_research_item(row: Dict[str, Any], config: Dict[str, Any]):
    """Lightweight pre-comp priority score. Returns (score, reasons)."""
    title = (row.get("title") or "").strip()
    category = norm_text(row.get("category"))
    location = norm_text(row.get("location"))
    model_guess = (row.get("model_guess") or "").strip()
    photo = norm_text(row.get("photo_quality"))
    text = norm_text(" ".join([title, row.get("description") or ""]))

    allowed_locs = [x.lower() for x in config.get("allowed_locations", [])]
    target_cats = config.get("allowed_research_categories", [])
    positives = config.get("positive_keywords_lt", [])
    cautions = config.get("caution_keywords_lt", [])

    score = 0.0
    reasons: List[str] = []

    if allowed_locs and any(a in location for a in allowed_locs):
        score += 2.0
        reasons.append(f"local pickup ({location.title()})")
    if category in target_cats:
        score += 2.0
        reasons.append(f"target category: {category}")
    if model_guess and model_guess.lower() != "unknown":
        score += 2.0
        reasons.append(f"identified model: {model_guess}")
    if has_any(text, positives):
        score += 1.5
        reasons.append("positive condition wording")
    if has_any(text, cautions):
        score -= 0.5
        reasons.append("caution wording")

    if photo == "good":
        score += 1.0
        reasons.append("clear photos")
    elif photo in ("", "unknown", "poor", "bad", "blurry"):
        score -= 1.0
        reasons.append("weak/no photos")

    if len(title.split()) < 3:
        score -= 1.0
        reasons.append("vague title")

    if parse_float(row.get("asking_price_eur"), 0) <= 0:
        score -= 1.0
        reasons.append("price missing/negotiable")

    return round(score, 2), reasons


def select_research_items(
    rows: Sequence[Dict[str, Any]],
    config: Dict[str, Any],
    already_alerted: Set[str] = frozenset(),
) -> List[Dict[str, Any]]:
    """Filter, score, sort and cap comp-review rows for alerting.

    Returns rows (copies) annotated with ``_score`` and ``_reasons``, strongest
    first, capped at ``max_research_alerts_per_run``.
    """
    min_score = float(config.get("min_research_alert_score", 3.0))
    max_alerts = int(config.get("max_research_alerts_per_run", 10))
    target_cats = config.get("allowed_research_categories", [])
    reject_words = config.get("reject_keywords_lt", [])
    min_price = float(config.get("min_asking_price_eur", 0))
    max_price = float(config.get("max_asking_price_eur", 1e9))

    selected: List[Dict[str, Any]] = []
    for row in rows:
        url = row.get("url", "")
        if url and url in already_alerted:
            continue
        category = norm_text(row.get("category"))
        if target_cats and category not in target_cats:
            continue
        text = norm_text(" ".join([row.get("title") or "", row.get("description") or ""]))
        if has_any(text, reject_words):   # defensive: broken/repair never alerts
            continue
        price = parse_float(row.get("asking_price_eur"), 0)
        if price > 0 and not (min_price <= price <= max_price):
            continue
        score, reasons = score_research_item(row, config)
        if score < min_score:
            continue
        annotated = dict(row)
        annotated["_score"] = score
        annotated["_reasons"] = reasons
        selected.append(annotated)

    selected.sort(key=lambda r: (-r["_score"], r.get("title", "")))
    return selected[:max_alerts]


def format_research_alert(row: Dict[str, Any]) -> str:
    reasons = "; ".join(row.get("_reasons") or []) or "promising local listing"
    return (
        f"🔬 RESEARCH | {row.get('category', '')} | {row.get('title', '')}\n"
        f"   {row.get('location', '')} | ask €{row.get('asking_price_eur', '?')} | "
        f"model_guess: {row.get('model_guess', 'Unknown')} | score {row.get('_score', '?')}\n"
        f"   why queued: {reasons}\n"
        f"   sold comps: {row.get('suggested_ebay_sold_search', '')}\n"
        f"   source: {row.get('source', '')} · {row.get('url', '')}\n"
        f"   ⚠ NO BUY DECISION yet — fill sold comps, then run the scanner."
    )


def build_research_alerts(
    rows: Sequence[Dict[str, Any]],
    config: Dict[str, Any],
    already_alerted: Set[str] = frozenset(),
) -> List[str]:
    """Formatted research alerts, strongest first (convenience wrapper)."""
    return [format_research_alert(r) for r in select_research_items(rows, config, already_alerted)]


# --- Telegram delivery ------------------------------------------------------

def telegram_api_url(token: str) -> str:
    return f"https://api.telegram.org/bot{token}/sendMessage"


def build_telegram_payload(message: str, chat_id: str) -> Dict[str, str]:
    return {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}


def telegram_from_env() -> Optional[Dict[str, str]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return {"token": token, "chat_id": chat_id}
    return None


def send_telegram(message: str, *, token: str, chat_id: str, timeout: int = 10) -> bool:
    """POST one message to Telegram. Returns True on success."""
    data = urlencode(build_telegram_payload(message, chat_id)).encode("utf-8")
    req = Request(telegram_api_url(token), data=data,
                  headers={"User-Agent": "LTFlipScanner/0.6"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except Exception as exc:
        print(f"  [telegram error] {exc}")
        return False


# --- TODO: Discord webhook (not wired up yet) -------------------------------
# Create a channel webhook (Channel Settings -> Integrations -> Webhooks),
# keep the URL in an env var, and POST {"content": message} to it.
def send_discord(message: str, *, webhook_url: str = "") -> None:
    raise NotImplementedError("TODO: wire up Discord webhook delivery.")


def notify(messages: List[str], telegram: Optional[Dict[str, str]] = None) -> None:
    """Print every alert; also send to Telegram when configured."""
    for message in messages:
        print(message)
        print()
    if telegram and messages:
        sent = sum(send_telegram(m, token=telegram["token"], chat_id=telegram["chat_id"])
                   for m in messages)
        print(f"Telegram: delivered {sent}/{len(messages)} alerts.")


# --- Stage runners ----------------------------------------------------------

def _read_csv(path: str) -> List[Dict[str, Any]]:
    if not Path(path).exists():
        print(f"  [skip] {path} not found.")
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def run_research(args, config, state, telegram) -> None:
    rows = _read_csv(args.review_queue)
    if not rows:
        return
    already = set() if args.ignore_seen else seen_state.alerted_urls(state, "research")
    selected = select_research_items(rows, config, already_alerted=already)

    print(f"\n[research] {len(selected)} new comp-review listing(s) to research "
          f"(of {len(rows)} queued):\n")
    if not selected:
        print("  Nothing new to research right now.")
        return

    notify([format_research_alert(r) for r in selected], telegram=telegram)

    if not args.ignore_seen:
        seen_state.mark_alerted(state, "research", [r.get("url", "") for r in selected])
        seen_state.save_state(state, args.state)


def run_action(args, config, state, telegram) -> None:
    rows = _read_csv(args.scan_results)
    if not rows:
        return
    new = None
    if not args.ignore_seen and Path(args.state).exists():
        new = seen_state.new_urls(state)
    messages = build_alerts(rows, new_urls=new)

    print(f"\n[action] {len(messages)} listing(s) worth inspecting now:\n")
    if not messages:
        print("  No new INSPECT / PRIORITY_INSPECT opportunities.")
        return
    notify(messages, telegram=telegram)


def main() -> None:
    ap = argparse.ArgumentParser(description="Two-stage alerts: research (comp queue) and action (scan results).")
    ap.add_argument("input_csv", nargs="?", help="(action mode) scan results CSV; same as --scan-results")
    ap.add_argument("--mode", choices=["research", "action", "both"], default="action",
                    help="Which alerts to emit (default: action)")
    ap.add_argument("--scan-results", default="scan_results.csv", help="Scan results CSV (action)")
    ap.add_argument("--review-queue", default="comp_review_queue.csv", help="Comp review queue CSV (research)")
    ap.add_argument("--config", help="Optional config JSON (defaults to config.json if present)")
    ap.add_argument("--state", default=seen_state.DEFAULT_STATE_PATH, help="Seen-state file")
    ap.add_argument("--ignore-seen", action="store_true", help="Alert on everything, ignore seen/alerted state")
    args = ap.parse_args()
    if args.input_csv:               # backward-compatible positional for action mode
        args.scan_results = args.input_csv

    config_path = args.config
    if config_path is None and Path("config.json").exists():
        config_path = "config.json"
    config = load_config(config_path)
    state = seen_state.load_state(args.state)

    telegram = telegram_from_env()
    print("Telegram configured; will also deliver alerts." if telegram
          else "Telegram not configured; console output only.")

    if args.mode in ("research", "both"):
        run_research(args, config, state, telegram)
    if args.mode in ("action", "both"):
        run_action(args, config, state, telegram)


if __name__ == "__main__":
    main()
