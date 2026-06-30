"""Post-build Spring Application Event resolver.

Connects publishEvent() call sites to @EventListener methods by matching
on the event class name.

Resolution chain:
    publisher_method  →(PUBLISHES)→  event:XxxEvent
    listener_method   →(HANDLES)→   event:XxxEvent
    ⟹ emit CALLS edge: publisher_method → listener_method
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore

logger = logging.getLogger(__name__)


def resolve_spring_events(store: GraphStore) -> dict:
    """Emit CALLS edges from event publishers to matching @EventListener methods.

    Safe to call multiple times — already-resolved edges are skipped via
    extra.event_resolved flag.

    Returns a dict with resolution counts for telemetry.
    """
    conn = store._conn

    # Only process Java files
    java_files: set[str] = {
        row["file_path"]
        for row in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE language = 'java'"
        ).fetchall()
    }
    if not java_files:
        return {"files_indexed": 0, "calls_emitted": 0}

    # Build: event_type → [listener_method_qualified]
    listeners: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT source_qualified, extra FROM edges WHERE kind = 'HANDLES'"
    ).fetchall():
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}
        event_type = extra.get("event_type")
        if event_type:
            listeners.setdefault(event_type, []).append(row["source_qualified"])

    if not listeners:
        logger.info("Event resolver: no HANDLES edges found, skipping")
        return {"files_indexed": len(java_files), "calls_emitted": 0}

    # Collect PUBLISHES edges and emit CALLS for each matching listener
    publishes_rows = conn.execute(
        "SELECT id, source_qualified, extra, file_path FROM edges WHERE kind = 'PUBLISHES'"
    ).fetchall()

    emitted = 0
    new_edges: list[tuple] = []

    for row in publishes_rows:
        if row["file_path"] not in java_files:
            continue
        try:
            extra = json.loads(row["extra"] or "{}")
        except (json.JSONDecodeError, TypeError):
            extra = {}

        if extra.get("event_resolved"):
            continue

        event_type = extra.get("event_type")
        if not event_type:
            continue

        matching_listeners = listeners.get(event_type, [])
        for listener_qual in matching_listeners:
            call_extra = json.dumps({
                "event_resolved": True,
                "event_type": event_type,
            })
            new_edges.append((
                "CALLS",
                row["source_qualified"],
                listener_qual,
                row["source_qualified"],
                listener_qual,
                row["file_path"],
                call_extra,
            ))
            emitted += 1
            logger.debug(
                "Event resolved: %s →[%s]→ %s",
                row["source_qualified"], event_type, listener_qual,
            )

        # Mark PUBLISHES edge as processed
        extra["event_resolved"] = True
        conn.execute(
            "UPDATE edges SET extra = ? WHERE id = ?",
            (json.dumps(extra), row["id"]),
        )

    if new_edges:
        conn.executemany(
            "INSERT OR IGNORE INTO edges "
            "(kind, source_qualified, target_qualified, file_path, extra) "
            "VALUES (?, ?, ?, ?, ?)",
            [(e[0], e[1], e[2], e[5], e[6]) for e in new_edges],
        )
        conn.commit()

    logger.info(
        "Spring event resolver: emitted %d CALLS edges in %d Java files",
        emitted, len(java_files),
    )
    return {"files_indexed": len(java_files), "calls_emitted": emitted}
