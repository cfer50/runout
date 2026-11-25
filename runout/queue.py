"""A tiny on-disk queue so scrobbles survive Wi-Fi dropouts and reboots.

If a target is unreachable, the scrobble is stored in SQLite and retried
later. Now-playing updates aren't queued; they're useless after the fact.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from runout.models import Track
from runout.targets import Target, TargetError

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    track TEXT NOT NULL,
    listened_at INTEGER NOT NULL,
    created REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS recent (
    track_key TEXT NOT NULL,
    listened_at INTEGER NOT NULL
);
"""

# Two scrobbles of the same song whose start times are this close are the same play
# (e.g. runout restarted mid-song). A genuine replay starts at least a song-length later.
DUPLICATE_WINDOW_SECONDS = 60


class ScrobbleQueue:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(_SCHEMA)

    def put(self, target: str, track: Track, listened_at: int, error: str = "") -> None:
        data = dataclasses.asdict(track)
        data.pop("extra", None)
        self.db.execute(
            "INSERT INTO pending (target, track, listened_at, created, attempts, last_error) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (target, json.dumps(data), listened_at, time.time(), error),
        )
        self.db.commit()

    def seen_recently(self, track: Track, listened_at: int) -> bool:
        """Record this scrobble; return True if it duplicates one already sent."""
        key = track.key
        dup = self.db.execute(
            "SELECT 1 FROM recent WHERE track_key = ? AND ABS(listened_at - ?) < ?",
            (key, listened_at, DUPLICATE_WINDOW_SECONDS),
        ).fetchone()
        if not dup:
            self.db.execute("INSERT INTO recent VALUES (?, ?)", (key, listened_at))
            self.db.execute("DELETE FROM recent WHERE listened_at < ?", (listened_at - 86400,))
            self.db.commit()
        return dup is not None

    def count(self, target: str | None = None) -> int:
        if target:
            sql, args = "SELECT COUNT(*) FROM pending WHERE target = ?", (target,)
        else:
            sql, args = "SELECT COUNT(*) FROM pending", ()
        return self.db.execute(sql, args).fetchone()[0]

    def items(self) -> list[tuple[int, str, Track, int, int, str | None]]:
        rows = self.db.execute(
            "SELECT id, target, track, listened_at, attempts, last_error FROM pending ORDER BY id"
        ).fetchall()
        return [(i, t, Track(**json.loads(tr)), ts, a, e) for i, t, tr, ts, a, e in rows]

    async def flush(self, targets: Iterable[Target]) -> int:
        """Retry everything queued. Returns how many were delivered."""
        by_name = {t.name: t for t in targets}
        delivered = 0
        blocked: set[str] = set()  # targets that failed this round; don't hammer them
        for row_id, name, track, listened_at, _attempts, _ in self.items():
            target = by_name.get(name)
            if target is None:
                log.warning("dropping queued scrobble for unknown target %r", name)
                self._delete(row_id)
                continue
            if name in blocked:
                continue
            try:
                await target.scrobble(track, listened_at)
            except TargetError as e:
                if e.retryable:
                    blocked.add(name)
                    self.db.execute(
                        "UPDATE pending SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                        (str(e), row_id),
                    )
                    self.db.commit()
                else:
                    log.error(
                        "giving up on queued scrobble %s - %s: %s", track.artist, track.title, e
                    )
                    self._delete(row_id)
                continue
            self._delete(row_id)
            delivered += 1
        if delivered:
            log.info("delivered %d queued scrobble(s)", delivered)
        return delivered

    def _delete(self, row_id: int) -> None:
        self.db.execute("DELETE FROM pending WHERE id = ?", (row_id,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()
