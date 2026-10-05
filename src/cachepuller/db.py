"""SQLite state: access scores, files we promoted and an action history.

Scores decay exponentially, all at the same rate, so instead of decaying
every row over time each row stores

    lk = log2(score) + score_ts / half_life

which never changes while the file isn't accessed, and orders rows exactly
like their current score: score(now) = 2 ** (lk - now / half_life). With an
index on ``lk``, "hottest N files", "files with score >= x" and pruning are
index range scans, so the table can hold millions of files.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Created after the lk column exists (older databases get it added first).
INDEXES = """
CREATE INDEX IF NOT EXISTS files_lk ON files (lk);
CREATE INDEX IF NOT EXISTS files_share_lk ON files (share, lk);
CREATE INDEX IF NOT EXISTS files_last_access ON files (last_access);
CREATE INDEX IF NOT EXISTS files_hits ON files (hits);
"""

_SORTS = {
    "score": "lk",
    "hits": "hits",
    "last_access": "last_access",
}


@dataclass
class FileFilter:
    """Conditions the database can evaluate itself (no disk access)."""

    share: str = ""
    min_score: float | None = None  # same rounding as is_hot()
    since: float | None = None  # last_access >= since (a timestamp)
    text: str = ""  # case-insensitive substring of "share/relpath"
    shares: list[str] = field(default_factory=list)  # restrict to these shares


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


def is_hot(score: float, min_score: float) -> bool:
    """Compare at the precision scores are shown with, so a file that was just
    opened 3 times (score 2.9999...) counts as reaching a threshold of 3."""
    return round(score, 2) >= min_score


def _log2(x: float) -> float:
    return math.log2(max(x, 1e-300))


def decayed(score: float, score_ts: float, now: float, half_life: float) -> float:
    if now <= score_ts:
        return score
    return score * 0.5 ** ((now - score_ts) / half_life)


class Database:
    def __init__(self, path: str, half_life: float):
        self.half_life = half_life
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.create_function("log2", 1, _log2, deterministic=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-65536")  # 64 MiB page cache
        self._conn.executescript(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(files)")}
        if "lk" not in cols:
            self._conn.execute("ALTER TABLE files ADD COLUMN lk REAL NOT NULL DEFAULT 0")
            self._recompute_lk("upgrading the database")
        elif self._meta("lk_half_life") != repr(float(half_life)):
            self._recompute_lk("half-life changed")
        self._conn.executescript(INDEXES)

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _recompute_lk(self, why: str) -> None:
        started = time.monotonic()
        n = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if n > 100000:
            log.info("%s: recomputing scores of %d files, this takes a moment…", why, n)
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("UPDATE files SET lk = log2(score) + score_ts / ?", (self.half_life,))
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('lk_half_life', ?)", (repr(float(self.half_life)),)
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        if n > 100000:
            log.info("scores recomputed in %.0fs", time.monotonic() - started)

    def set_half_life(self, half_life: float) -> None:
        """Change the half-life (rewrites every row's sort key)."""
        if half_life == self.half_life:
            return
        with self._lock:
            self.half_life = half_life
            self._recompute_lk("half-life changed")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- access scores ---------------------------------------------------

    def _lk_at_least(self, score: float, now: float) -> float:
        return _log2(score) + now / self.half_life

    def _score(self, lk: float, now: float) -> float:
        return 2.0 ** (lk - now / self.half_life)

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
                    lk = _log2(score) + ts / self.half_life
                    cur.execute(
                        "INSERT INTO files (share, relpath, score, score_ts, hits, last_access, lk) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(share, relpath) DO UPDATE SET "
                        "score=excluded.score, score_ts=excluded.score_ts, hits=excluded.hits, "
                        "last_access=excluded.last_access, lk=excluded.lk",
                        (share, rel, score, ts, count, last, lk),
                    )
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    def _where(self, f: FileFilter | None, now: float) -> tuple[str, list]:
        if f is None:
            return "", []
        clauses, args = [], []
        if f.share:
            clauses.append("share = ?")
            args.append(f.share)
        if f.shares:
            clauses.append(f"share IN ({','.join('?' * len(f.shares))})")
            args.extend(f.shares)
        if f.min_score is not None:
            # is_hot() rounds to 2 decimals: score >= min - 0.005
            clauses.append("lk >= ?")
            args.append(self._lk_at_least(max(f.min_score - 0.005, 1e-9), now))
        if f.since is not None:
            clauses.append("last_access >= ?")
            args.append(f.since)
        if f.text:
            clauses.append("instr(lower(share || '/' || relpath), ?) > 0")
            args.append(f.text.lower())
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def query(
        self,
        f: FileFilter | None = None,
        sort: str = "score",
        descending: bool = True,
        limit: int | None = None,
        offset: int = 0,
        now: float | None = None,
    ) -> list[FileScore]:
        """Tracked files matching ``f``, sorted, one page at a time."""
        now = time.time() if now is None else now
        where, args = self._where(f, now)
        direction = "DESC" if descending else "ASC"
        if sort == "path":
            order = f"share {direction}, relpath {direction}"  # primary key order
        elif sort in _SORTS:
            # Tie-break on rowid in the same direction: each index already
            # holds (column, rowid) in that order, so SQLite can page through
            # it without sorting, even when millions of rows share a value.
            order = f"{_SORTS[sort]} {direction}, rowid {direction}"
        else:
            raise ValueError(f"unknown sort {sort!r}")
        sql = f"SELECT share, relpath, lk, hits, last_access FROM files{where} ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = [*args, limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [FileScore(s, r, self._score(lk, now), h, la) for s, r, lk, h, la in rows]

    def count(self, f: FileFilter | None = None, now: float | None = None) -> int:
        now = time.time() if now is None else now
        where, args = self._where(f, now)
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM files{where}", args).fetchone()[0]

    def hot(self, min_score: float, now: float | None = None) -> list[FileScore]:
        """Files with a score of at least ``min_score``, hottest first."""
        return self.query(FileFilter(min_score=min_score), now=now)

    def count_by_share(self, min_score: float | None = None, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        if min_score is None:
            sql, args = "SELECT share, COUNT(*) FROM files GROUP BY share", []
        else:
            # Without the hint SQLite walks the whole primary key to avoid a
            # sort for GROUP BY; the hot rows are a small range of files_lk.
            sql = "SELECT share, COUNT(*) FROM files INDEXED BY files_lk WHERE lk >= ? GROUP BY share"
            args = [self._lk_at_least(max(min_score - 0.005, 1e-9), now)]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return dict(rows)

    def tracked_shares(self) -> list[str]:
        """Distinct shares, found by skipping through the primary key index."""
        with self._lock:
            rows = self._conn.execute(
                "WITH RECURSIVE s(x) AS ("
                " SELECT min(share) FROM files"
                " UNION ALL SELECT (SELECT min(share) FROM files WHERE share > x) FROM s WHERE x IS NOT NULL"
                ") SELECT x FROM s WHERE x IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows]

    def relpaths(self, share: str, batch: int = 5000):
        """Yield every tracked relpath of a share, in batches (keyset paging)."""
        last = ""
        while True:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT relpath FROM files WHERE share=? AND relpath > ? ORDER BY relpath LIMIT ?",
                    (share, last, batch),
                ).fetchall()
            if not rows:
                return
            yield [r[0] for r in rows]
            last = rows[-1][0]

    def scores(self, share: str | None = None, now: float | None = None) -> list[FileScore]:
        """All tracked files with their current score, hottest first.

        Loads everything: for tests and small tables. Use query() otherwise.
        """
        return self.query(FileFilter(share=share or ""), now=now)

    def score_of(self, share: str, relpath: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT lk FROM files WHERE share=? AND relpath=?", (share, relpath)
            ).fetchone()
        return self._score(row[0], now) if row else 0.0

    def prune(self, min_score: float = 0.01, now: float | None = None) -> int:
        """Forget files whose score has decayed to (almost) nothing."""
        now = time.time() if now is None else now
        cutoff = self._lk_at_least(min_score, now)
        n = 0
        while True:
            # Small chunks, releasing the lock in between, so a large prune
            # doesn't stall the tracker or the web UI.
            with self._lock:
                deleted = self._conn.execute(
                    "DELETE FROM files WHERE rowid IN (SELECT rowid FROM files WHERE lk < ? LIMIT 5000)",
                    (cutoff,),
                ).rowcount
            n += deleted
            if deleted < 5000:
                break
            time.sleep(0.02)  # Python locks aren't fair: let waiting threads in
        with self._lock:
            self._conn.execute("DELETE FROM history WHERE ts < ?", (now - 90 * 86400,))
        return n

    # -- staging files found missing (keeps memory flat for huge shares) ----

    def missing_reset(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS missing (share TEXT NOT NULL, relpath TEXT NOT NULL, "
                "PRIMARY KEY (share, relpath))"
            )
            self._conn.execute("DELETE FROM temp.missing")

    def missing_add(self, share: str, relpaths: list[str]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO temp.missing VALUES (?, ?)", [(share, r) for r in relpaths]
            )

    def missing_examples(self, share: str, n: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT relpath FROM temp.missing WHERE share=? LIMIT ?", (share, n)
            ).fetchall()
        return [r[0] for r in rows]

    def missing_commit(self, share: str) -> int:
        """Forget the staged missing files of a share. Returns how many."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                n = cur.execute(
                    "DELETE FROM files WHERE share=? AND relpath IN "
                    "(SELECT relpath FROM temp.missing WHERE share=?)", (share, share)
                ).rowcount
                cur.execute(
                    "DELETE FROM promoted WHERE share=? AND relpath IN "
                    "(SELECT relpath FROM temp.missing WHERE share=?)", (share, share)
                )
                cur.execute("DELETE FROM temp.missing WHERE share=?", (share,))
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
        return n

    def missing_discard(self, share: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM temp.missing WHERE share=?", (share,))

    def is_tracked(self, share: str, relpath: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM files WHERE share=? AND relpath=?", (share, relpath)
            ).fetchone()
        return row is not None

    def forget(self, keys: list[tuple[str, str]]) -> None:
        """Remove files from the score list and the promoted list."""
        if not keys:
            return
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.executemany("DELETE FROM files WHERE share=? AND relpath=?", keys)
                cur.executemany("DELETE FROM promoted WHERE share=? AND relpath=?", keys)
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

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
