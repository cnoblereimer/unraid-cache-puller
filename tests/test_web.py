import json
import os
import time
import urllib.error
import urllib.request

import pytest

from cachepuller.daemon import Daemon
from cachepuller.service import PoolUsage, Service
from cachepuller.settings import SettingsStore
from cachepuller.web import Api, start_web
from conftest import FakeOpenChecker, write

GiB = 1024**3


@pytest.fixture
def daemon(unraid, db):
    base_env = {
        "MNT_ROOT": unraid.mnt_root, "SHARES_CFG_DIR": unraid.shares_cfg_dir,
        "EMHTTP_DIR": unraid.emhttp_dir, "CONFIG_DIR": unraid.config_dir,
        "DRY_RUN": "false", "MIN_FILE_AGE_MINUTES": "0", "CACHE_MIN_FREE": "0", "MIN_SCORE": "2",
        "MOVER_PID_FILES": unraid.mover_pid_files[0], "MOVER_IGNORE_FILE": unraid.mover_ignore_file,
        "REQUIRE_MOUNTS": "false",
    }
    store = SettingsStore(os.path.join(unraid.config_dir, "settings.json"), base_env=base_env)
    usage = lambda path: PoolUsage(total=100 * GiB, used=10 * GiB, avail=90 * GiB)
    svc = Service(unraid, db, open_checker=FakeOpenChecker(), require_mounts=False, usage_fn=usage)
    d = Daemon(unraid, db, store, service=svc)
    return d


def hot(db, share, rel, hits=3):
    now = time.time()
    db.record_hits({(share, rel): [now - i for i in range(hits)]})


def test_overview(daemon, unraid):
    o = Api(daemon).overview()
    assert all(g["ok"] for g in o["gates"])
    shares = {s["name"]: s["status"] for s in o["shares"]}
    assert shares == {"media": "managed", "games": "managed", "backup": "not applicable",
                      "appdata": "not applicable", "p2p": "not applicable"}
    assert [p["name"] for p in o["pools"]] == ["cache"]


def test_files_states(daemon, unraid, db):
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/hot.mkv")
    write(f"{mnt}/disk1/media/cold.mkv")
    write(f"{mnt}/cache/media/onpool.mkv")
    hot(db, "media", "hot.mkv", 3)
    hot(db, "media", "cold.mkv", 1)
    hot(db, "media", "onpool.mkv", 5)
    hot(db, "media", "gone.mkv", 4)
    rows = {r["relpath"]: r["state"] for r in Api(daemon).files()["rows"]}
    assert rows == {"hot.mkv": "candidate", "cold.mkv": "cold", "onpool.mkv": "pool", "gone.mkv": "missing"}
    assert [r["relpath"] for r in Api(daemon).files(q="COLD")["rows"]] == ["cold.mkv"]
    assert len(Api(daemon).files(hot_only=True)["rows"]) == 3


def test_settings_roundtrip_and_validation(daemon):
    api = Api(daemon)
    data = api.save_settings({"changes": {"DRY_RUN": False, "MIN_SCORE": 5, "CACHE_MIN_FREE": "20G",
                                          "EXCLUDE_PATTERNS": ["*.part", "tmp/*"]}})
    values = {f["key"]: f["value"] for f in data["fields"]}
    assert values["DRY_RUN"] is False and values["MIN_SCORE"] == 5
    assert values["CACHE_MIN_FREE"] == "20G"
    assert daemon.cfg.exclude_patterns == ["*.part", "tmp/*"]
    assert daemon.service.cfg is daemon.cfg
    with pytest.raises(ValueError):
        api.save_settings({"changes": {"CACHE_MAX_PERCENT": 150}})
    with pytest.raises(ValueError):
        api.save_settings({"changes": {"MNT_ROOT": "/"}})  # not editable
    assert daemon.cfg.cache_max_percent == 80  # unchanged after a failed save
    api.reset_settings()
    assert not os.path.exists(daemon.store.path)


def test_promote_and_exclude(daemon, unraid, db):
    mnt = unraid.mnt_root
    write(f"{mnt}/disk1/media/[x] a.mkv")
    hot(db, "media", "[x] a.mkv", 1)  # below MIN_SCORE: manual move still allowed
    api = Api(daemon)
    res = api.promote({"share": "media", "relpath": "[x] a.mkv"})
    assert res["ok"], res
    assert os.path.exists(f"{mnt}/cache/media/[x] a.mkv")

    write(f"{mnt}/disk1/media/[y] b.mkv")
    hot(db, "media", "[y] b.mkv", 5)
    api.exclude({"share": "media", "relpath": "[y] b.mkv"})
    row = next(r for r in api.files()["rows"] if r["relpath"] == "[y] b.mkv")
    assert row["state"] == "blocked" and "exclude" in row["reason"]
    with pytest.raises(ValueError):
        api.promote({"share": "media", "relpath": "../etc/passwd"})


def test_promote_respects_gates(daemon, unraid, db):
    write(f"{unraid.mnt_root}/disk1/media/a.mkv")
    with open(f"{unraid.emhttp_dir}/var.ini", "w") as fh:
        fh.write('mdState="STOPPED"\n')
    res = Api(daemon).promote({"share": "media", "relpath": "a.mkv"})
    assert not res["ok"] and "not safe" in res["message"]


def _req(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_server(daemon):
    server = start_web(daemon, "127.0.0.1", 0, None)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = _req(base + "/")
        assert status == 200 and b"Cache Puller" in body
        assert _req(base + "/app.js")[0] == 200
        status, body = _req(base + "/api/overview")
        assert status == 200 and "gates" in json.loads(body)
        # POST without JSON content type is refused (CSRF protection)
        assert _req(base + "/api/run", b"{}", {"Content-Type": "text/plain"})[0] == 415
        # Cross-origin POST is refused
        assert _req(base + "/api/run", b"{}", {"Content-Type": "application/json",
                                               "Origin": "http://evil.example"})[0] == 403
        assert _req(base + "/api/run", b"{}", {"Content-Type": "application/json"})[0] == 200
        assert _req(base + "/nope")[0] == 404
    finally:
        server.shutdown()


def test_http_password(daemon):
    server = start_web(daemon, "127.0.0.1", 0, "s3cret")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _req(base + "/api/overview")[0] == 401
        import base64
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
        assert _req(base + "/api/overview", headers=auth)[0] == 200
        bad = {"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()}
        assert _req(base + "/api/overview", headers=bad)[0] == 401
    finally:
        server.shutdown()


def test_forget_endpoint(daemon, unraid, db):
    write(f"{unraid.mnt_root}/disk1/media/here.mkv")
    hot(db, "media", "here.mkv", 1)
    hot(db, "media", "gone.mkv", 1)
    api = Api(daemon)
    assert not api.forget({"share": "media", "relpath": "here.mkv"})["ok"]
    assert api.forget({"share": "media", "relpath": "gone.mkv"})["ok"]
    assert [r["relpath"] for r in api.files()["rows"]] == ["here.mkv"]
    assert "cleanup" in api.overview()
