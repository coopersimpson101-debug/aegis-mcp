"""Protocol-level tests: spawn real server processes and talk MCP over stdio."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


def _config(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / "server.toml"
    state = (tmp_path / "state").as_posix()
    audit = (tmp_path / "audit.jsonl").as_posix()
    cfg.write_text(f'[safety]\naudit_log = "{audit}"\nstate_dir = "{state}"\n{extra}', encoding="utf-8")
    return cfg


async def test_nas_server_over_stdio(tmp_path: Path):
    (tmp_path / "share").mkdir()
    (tmp_path / "share" / "readme.md").write_text("# hi")
    cfg = _config(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "aegis_mcp.nas", "--config", str(cfg), "--root", f"share={tmp_path / 'share'}"])
    async with Client(params) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert {"nas_list", "nas_read", "nas_search", "nas_disk_usage", "nas_smart_status", "nas_delete"} <= set(tools)
        assert tools["nas_delete"].meta["aegis/risk"] == "high"
        assert tools["nas_read"].annotations.read_only_hint is True
        r = await c.call_tool("nas_list", {"path": "share"})
        body = json.loads(r.content[0].text)
        assert body["status"] == "ok" and body["result"]["entries"][0]["name"] == "readme.md"
        r = await c.call_tool("nas_read", {"path": "share/../../etc/passwd"})
        assert json.loads(r.content[0].text)["status"] == "blocked"
    # the subprocess wrote an audit log that verifies
    from aegis_mcp.safety import verify_audit_log

    assert verify_audit_log(tmp_path / "audit.jsonl")[0]


async def test_every_server_lists_tools_over_stdio(tmp_path: Path):
    cfg = _config(tmp_path)
    cases = {
        "aegis_mcp.printer": (["--url", "http://127.0.0.1:9"], "printer_set_temperature"),
        "aegis_mcp.drone": (["--connection", "tcp:127.0.0.1:9"], "drone_arm"),
        "aegis_mcp.lab": (["--demo"], "lab_set_power"),
    }
    for module, (args, expected) in cases.items():
        params = StdioServerParameters(command=sys.executable, args=["-m", module, "--config", str(cfg), *args])
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert expected in names, (module, names)


async def test_lab_server_over_streamable_http(tmp_path: Path):
    import socket
    import subprocess
    import time

    import anyio
    import httpx

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    cfg = _config(tmp_path)
    proc = subprocess.Popen([sys.executable, "-m", "aegis_mcp.lab", "--config", str(cfg), "--demo",
                             "--transport", "streamable-http", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=0.5)
                break
            except httpx.HTTPError:
                await anyio.sleep(0.2)
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert "lab_activate_scene" in names
            r = await c.call_tool("lab_list_scenes", {})
            assert json.loads(r.content[0].text)["status"] == "ok"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
