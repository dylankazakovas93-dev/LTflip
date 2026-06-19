#!/usr/bin/env python3
"""
One-command LOCAL scan for the Lithuania Flip Scanner (v0.7).

Runs the full no-comp discovery workflow on your Mac:

    browser_navigator  ->  raw_listings.csv
                        ->  enrich_candidates  ->  candidate_listings.csv
                                              ->  comp_review_queue.csv
                        ->  notifier (research alerts)

You only review the research alerts — not every listing. There is no buy
decision here: comps are still blank, so the scanner stage is intentionally not
run. Fill comps and run listing_scanner.py + notifier --mode action separately.

Run on your Mac:
    python3 run_local_scan.py

Dry run (skip the browser, reuse an existing raw_listings.csv):
    python3 run_local_scan.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, Optional

import browser_navigator
import enrich_candidates
import notifier
import seen_state
from listing_scanner import load_config


def run_local_scan(
    *,
    browser_config: str = "browser_sources.json",
    app_config: Optional[str] = None,
    raw_path: str = "raw_listings.csv",
    candidate_path: str = "candidate_listings.csv",
    review_path: str = "comp_review_queue.csv",
    state_path: str = seen_state.DEFAULT_STATE_PATH,
    dry_run: bool = False,
    ignore_seen: bool = False,
    fetch_factory=browser_navigator.make_playwright_fetch,
) -> Dict[str, Any]:
    """Run the local discovery workflow. Returns a summary dict.

    With ``dry_run=True`` the browser step is skipped and an existing
    ``raw_path`` is reused (used by tests and for re-running enrichment/alerts).
    """
    summary: Dict[str, Any] = {"dry_run": dry_run, "blocked": None}

    # 1) Browse (unless dry-run).
    if not dry_run:
        cfg = json.loads(Path(browser_config).read_text(encoding="utf-8"))
        print("== Browsing configured searches ==")
        result = browser_navigator.collect(cfg, raw_path, fetch_factory=fetch_factory)
        summary["blocked"] = result.get("blocked")
        if summary["blocked"]:
            print(f"!! Stopped on block: {summary['blocked'][1]}. "
                  f"Reporting what was collected before the block.")

    if not Path(raw_path).exists():
        print(f"No {raw_path} found. Nothing to enrich.")
        summary.update(raw=0, candidates=0, rejected=0, review_queue=0, research_alerts=0)
        return summary

    # 2) Enrich.
    config_path = app_config
    if config_path is None and Path("config.json").exists():
        config_path = "config.json"
    config = load_config(config_path)

    with open(raw_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    candidates, skipped, review = enrich_candidates.enrich_rows(rows, config)
    enrich_candidates._write_csv(candidate_path, enrich_candidates.TEMPLATE_FIELDS, candidates)
    enrich_candidates._write_csv(review_path, enrich_candidates.REVIEW_FIELDS, review)
    print(f"== Enriched: {len(candidates)} candidates, "
          f"{len(skipped)} broken/rejected, {len(review)} in comp queue ==")

    # 3) Research alerts (no comps -> no buy decision).
    state = seen_state.load_state(state_path)
    already = set() if ignore_seen else seen_state.alerted_urls(state, "research")
    selected = notifier.select_research_items(review, config, already_alerted=already)
    messages = [notifier.format_research_alert(r) for r in selected]
    telegram = notifier.telegram_from_env()

    print(f"== {len(messages)} research alert(s) ==")
    notifier.notify(messages, telegram=telegram)
    if not ignore_seen and selected:
        seen_state.mark_alerted(state, "research", [r.get("url", "") for r in selected])
        seen_state.save_state(state, state_path)

    summary.update(
        raw=len(rows),
        candidates=len(candidates),
        rejected=len(skipped),
        review_queue=len(review),
        research_alerts=len(messages),
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the full local discovery scan (browse -> enrich -> research alerts).")
    ap.add_argument("--browser-config", default="browser_sources.json")
    ap.add_argument("--config", default=None, help="App config JSON (defaults to config.json)")
    ap.add_argument("--raw", default="raw_listings.csv")
    ap.add_argument("--candidates", default="candidate_listings.csv")
    ap.add_argument("--review-queue", default="comp_review_queue.csv")
    ap.add_argument("--state", default=seen_state.DEFAULT_STATE_PATH)
    ap.add_argument("--dry-run", action="store_true", help="Skip the browser; reuse existing raw_listings.csv")
    ap.add_argument("--ignore-seen", action="store_true", help="Alert on everything, ignore alerted state")
    args = ap.parse_args()

    summary = run_local_scan(
        browser_config=args.browser_config,
        app_config=args.config,
        raw_path=args.raw,
        candidate_path=args.candidates,
        review_path=args.review_queue,
        state_path=args.state,
        dry_run=args.dry_run,
        ignore_seen=args.ignore_seen,
    )
    print("\n== Summary ==")
    for key in ("raw", "candidates", "rejected", "review_queue", "research_alerts"):
        print(f"  {key}: {summary.get(key, 0)}")
    if summary.get("blocked"):
        print(f"  blocked: {summary['blocked'][1]} (on '{summary['blocked'][0]}')")


if __name__ == "__main__":
    main()
