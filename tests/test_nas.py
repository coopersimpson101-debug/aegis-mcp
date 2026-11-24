from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from mcp import Client

from aegis_mcp.nas import build_server
from aegis_mcp.nas.appliance import Synology, TrueNAS
from aegis_mcp.nas.smart import SmartError, parse_smartctl, validate_device
from aegis_mcp.sim.http_fake import FakeApp, FakeRequest

from .conftest import call

FIXTURES = Path(__file__).parent / "fixtures"


def fake_smartctl(args: list[str]) -> str:
    if args[:1] == ["--scan"]:
        return json.dumps({"devices": [{"name": "/dev/sda", "type": "sat", "info_name": "/dev/sda [SAT]"},
                                       {"name": "/dev/nvme0", "type": "nvme", "info_name": "/dev/nvme0"}]})
    dev = args[-1]
    return (FIXTURES / ("smart_sda_warning.json" if dev == "/dev/sda" else "smart_nvme_ok.json")).read_text()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    media = tmp_path / "media"
    (media / "movies").mkdir(parents=True)
    (media / "movies" / "Big Buck Bunny.mkv").write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 100)
    (media / "notes.txt").write_bytes(b"hello nas\nline two\n")
    (media / ".hidden").write_text("x")
    (media / ".ssh").mkdir()
    (media / ".ssh" / "id_ed25519").write_text("SECRET")
    docs = tmp_path / "docs"
    (docs / "2024").mkdir(parents=True)
    (docs / "2024" / "invoice-001.pdf").write_bytes(b"%PDF-1.7")
    (docs / "2024" / "Invoice-002.PDF").write_bytes(b"%PDF-1.7")
    (tmp_path / "outside.txt").write_text("you should never read me")
    return tmp_path


@pytest.fixture
def server(tree: Path, make_guard):
    cfg = {"nas": {"roots": {"media": str(tree / "media"), "docs": str(tree / "docs")}, "writable_roots": ["docs"]}}
    return build_server(cfg, guard=make_guard("nas"), smart_runner=fake_smartctl)


async def test_list_stat_read(server):
    async with Client(server) as c:
        roots = await call(c, "nas_list_roots")
        assert {r["root"]: r["writable"] for r in roots["result"]} == {"media": False, "docs": True}
        ls = await call(c, "nas_list", {"path": "media"})
        names = [e["name"] for e in ls["result"]["entries"]]
        assert names == ["movies", "notes.txt"]  # dirs first, hidden + denied hidden
        st = await call(c, "nas_stat", {"path": "media/notes.txt"})
        assert st["result"]["size"] == len("hello nas\nline two\n") and st["result"]["type"] == "file"
        rd = await call(c, "nas_read", {"path": "media/notes.txt", "offset": 6, "max_bytes": 3})
        assert rd["result"]["content"] == "nas" and rd["result"]["encoding"] == "utf-8" and not rd["result"]["eof"]
        binr = await call(c, "nas_read", {"path": "media/movies/Big Buck Bunny.mkv", "max_bytes": 4})
        assert binr["result"]["encoding"] == "base64" and base64.b64decode(binr["result"]["content"]) == b"\x1aE\xdf\xa3"


async def test_search_and_disk_usage(server):
    async with Client(server) as c:
        s = await call(c, "nas_search", {"path": "docs", "pattern": "*invoice*"})
        assert sorted(m["path"] for m in s["result"]["matches"]) == ["docs/2024/Invoice-002.PDF", "docs/2024/invoice-001.pdf"]
        s2 = await call(c, "nas_search", {"path": "media", "pattern": "id_*"})
        assert s2["result"]["matches"] == []  # .ssh is denied
        du = await call(c, "nas_disk_usage", {"path": "docs", "measure_directory": True})
        assert du["result"]["total_bytes"] > 0 and du["result"]["directory_files"] == 2
        assert du["result"]["directory_bytes"] == 16


@pytest.mark.parametrize("evil", [
    "media/../outside.txt", "../outside.txt", "media/movies/../../outside.txt", "/etc/passwd",
    "C:/Windows/win.ini", "\\\\server\\share\\x", "media\\..\\outside.txt", "nope/file", "media/.ssh/id_ed25519",
    "media/x\x00.txt",
])
async def test_path_traversal_rejected(server, evil):
    async with Client(server) as c:
        r = await call(c, "nas_read", {"path": evil})
        assert r["status"] == "blocked", r


async def test_symlink_escape_rejected(server, tree):
    link = tree / "media" / "escape"
    try:
        os.symlink(tree / "outside.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform/user")
    async with Client(server) as c:
        r = await call(c, "nas_read", {"path": "media/escape"})
        assert r["status"] == "blocked" and "escapes" in r["reason"]


