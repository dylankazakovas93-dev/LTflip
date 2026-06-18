#!/usr/bin/env python3
"""
Notifier for the Lithuania Flip Scanner (v0.5).

Reads scan_results.csv and alerts on the rows worth acting on
(PRIORITY_INSPECT and INSPECT). Everything else is ignored.

Delivery:
- Always prints to the console.
- If TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in the environment, also
  sends each alert to Telegram. If they are missing, it just uses the console.

Fresh-listing filter:
- If a seen-state file (.seen_listings.json, written by the collector) is
  present, only listings that were NEW on the last collector run are alerted,
  so you are never re-spammed about listings you have already seen. Use
  --ignore-seen to alert on everything.

Run:
    python3 notifier.py scan_results.csv
    TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python3 notifier.py scan_results.csv
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import seen_state

# Only these decisions are worth an alert.
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
                  headers={"User-Agent": "LTFlipScanner/0.5"})
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
    if telegram:
        sent = sum(send_telegram(m, token=telegram["token"], chat_id=telegram["chat_id"])
                   for m in messages)
        print(f"Telegram: delivered {sent}/{len(messages)} alerts.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Alert on INSPECT / PRIORITY_INSPECT scan results.")
    ap.add_argument("input_csv", nargs="?", default="scan_results.csv", help="Scan results CSV")
    ap.add_argument("--state", default=seen_state.DEFAULT_STATE_PATH,
                    help="Seen-state file used to alert on new listings only")
    ap.add_argument("--ignore-seen", action="store_true",
                    help="Alert on all qualifying rows, even already-seen ones")
    args = ap.parse_args()

    with open(args.input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    new_urls: Optional[Set[str]] = None
    if not args.ignore_seen and Path(args.state).exists():
        new_urls = seen_state.new_urls(seen_state.load_state(args.state))
        print(f"Fresh-listing filter on ({len(new_urls)} new since last collect). "
              f"Use --ignore-seen to disable.")

    messages = build_alerts(rows, new_urls=new_urls)

    telegram = telegram_from_env()
    print("Telegram configured; will also deliver alerts." if telegram
          else "Telegram not configured; console output only.")

    if not messages:
        print("\nNo new INSPECT / PRIORITY_INSPECT opportunities to alert on.")
        return

    print(f"\n{len(messages)} opportunit{'y' if len(messages) == 1 else 'ies'} worth inspecting:\n")
    notify(messages, telegram=telegram)


if __name__ == "__main__":
    main()
