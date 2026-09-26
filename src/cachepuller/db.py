"""SQLite state: access scores, files we promoted and an action history."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    share       TEXT NOT NULL,
    relpath     TEXT NOT NULL,
    score       REAL NOT NULL,
    score_ts    REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_access REAL NOT NULL,
    PRIMARY KEY (share, relpath)
);
CREATE TABLE IF NOT EXISTS promoted (
    share       TEXT NOT NULL,
    relpath     TEXT NOT NULL,
    pool        TEXT NOT NULL,
    origin_disk TEXT NOT NULL,
    size        INTEGER NOT NULL,
    promoted_at REAL NOT NULL,
    PRIMARY KEY (share, relpath)
);
CREATE TABLE IF NOT EXISTS history (
    ts      REAL NOT NULL,
    action  TEXT NOT NULL,
    share   TEXT NOT NULL,
    relpath TEXT NOT NULL,
    src     TEXT,
    dst     TEXT,
    size    INTEGER,
    result  TEXT NOT NULL,
    message TEXT
);
CREATE INDEX IF NOT EXISTS history_ts ON history (ts);
"""


@dataclass
class FileScore:
    share: str
    relpath: str
    score: float
    hits: int
    last_access: float


@dataclass
class Promoted:
    share: str
    relpath: str
    pool: str
    origin_disk: str
    size: int
    promoted_at: float


def decayed(score: float, score_ts: float, now: float, half_life: float) -> float:
    if now <= score_ts:
        return score
    return score * 0.5 ** ((now - score_ts) / half_life)


class Database:
    def __init__(self, path: str, half_life: float):
        self.half_life = half_life
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- access scores ---------------------------------------------------

    def record_hits(self, hits: dict[tuple[str, str], list[float]]) -> None:
        """Add access timestamps, applying exponential decay to the score."""
        if not hits:
            return
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                for (share, rel), stamps in hits.items():
                    row = cur.execute(
                        "SELECT score, score_ts, hits, last_access FROM files WHERE share=? AND relpath=?",
                        (share, rel),
                    ).fetchone()
                    score, ts, count, last = row if row else (0.0, 0.0, 0, 0.0)
                    for t in sorted(stamps):
                        score = decayed(score, ts, t, self.half_life) + 1.0
                        ts = max(ts, t)
                        count += 1
                        last = max(last, t)
                    cur.execute(
                        "INSERT INTO files (share, relpath, score, score_ts, hits, last_access) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(share, relpath) DO UPDATE SET "
                        "score=excluded.score, score_ts=excluded.score_ts, hits=excluded.hits, "
                        "last_access=excluded.last_access",
                        (share, rel, score, ts, count, last),
                    )
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    def scores(self, share: str | None = None, now: float | None = None) -> list[FileScore]:
        """All tracked files with their current (decayed) score, hottest first."""
        now = time.time() if now is None else now
        with self._lock:
            if share is None:
                rows = self._conn.execute(
                    "SELECT share, relpath, score, score_ts, hits, last_access FROM files"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT share, relpath, score, score_ts, hits, last_access FROM files WHERE share=?",
                    (share,),
                ).fetchall()
        out = [
            FileScore(s, r, decayed(sc, ts, now, self.half_life), h, la)
            for s, r, sc, ts, h, la in rows
        ]
        out.sort(key=lambda f: f.score, reverse=True)
        return out

    def score_of(self, share: str, relpath: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT score, score_ts FROM files WHERE share=? AND relpath=?", (share, relpath)
            ).fetchone()
        return decayed(row[0], row[1], now, self.half_life) if row else 0.0

    def prune(self, min_score: float = 0.01, now: float | None = None) -> int:
        """Forget files whose score has decayed to (almost) nothing."""
        now = time.time() if now is None else now
        stale = [
            (f.share, f.relpath) for f in self.scores(now=now) if f.score < min_score
        ]
        with self._lock:
            self._conn.executemany("DELETE FROM files WHERE share=? AND relpath=?", stale)
            self._conn.execute("DELETE FROM history WHERE ts < ?", (now - 90 * 86400,))
        return len(stale)

    def rename(self, share: str, old: str, new: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE OR REPLACE files SET relpath=? WHERE share=? AND relpath=?", (new, share, old)
            )
            self._conn.execute(
                "UPDATE OR REPLACE promoted SET relpath=? WHERE share=? AND relpath=?", (new, share, old)
            )

    # -- promoted files ----------------------------------------------------

    def add_promoted(self, p: Promoted) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO promoted VALUES (?, ?, ?, ?, ?, ?)",
                (p.share, p.relpath, p.pool, p.origin_disk, p.size, p.promoted_at),
            )

    def remove_promoted(self, share: str, relpath: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM promoted WHERE share=? AND relpath=?", (share, relpath))

    def promoted(self) -> list[Promoted]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT share, relpath, pool, origin_disk, size, promoted_at FROM promoted"
            ).fetchall()
        return [Promoted(*r) for r in rows]

    # -- history -----------------------------------------------------------

    def log_action(
        self,
        action: str,
        share: str,
        relpath: str,
        src: str | None,
        dst: str | None,
        size: int | None,
        result: str,
        message: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), action, share, relpath, src, dst, size, result, message),
            )

    def history(self, limit: int = 50) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT ts, action, share, relpath, size, result, message FROM history "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
