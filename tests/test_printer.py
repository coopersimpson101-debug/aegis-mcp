from __future__ import annotations

import base64

import pytest
from mcp import Client

from aegis_mcp.printer import MoonrakerBackend, OctoPrintBackend, build_server, scan_gcode
from aegis_mcp.sim.http_fake import TINY_JPEG
from aegis_mcp.sim.printers import FakeMoonraker, FakeOctoPrint

from .conftest import call


def _octo():
    fake = FakeOctoPrint(api_key="k")
    return fake, OctoPrintBackend("http://printer.test", api_key="k", transport=fake.transport())


def _moon():
    fake = FakeMoonraker()
    return fake, MoonrakerBackend("http://printer.test", transport=fake.transport())


BACKENDS = [_octo, _moon]


@pytest.mark.parametrize("factory", BACKENDS, ids=["octoprint", "moonraker"])
async def test_status_temps_progress(factory, make_guard):
    fake, backend = factory()
    server = build_server({}, backend=backend, guard=make_guard("printer"))
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"printer_status", "printer_set_temperature", "printer_upload_gcode", "printer_start_print",
                "printer_pause", "printer_resume", "printer_cancel", "printer_progress",
                "printer_get_temperatures", "printer_list_files"} <= names
        st = await call(c, "printer_status")
        assert st["status"] == "ok" and st["result"]["backend"] == backend.name
        assert "tool0" in st["result"]["temperatures"] and "bed" in st["result"]["temperatures"]
        temps = await call(c, "printer_get_temperatures")
        assert temps["result"]["bed"]["target_c"] == 0
        prog = await call(c, "printer_progress")
        assert prog["result"]["file"] is None


@pytest.mark.parametrize("factory", BACKENDS, ids=["octoprint", "moonraker"])
async def test_set_temperature_tiers(factory, make_guard, notes):
    fake, backend = factory()
    server = build_server({}, backend=backend, guard=make_guard("printer"))
    async with Client(server) as c:
        ok = await call(c, "printer_set_temperature", {"heater": "bed", "target_c": 60})
        assert ok["status"] == "ok" and ok["risk"] == "medium"
        assert fake.printer.temps["bed"][1] == 60
        # above confirm threshold (90 C bed) -> needs a human
        pend = await call(c, "printer_set_temperature", {"heater": "bed", "target_c": 100})
        assert pend["status"] == "confirmation_required" and "code" not in pend
        assert fake.printer.temps["bed"][1] == 60
        # guessed token fails, real code works, and only once
        bad = await call(c, "printer_set_temperature", {"heater": "bed", "target_c": 100, "confirm_token": "AAAAAA"})
        assert bad["status"] == "confirmation_required"
        code = notes.codes[pend["request_id"]]
        done = await call(c, "printer_set_temperature", {"heater": "bed", "target_c": 100, "confirm_token": code})
        assert done["status"] == "ok" and fake.printer.temps["bed"][1] == 100
        again = await call(c, "printer_set_temperature", {"heater": "bed", "target_c": 100, "confirm_token": code})
        assert again["status"] == "confirmation_required"
        # above hard max is refused outright
        hard = await call(c, "printer_set_temperature", {"heater": "tool0", "target_c": 320})
        assert hard["status"] == "blocked" and "hard limit" in hard["reason"]
        # turning off is always low risk
        off = await call(c, "printer_set_temperature", {"heater": "tool0", "target_c": 0})
        assert off["risk"] == "low"


@pytest.mark.parametrize("factory", BACKENDS, ids=["octoprint", "moonraker"])
async def test_upload_and_print_lifecycle(factory, make_guard, notes):
    fake, backend = factory()
    server = build_server({}, backend=backend, guard=make_guard("printer"))
    async with Client(server) as c:
        up = await call(c, "printer_upload_gcode", {"filename": "part.gcode", "gcode": "M140 S60\nM104 S210\nG28\n"})
        assert up["status"] == "ok" and fake.printer.files["part.gcode"].startswith(b"M140")
        files = await call(c, "printer_list_files")
        assert "part.gcode" in [f["name"] for f in files["result"]]
        pend = await call(c, "printer_start_print", {"filename": "part.gcode"})
        assert pend["status"] == "confirmation_required" and fake.printer.state == "idle"
        go = await call(c, "printer_start_print", {"filename": "part.gcode", "confirm_token": notes.codes[pend["request_id"]]})
        assert go["status"] == "ok" and fake.printer.state == "printing"
        prog = await call(c, "printer_progress")
        assert prog["result"]["file"] == "part.gcode" and prog["result"]["progress_pct"] > 0
        assert (await call(c, "printer_pause"))["status"] == "ok" and fake.printer.state == "paused"
        assert (await call(c, "printer_resume"))["status"] == "ok" and fake.printer.state == "printing"
        assert (await call(c, "printer_cancel"))["status"] == "ok" and fake.printer.state == "cancelled"


async def test_upload_scanner_blocks_and_escalates(make_guard):
    fake, backend = _octo()
    server = build_server({}, backend=backend, guard=make_guard("printer"))
    async with Client(server) as c:
        r = await call(c, "printer_upload_gcode", {"filename": "evil.gcode", "gcode": "M104 S400\n"})
        assert r["status"] == "blocked" and "hotend" in r["reason"]
        r = await call(c, "printer_upload_gcode", {"filename": "../etc.gcode", "gcode": "G28\n"})
        assert r["status"] == "blocked"
        r = await call(c, "printer_upload_gcode", {"filename": "hot.gcode", "gcode": "M190 S100 ; ABS\n"})
        assert r["status"] == "confirmation_required"
    assert "evil.gcode" not in fake.printer.files


def test_scan_gcode():
    peaks = scan_gcode("; M104 S999 comment only\nM104 S215 T0\nM109 S220\nM140 S70\nM190 S75\nG1 X1 S900\n")
    assert peaks == {"hotend": 220.0, "bed": 75.0, "chamber": 0.0}


async def test_webcam_and_status_resources(make_guard):
    fake, backend = _moon()
    server = build_server({}, backend=backend, guard=make_guard("printer"))
    async with Client(server) as c:
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert {"printer://status", "printer://webcam/snapshot", "printer://files"} <= uris
        snap = await c.read_resource("printer://webcam/snapshot")
        blob = snap.contents[0]
        assert blob.mime_type == "image/jpeg" and base64.b64decode(blob.blob) == TINY_JPEG


async def test_bad_api_key_surfaces_clean_error(make_guard):
    fake = FakeOctoPrint(api_key="right")
    backend = OctoPrintBackend("http://printer.test", api_key="wrong", transport=fake.transport())
    server = build_server({}, backend=backend, guard=make_guard("printer"))
    async with Client(server) as c:
        with pytest.raises(RuntimeError, match="credentials"):
            await call(c, "printer_status")


async def test_fake_octoprint_over_real_socket():
    fake = FakeOctoPrint(api_key="k")
    srv, _ = fake.serve()
    try:
        backend = OctoPrintBackend(f"http://127.0.0.1:{srv.server_address[1]}", api_key="k")
        st = await backend.status()
        assert st["operational"] and st["backend"] == "octoprint"
        await backend.upload("sock.gcode", b"G28\n")
        assert "sock.gcode" in fake.printer.files
        await backend.aclose()
    finally:
        srv.shutdown()
