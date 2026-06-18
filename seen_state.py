#!/usr/bin/env python3
"""
Saved-state diff for the Lithuania Flip Scanner (v0.5).

Remembers which listing URLs the collector has already seen, in a small JSON
file (default ``.seen_listings.json``). Each collector run classifies the URLs
it observed as:

- new     : never seen before  -> eligible for alerting
- seen    : seen on a previous run too
- removed : known previously but not seen this run (expired / sold / pulled)

Only the "new" URLs are written to ``last_run.new_urls`` so the notifier can
alert on fresh listings only and never re-spam old ones.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

DEFAULT_STATE_PATH = ".seen_listings.json"


def load_state(path: str = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    p = Path(path)
    data: Dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    data.setdefault("listings", {})
    data.setdefault("last_run", {})
    return data


def diff_and_update(
    state: Dict[str, Any],
    current_urls: Iterable[str],
    now: str | None = None,
) -> Tuple[List[str], List[str], List[str]]:
    """Update ``state`` in place and return (new, seen, removed) URL lists."""
    now = now or datetime.now().isoformat(timespec="seconds")
    listings: Dict[str, Any] = state.setdefault("listings", {})

    current: Set[str] = {u for u in current_urls if u}
    known: Set[str] = set(listings.keys())

    new = sorted(current - known)
    seen = sorted(current & known)
    removed = sorted(known - current)

    for url in current:
        if url in listings:
            listings[url]["last_seen"] = now
        else:
            listings[url] = {"first_seen": now, "last_seen": now}

    state["last_run"] = {
        "timestamp": now,
        "new_urls": new,
        "seen_count": len(seen),
        "removed_urls": removed,
    }
    return new, seen, removed


def save_state(state: Dict[str, Any], path: str = DEFAULT_STATE_PATH) -> None:
    Path(path).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def new_urls(state: Dict[str, Any]) -> Set[str]:
    """URLs that were new on the most recent collector run."""
    return set(state.get("last_run", {}).get("new_urls", []))