async def test_delete_needs_confirmation_and_uses_trash(server, tree, notes):
    async with Client(server) as c:
        ro = await call(c, "nas_delete", {"path": "media/notes.txt"})
        assert ro["status"] == "blocked" and "read-only" in ro["reason"]
        pend = await call(c, "nas_delete", {"path": "docs/2024/invoice-001.pdf"})
        assert pend["status"] == "confirmation_required"
        assert (tree / "docs" / "2024" / "invoice-001.pdf").exists()
        # token for one file cannot delete another
        wrong = await call(c, "nas_delete", {"path": "docs/2024/Invoice-002.PDF", "confirm_token": notes.codes[pend["request_id"]]})
        assert wrong["status"] == "confirmation_required"
        ok = await call(c, "nas_delete", {"path": "docs/2024/invoice-001.pdf", "confirm_token": notes.codes[pend["request_id"]]})
        assert ok["status"] == "ok" and not (tree / "docs" / "2024" / "invoice-001.pdf").exists()
        trashed = list((tree / "docs" / ".aegis-trash").rglob("invoice-001.pdf"))
        assert len(trashed) == 1
        # trash is invisible and unreadable through the API
        assert ".aegis-trash" not in [e["name"] for e in (await call(c, "nas_list", {"path": "docs", "include_hidden": True}))["result"]["entries"]]
        assert (await call(c, "nas_read", {"path": ok["result"]["trash"]}))["status"] == "blocked"
        root = await call(c, "nas_delete", {"path": "docs"})
        assert root["status"] == "blocked"
        perm = await call(c, "nas_delete", {"path": "docs/2024", "permanent": True})
        assert perm["status"] == "confirmation_required" and perm["risk"] == "critical"


async def test_smart(server):
    async with Client(server) as c:
        scan = await call(c, "nas_smart_scan")
        assert [d["name"] for d in scan["result"]] == ["/dev/sda", "/dev/nvme0"]
        allh = await call(c, "nas_smart_status")
        by = {d["device"]: d for d in allh["result"]}
        assert by["/dev/sda"]["health"] == "WARNING" and "reallocated_sectors=8" in by["/dev/sda"]["warnings"]
        assert by["/dev/nvme0"]["health"] == "OK" and by["/dev/nvme0"]["counters"]["percentage_used"] == 3
        missing = await call(c, "nas_smart_status", {"device": "/dev/sdz"})
        assert missing["status"] == "blocked"


def test_parse_failing_and_validation():
    data = json.loads((FIXTURES / "smart_sda_warning.json").read_text())
    data["smart_status"]["passed"] = False
    assert parse_smartctl(data)["health"] == "FAILING"
    for bad in ["/dev/sda; rm -rf /", "../../etc", "-d foo"]:
        with pytest.raises(SmartError):
            validate_device(bad)


class FakeTrueNAS(FakeApp):
    def handle(self, req: FakeRequest):
        if req.headers.get("authorization") != "Bearer tn-key":
            return 401, {"message": "unauthorized"}
        if req.path == "/api/v2.0/pool":
            return 200, [{"name": "tank", "status": "ONLINE", "healthy": True, "size": 1000, "allocated": 400}]
        if req.path == "/api/v2.0/alert/list":
            return 200, [{"level": "WARNING", "formatted": "Scrub overdue", "dismissed": False},
                         {"level": "INFO", "formatted": "old", "dismissed": True}]
        return 404, {}


class FakeSynology(FakeApp):
    def handle(self, req: FakeRequest):
        form = req.form()
        if req.path == "/webapi/auth.cgi":
            ok = form.get("account") == "svc" and form.get("passwd") == "pw"
            return 200, {"success": ok, **({"data": {"sid": "SID1"}} if ok else {"error": {"code": 400}})}
        if req.path == "/webapi/entry.cgi" and form.get("_sid") == "SID1":
            return 200, {"success": True, "data": {
                "volumes": [{"id": "volume_1", "status": "normal", "size": {"total": "2000", "used": "500"}}],
                "disks": [{"name": "Drive 1", "status": "normal", "smart_status": "normal", "temp": 34}]}}
        return 200, {"success": False, "error": {"code": 119}}


async def test_appliances(tree, make_guard):
    tn = TrueNAS("http://nas.test", "tn-key", transport=FakeTrueNAS().transport())
    h = await tn.health()
    assert h["pools"][0]["name"] == "tank" and [a["text"] for a in h["alerts"]] == ["Scrub overdue"]
    syn = FakeSynology()
    s = await Synology("http://nas.test", "svc", "pw", transport=syn.transport()).health()
    assert s["pools"][0]["healthy"] and s["disks"][0]["temp_c"] == 34
    assert all("pw" not in r.raw_query for r in syn.requests)  # password never in a URL
    server = build_server({"nas": {"roots": {"docs": str(tree / "docs")}}}, guard=make_guard("nas"),
                          smart_runner=fake_smartctl, appliance=tn)
    async with Client(server) as c:
        r = await call(c, "nas_appliance_health")
        assert r["result"]["appliance"] == "truenas"
