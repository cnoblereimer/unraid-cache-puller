"""The indexed score key (lk) and the queries built on it."""

import random
import sqlite3
import time

import pytest

from cachepuller.db import Database, FileFilter, decayed, is_hot

H = 3600.0


def make(tmp_path, half_life=72 * H):
    return Database(str(tmp_path / "db.sqlite"), half_life)


def fill(db, n=300, seed=1, now=None):
    now = now or time.time()
    rnd = random.Random(seed)
    hits = {}
    for i in range(n):
        stamps = [now - rnd.random() * 20 * 86400 for _ in range(rnd.randint(1, 6))]
        hits[(rnd.choice(["media", "photos", "games"]), f"dir{i % 7}/f{i:04d}.bin")] = stamps
    db.record_hits(hits)
    return hits


def reference_scores(hits, now, half_life):
    out = {}
    for key, stamps in hits.items():
        score, ts = 0.0, 0.0
        for t in sorted(stamps):
            score = decayed(score, ts, t, half_life) + 1.0
            ts = max(ts, t)
        out[key] = decayed(score, ts, now, half_life)
    return out


def test_scores_match_plain_exponential_decay(tmp_path):
    db = make(tmp_path)
    now = time.time()
    hits = fill(db, now=now)
    ref = reference_scores(hits, now + 5 * 86400, db.half_life)
    got = {(f.share, f.relpath): f.score for f in db.query(now=now + 5 * 86400)}
    assert got.keys() == ref.keys()
    for k in ref:
        assert got[k] == pytest.approx(ref[k], rel=1e-9)
    # Sorted by score, highest first.
    scores = [f.score for f in db.query(now=now)]
    assert scores == sorted(scores, reverse=True)


def test_hot_threshold_matches_is_hot(tmp_path):
    db = make(tmp_path)
    now = time.time()
    fill(db, now=now)
    for min_score in (0.5, 1, 2, 3, 4.5):
        expected = {(f.share, f.relpath) for f in db.query(now=now) if is_hot(f.score, min_score)}
        assert {(f.share, f.relpath) for f in db.hot(min_score, now)} == expected
        assert db.count(FileFilter(min_score=min_score), now) == len(expected)
        by_share = db.count_by_share(min_score, now)
        assert sum(by_share.values()) == len(expected)


def test_just_opened_three_times_counts_as_score_3(tmp_path):
    db = make(tmp_path)
    now = time.time()
    db.record_hits({("media", "a.mkv"): [now - 2, now - 1, now]})
    assert [f.relpath for f in db.hot(3, now + 1)] == ["a.mkv"]


def test_upgrades_old_database(tmp_path):
    path = str(tmp_path / "old.sqlite")
    now = time.time()
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE files (share TEXT NOT NULL, relpath TEXT NOT NULL, score REAL NOT NULL, "
        "score_ts REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0, last_access REAL NOT NULL, "
        "PRIMARY KEY (share, relpath));"
    )
    c.executemany("INSERT INTO files VALUES (?,?,?,?,?,?)", [
        ("media", "hot.mkv", 4.0, now - 3600, 4, now - 3600),
        ("media", "cold.mkv", 1.0, now - 30 * 86400, 1, now - 30 * 86400),
    ])
    c.commit()
    c.close()
    db = Database(path, 72 * H)
    got = {f.relpath: f.score for f in db.query(now=now)}
    assert got["hot.mkv"] == pytest.approx(decayed(4.0, now - 3600, now, 72 * H))
    assert got["cold.mkv"] == pytest.approx(decayed(1.0, now - 30 * 86400, now, 72 * H))
    assert [f.relpath for f in db.hot(3, now)] == ["hot.mkv"]
    db.close()


def test_half_life_change_recomputes(tmp_path):
    # Decay between a file's past accesses was already applied with the old
    # half-life; a change applies from then on. With one access per file the
    # expected score is exact.
    db = make(tmp_path)
    now = time.time()
    rnd = random.Random(3)
    hits = {("media", f"f{i}"): [now - rnd.random() * 10 * 86400] for i in range(50)}
    db.record_hits(hits)
    db.set_half_life(24 * H)
    ref = reference_scores(hits, now, 24 * H)
    for f in db.query(now=now):
        assert f.score == pytest.approx(ref[(f.share, f.relpath)], rel=1e-9)
    db.close()
    # Reopening with yet another half-life (e.g. changed env var) recomputes too.
    db = Database(str(tmp_path / "db.sqlite"), 12 * H)
    ref = reference_scores(hits, now, 12 * H)
    for f in db.query(now=now):
        assert f.score == pytest.approx(ref[(f.share, f.relpath)], rel=1e-9)


def test_sorting_and_paging_is_stable(tmp_path):
    db = make(tmp_path)
    now = time.time()
    # Many ties: every file has exactly one access at the same time.
    db.record_hits({("media", f"f{i:03d}"): [now] for i in range(120)})
    for sort in ("score", "hits", "last_access", "path"):
        for desc in (True, False):
            pages = [db.query(sort=sort, descending=desc, limit=25, offset=o, now=now) for o in range(0, 120, 25)]
            seen = [f.relpath for p in pages for f in p]
            assert len(seen) == 120 and len(set(seen)) == 120, (sort, desc)
    assert [f.relpath for f in db.query(sort="path", descending=False, limit=2, now=now)] == ["f000", "f001"]


def test_filters(tmp_path):
    db = make(tmp_path)
    now = time.time()
    db.record_hits({
        ("media", "TV/Show/e1.mkv"): [now - 60],
        ("media", "Movies/x.mkv"): [now - 10 * 86400],
        ("photos", "2024/img.jpg"): [now - 3600],
    })
    assert db.count(FileFilter(share="media"), now) == 2
    assert db.count(FileFilter(since=now - 86400), now) == 2
    assert [f.relpath for f in db.query(FileFilter(text="tv/SHOW"), now=now)] == ["TV/Show/e1.mkv"]
    assert [f.relpath for f in db.query(FileFilter(text="photos/2024"), now=now)] == ["2024/img.jpg"]
    assert db.tracked_shares() == ["media", "photos"]


def test_prune_in_chunks(tmp_path):
    db = make(tmp_path, half_life=H)
    now = time.time()
    db.record_hits({("media", f"old{i}"): [now - 30 * H] for i in range(12000)})
    db.record_hits({("media", "new"): [now]})
    assert db.prune(now=now) == 12000
    assert [f.relpath for f in db.query(now=now)] == ["new"]


def test_relpaths_in_batches(tmp_path):
    db = make(tmp_path)
    now = time.time()
    db.record_hits({("media", f"f{i:05d}"): [now] for i in range(1234)})
    db.record_hits({("other", "x"): [now]})
    batches = list(db.relpaths("media", batch=500))
    assert [len(b) for b in batches] == [500, 500, 234]
    assert sum(batches, []) == sorted(f"f{i:05d}" for i in range(1234))
