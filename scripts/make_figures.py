"""Regenerate the README figures from real runs (no hardware, no API key).

    python scripts/make_figures.py

Writes, under docs/assets/:
  banner.svg              banner; the motif is the guard's real decision for every bed setpoint 0-130 C
  safety_walkthrough.svg  a dangerous call blocked until the human's code is supplied (real MCP calls)
  offline_demo.svg        stdout of examples/offline_demo.py
  route_explain.svg       stdout of examples/route_explain.py
and the plain-text captures next to them (docs/assets/*.txt).

Everything runs in-process against the simulators in src/aegis_mcp/sim; the tool calls go through a
real MCP client session, so the guard, confirmation codes and audit log are the production code paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from mcp import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aegis_mcp.nas import build_server as build_nas  # noqa: E402
from aegis_mcp.printer import OctoPrintBackend  # noqa: E402
from aegis_mcp.printer import build_server as build_printer  # noqa: E402
from aegis_mcp.safety import Guard, SafetyPolicy  # noqa: E402
from aegis_mcp.safety.audit import verify_audit_log  # noqa: E402
from aegis_mcp.sim.printers import FakeOctoPrint  # noqa: E402

ASSETS = ROOT / "docs" / "assets"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
BG, FG, MUTED, GRID = "#0d1117", "#e6edf3", "#8b949e", "#21262d"
ACCENT, RED, AMBER, GREEN = "#818CF8", "#f85149", "#d29922", "#3fb950"
STATUS_COLOUR = {"ok": GREEN, "confirmation_required": AMBER, "blocked": RED, "dry_run": ACCENT}

Seg = tuple[str, str]   # (text, colour)


async def _call(c: Client, tool: str, args: dict) -> dict:
    r = await c.call_tool(tool, args)
    return json.loads("".join(getattr(x, "text", "") for x in r.content))


# ------------------------------------------------------------------- walkthrough
async def walkthrough(tmp: Path) -> tuple[list[list[Seg]], list[tuple[float, str]]]:
    codes: dict[str, str] = {}

    def guard(name: str) -> Guard:
        pol = SafetyPolicy(audit_log=str(tmp / f"{name}.jsonl"), state_dir=str(tmp / "state"))
        return Guard(pol, server=name, notifier=lambda p: codes.__setitem__(p.request_id, p.code))

    octo = FakeOctoPrint(api_key="demo")
    printer = build_printer({}, backend=OctoPrintBackend("http://sim", api_key="demo", transport=octo.transport()),
                            guard=guard("printer"))
    (tmp / "docs").mkdir()
    (tmp / "docs" / "notes.txt").write_text("hello\n")
    (tmp / "secret.txt").write_text("outside the root\n")
    nas = build_nas({"nas": {"roots": {"docs": str(tmp / "docs")}}}, guard=guard("nas"))

    lines: list[list[Seg]] = []

    def bed() -> str:
        return f"{octo.printer.temps['bed'][1]:g} C"

    def show(tool: str, args: dict, body: dict, note: str = "") -> None:
        a = json.dumps({k: v for k, v in args.items()}, separators=(", ", ": "))
        lines.append([("model → ", MUTED), (tool, FG), (" " + a, MUTED)])
        st = body.get("status", "?")
        detail = {"ok": f"risk={body.get('risk')}",
                  "confirmation_required": f"risk={body.get('risk')}  request_id={body.get('request_id')}  "
                                           f"expires_in_s={body.get('expires_in_s')}",
                  "blocked": f"{body.get('reason')}"}.get(st, "")
        row = [("   ← ", MUTED), (st, STATUS_COLOUR.get(st, FG)), ("  " + detail, MUTED)]
        if note:
            row.append(("   " + note, FG))
        lines.append(row)

    async with Client(printer) as p, Client(nas) as n:
        a = {"heater": "bed", "target_c": 60}
        show("printer_set_temperature", a, await _call(p, "printer_set_temperature", a), f"bed target: {bed()}")
        a = {"heater": "bed", "target_c": 100}
        pend = await _call(p, "printer_set_temperature", a)
        show("printer_set_temperature", a, pend, f"bed target still {bed()}; no code in the tool result")
        rid = pend["request_id"]
        a2 = {**a, "confirm_token": "AAAAAA"}
        show("printer_set_temperature", a2, await _call(p, "printer_set_temperature", a2), "(model guessed a code)")
        code = codes[rid]
        lines.append([("server log → human: ", MUTED), (f"confirmation code for {rid}: ", FG), (code, ACCENT)])
        a3 = {"heater": "bed", "target_c": 105, "confirm_token": code}
        show("printer_set_temperature", a3, await _call(p, "printer_set_temperature", a3),
             "(code is bound to the exact arguments)")
        a4 = {**a, "confirm_token": code}
        show("printer_set_temperature", a4, await _call(p, "printer_set_temperature", a4), f"bed target: {bed()}")
        show("printer_set_temperature", a4, await _call(p, "printer_set_temperature", a4), "(codes are single-use)")
        a5 = {"heater": "bed", "target_c": 130}
        show("printer_set_temperature", a5, await _call(p, "printer_set_temperature", a5))
        a6 = {"path": "docs/../secret.txt"}
        show("nas_read", a6, await _call(n, "nas_read", a6))

        # the banner motif: the guard's decision for every bed setpoint, no confirmation given
        ladder = []
        for t in range(0, 131, 5):
            b = await _call(p, "printer_set_temperature", {"heater": "bed", "target_c": t})
            ladder.append((t, b["status"] if b["status"] != "ok" else b["risk"]))
        await _call(p, "printer_set_temperature", {"heater": "bed", "target_c": 0})

    for name in ("printer", "nas"):
        ok, count, msg = verify_audit_log(tmp / f"{name}.jsonl")
        lines.append([("aegis-mcp audit ", MUTED), (f"{name}.jsonl", FG), (" → ", MUTED),
                      ("chain intact" if ok else "TAMPERED", GREEN if ok else RED),
                      (f" ({count} record{'s' * (count != 1)})" + ("" if msg == "ok" else f" {msg}"), MUTED)])
    return lines, ladder


def run_example(script: str) -> list[str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, str(ROOT / "examples" / script)], capture_output=True, text=True,
                       encoding="utf-8", env=env, cwd=ROOT, timeout=300, check=True)
    tmp = tempfile.gettempdir()
    # keep local paths (which contain the user name) out of the published figures
    out = [ln.rstrip().replace(tmp, "$TMPDIR") for ln in r.stdout.splitlines()]
    while out and not out[-1]:
        out.pop()
    return out


# ------------------------------------------------------------------- rendering
def terminal_svg(title: str, command: str, rows: list[list[Seg]], width: int = 1180) -> str:
    fs, lh, pad = 13, 20, 22
    H = int(64 + pad + lh * (len(rows) + 1) + pad - 6)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {H}" width="{width}" height="{H}" '
           f'font-family="{MONO}" font-size="{fs}">',
           f'<rect x="0.5" y="0.5" width="{width - 1}" height="{H - 1}" rx="10" fill="{BG}" stroke="#30363d"/>',
           f'<path d="M0.5,40 h{width - 1}" stroke="#30363d"/>',
           '<circle cx="22" cy="20" r="6" fill="#ff5f56"/><circle cx="42" cy="20" r="6" fill="#ffbd2e"/>'
           '<circle cx="62" cy="20" r="6" fill="#27c93f"/>',
           f'<text x="{width / 2}" y="25" text-anchor="middle" fill="{MUTED}" font-size="12.5">{escape(title)}</text>',
           f'<text x="{pad}" y="68" fill="{ACCENT}">$ <tspan fill="{FG}">{escape(command)}</tspan></text>']
    y = 68 + lh
    for row in rows:
        body = "".join(f'<tspan fill="{c}">{escape(t)}</tspan>' for t, c in row if t)
        out.append(f'<text x="{pad}" y="{y}" xml:space="preserve">{body}</text>')
        y += lh
    out.append("</svg>")
    return "\n".join(out)


def colour_plain(lines: list[str]) -> list[list[Seg]]:
    rows = []
    for ln in lines:
        if ln.startswith("FINAL:"):
            rows.append([("FINAL:", ACCENT), (ln[6:], FG)])
        elif "[human]" in ln:
            rows.append([(ln, AMBER)])
        elif ln.strip().startswith("tool "):
            head, _, st = ln.rpartition(": ")
            rows.append([(head + ": ", FG), (st, STATUS_COLOUR.get(st, FG))])
        elif ln.strip().startswith("reasons:"):
            rows.append([(ln, MUTED)])
        elif ln[:9].strip() in ("local", "fast", "balanced", "deep"):
            rows.append([(ln[:30], ACCENT), (ln[30:], FG)])
        else:
            rows.append([(ln, MUTED if ln.startswith(("audit", "connected")) else FG)])
    return rows


def banner_svg(ladder: list[tuple[float, str]]) -> str:
    W, H = 1280, 400
    colour = {"low": "#3b3f8f", "medium": "#5b61c9", "confirmation_required": ACCENT, "blocked": RED}
    x0, base, bw, gap = 760, 318, 12, 6
    bars = []
    for i, (t, s) in enumerate(ladder):
        h = 18 + t * 1.45
        x = x0 + i * (bw + gap)
        op = "0.55" if s == "blocked" else "1"
        bars.append(f'<rect x="{x}" y="{base - h:.1f}" width="{bw}" height="{h:.1f}" rx="2" '
                    f'fill="{colour.get(s, MUTED)}" opacity="{op}"/>')
    xe = x0 + len(ladder) * (bw + gap) - gap
    legend = []
    lx = x0
    for label, key in (("auto", "medium"), ("needs human code", "confirmation_required"), ("refused", "blocked")):
        legend.append(f'<rect x="{lx}" y="345" width="10" height="10" rx="2" fill="{colour[key]}"/>'
                      f'<text x="{lx + 16}" y="354" font-size="12" fill="{MUTED}">{label}</text>')
        lx += 40 + len(label) * 7.4
    return "\n".join([
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{MONO}">',
        '<defs><pattern id="g" width="32" height="32" patternUnits="userSpaceOnUse">'
        f'<path d="M 32 0 L 0 0 0 32" fill="none" stroke="{GRID}" stroke-width="1"/></pattern>'
        '<radialGradient id="glow" cx="0.78" cy="0.55" r="0.5">'
        f'<stop offset="0" stop-color="{ACCENT}" stop-opacity="0.12"/>'
        f'<stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></radialGradient></defs>',
        f'<rect width="{W}" height="{H}" fill="{BG}"/>',
        f'<rect width="{W}" height="{H}" fill="url(#g)" opacity="0.6"/>',
        f'<rect width="{W}" height="{H}" fill="url(#glow)"/>',
        f'<rect x="72" y="118" width="6" height="74" fill="{ACCENT}"/>',
        f'<text x="98" y="178" font-size="76" font-weight="700" fill="{FG}">aegis-mcp</text>',
        f'<text x="100" y="232" font-size="22" fill="{MUTED}">Safety-first MCP servers for physical hardware</text>',
        f'<text x="100" y="272" font-size="15" fill="{ACCENT}">printer · drone · NAS · lab bench · '
        f'cost-aware model router</text>',
        f'<text x="{xe}" y="96" font-size="12" text-anchor="end" fill="{MUTED}">real guard decisions: '
        f'printer_set_temperature(bed, 0→130 °C)</text>',
        *bars,
        f'<line x1="{x0 - 4}" y1="{base + 0.5}" x2="{xe + 4}" y2="{base + 0.5}" stroke="#30363d"/>',
        *legend,
        "</svg>"])


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aegis-figs-") as d:
        rows, ladder = asyncio.run(walkthrough(Path(d)))
    files = {
        "safety_walkthrough": (terminal_svg("safety layer: real MCP calls against the simulated printer and NAS",
                                            "python scripts/make_figures.py   # safety walkthrough", rows, 1240),
                               ["".join(t for t, _ in r) for r in rows]),
        "banner": (banner_svg(ladder), [f"{t:g} C -> {s}" for t, s in ladder]),
    }
    for script, title in (("offline_demo.py", "orchestrator + 2 MCP servers + scripted model, no hardware"),
                          ("route_explain.py", "cost-aware router decisions (no API calls)")):
        out = run_example(script)
        files[script[:-3]] = (terminal_svg(title, f"python examples/{script}", colour_plain(out)), out)
    for name, (svg, text) in files.items():
        (ASSETS / f"{name}.svg").write_text(svg + "\n", encoding="utf-8", newline="\n")
        (ASSETS / f"{name}.txt").write_text("\n".join(text) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote docs/assets/{name}.svg and .txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
