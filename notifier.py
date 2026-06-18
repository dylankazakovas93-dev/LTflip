#!/usr/bin/env python3
"""
Notifier scaffold for the Lithuania Flip Scanner (v0.4).

Reads scan_results.csv and prints alert messages for the rows worth acting on:
PRIORITY_INSPECT and INSPECT. Everything else is ignored.

This is a scaffold on purpose: it prints to the console and contains clearly
marked TODOs for Telegram / Discord delivery. No credentials are required yet.

Run:
    python3 notifier.py scan_results.csv
"""

from __future__ import annotations

import argparse
import csv
from typing import Any, Dict, List

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


def build_alerts(rows: List[Dict[str, Any]]) -> List[str]:
    """Return formatted alert strings for INSPECT / PRIORITY_INSPECT rows, strongest first."""
    picked = [r for r in rows if (r.get("decision") or "").strip().upper() in ALERT_DECISIONS]
    picked.sort(key=lambda r: (
        _ORDER.get((r.get("decision") or "").strip().upper(), 9),
        -_to_float(r.get("net_profit_eur")),
    ))
    return [format_alert(r) for r in picked]


def notify(messages: List[str]) -> None:
    """Deliver alerts. For now: print. See TODOs below to add webhooks."""
    for message in messages:
        print(message)
        print()
        # TODO: send_telegram(message)
        # TODO: send_discord(message)


# --- TODO: webhook delivery (no credentials wired up yet) -------------------
#
# Telegram:
#   1. Create a bot via @BotFather, get TELEGRAM_BOT_TOKEN.
#   2. Get your TELEGRAM_CHAT_ID.
#   3. POST to https://api.telegram.org/bot<token>/sendMessage
#      with {"chat_id": chat_id, "text": message}.
#
# Discord:
#   1. Channel Settings -> Integrations -> Webhooks -> New Webhook, copy URL.
#   2. POST to the webhook URL with {"content": message}.
#
# Keep secrets in environment variables, never commit them.

def send_telegram(message: str, *, token: str = "", chat_id: str = "") -> None:
    raise NotImplementedError("TODO: wire up Telegram sendMessage (see notes above).")


def send_discord(message: str, *, webhook_url: str = "") -> None:
    raise NotImplementedError("TODO: wire up Discord webhook (see notes above).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Print alerts for INSPECT / PRIORITY_INSPECT scan results.")
    ap.add_argument("input_csv", nargs="?", default="scan_results.csv", help="Scan results CSV")
    args = ap.parse_args()

    with open(args.input_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    messages = build_alerts(rows)
    if not messages:
        print("No INSPECT / PRIORITY_INSPECT opportunities to alert on.")
        return

    print(f"{len(messages)} opportunit{'y' if len(messages) == 1 else 'ies'} worth inspecting:\n")
    notify(messages)


if __name__ == "__main__":
    main()
