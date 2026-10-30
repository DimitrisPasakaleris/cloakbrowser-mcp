"""End-to-end check of the token-saving features over real MCP stdio (local test site, isolated home).

Covers: the lean profile, read_url / find_in_page / run_steps, scaled screenshots, the usage log + `stats`, and the memory
tools appearing (with a tools/list_changed notification) only after the user opts in."""
import asyncio
import functools
import http.server
import json
import subprocess
import sys
import tempfile
import threading
from base64 import b64decode
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult

ROOT = Path(__file__).parent
FAILS = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        FAILS.append(label)


def serve():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "tests" / "site"))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def params(home, **env):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "cloakbrowser_mcp"],
        env={"PYTHONUTF8": "1", "CLOAK_MCP_HOME": home, "CLOAK_HUMANIZE": "0", "CLOAK_ALLOW_PRIVATE": "1", **env},
    )


async def text(cs, name, **a):
    res = await cs.call_tool(name, a)
    return "".join(c.text for c in res.content if c.type == "text")


def png_or_jpeg_width(data: bytes) -> int:
    import struct

    if data[:4] == b"\x89PNG":
        return struct.unpack(">I", data[16:20])[0]
    i = 2
    while i + 9 < len(data):
        if data[i] == 0xFF and data[i + 1] in (0xC0, 0xC1, 0xC2):
            return struct.unpack(">H", data[i + 7:i + 9])[0]
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0] if data[i] == 0xFF else 1
    return 0


async def lean(base):
    print("1. lean profile")
    home = tempfile.mkdtemp(prefix="cloaklean-")
    async with stdio_client(params(home, CLOAK_TOOLS="lean", CLOAK_USAGE_LOG="1")) as (r, w):
        async with ClientSession(r, w) as cs:
            await cs.initialize()
            names = sorted(t.name for t in (await cs.list_tools()).tools)
            check("18 lean tools, no memory/debug/configure", len(names) == 18 and not any(n.startswith("memory_") or n in ("configure", "evaluate") for n in names), f"{len(names)} tools")
            out = await text(cs, "read_url", url=base + "/article.html")
            check("read_url: one call, clean markdown", "# Real Article" in out and "Nav One" not in out and "Footer legal" not in out)
            check("no 'memory is off' hint when memory tools are not offered", "MEMORY is off" not in out)
            found = await text(cs, "find_in_page", query="needle", context=20)
            check("find_in_page returns a snippet", "1 match" in found and len(found) < 250)
            steps = await text(cs, "run_steps", steps=[
                {"do": "goto", "url": base + "/index.html"},
                {"do": "type", "target": "#name", "text": "Ann"},
                {"do": "select", "target": "#sel", "value": "y"},
                {"do": "click", "target": "#go"},
                {"do": "wait", "text": "hello Ann"},
            ], then="text")
            check("run_steps: 5 actions in one call", "hello Ann/y" in steps and "ERROR" not in steps.split("<untrusted")[0], steps.splitlines()[0][:60])
            shot = await cs.call_tool("screenshot", {})
            img = next(c for c in shot.content if c.type == "image")
            width = png_or_jpeg_width(b64decode(img.data))
            check("screenshot: JPEG, scaled to <= 1024px", img.mimeType == "image/jpeg" and 0 < width <= 1024, f"{img.mimeType} {width}px")
            await text(cs, "close_browser")
    log = Path(home) / "logs" / "usage.jsonl"
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    check("usage log has one line per call, names only", {r["tool"] for r in rows} >= {"read_url", "find_in_page", "run_steps", "screenshot"} and "article" not in log.read_text(encoding="utf-8"))
    res = subprocess.run([sys.executable, "-m", "cloakbrowser_mcp", "stats", "--json"], capture_output=True, text=True,
                         env={**__import__("os").environ, "CLOAK_MCP_HOME": home, "PYTHONUTF8": "1"})
    data = json.loads(res.stdout)
    sizes = {d["profile"]: d["tokens"] for d in data["definitions"]}
    check("stats command reports usage and definition sizes", data["results"]["total"]["calls"] >= 5 and sizes["lean"] < sizes["full (memory on)"] / 2, f"lean ~{sizes['lean']} vs full ~{sizes['full (memory on)']} tokens")


async def memory_gate(base):
    print("2. memory tools appear only after opt-in")
    home = tempfile.mkdtemp(prefix="cloakgate-")
    changed = []

    async def on_message(msg):
        if type(msg).__name__ == "ServerNotification" or hasattr(msg, "root"):
            changed.append(type(getattr(msg, "root", msg)).__name__)

    async def accept(context, p):
        return ElicitResult(action="accept", content={"decision": "enable", "site_stats": True, "site_structure": True, "extractors": True,
                                                       "overlays": True, "recipes": False, "notes": False, "scope_mode": "all_sites",
                                                       "scope_domains": "", "retention_days": "30", "effort": "low", "reflect_when": "manual"})

    async with stdio_client(params(home)) as (r, w):
        async with ClientSession(r, w, elicitation_callback=accept, message_handler=on_message) as cs:
            await cs.initialize()
            before = {t.name for t in (await cs.list_tools()).tools if t.name.startswith("memory_")}
            check("before opt-in only memory_request_access is listed", before == {"memory_request_access"}, str(sorted(before)))
            out = await text(cs, "memory_request_access")
            await asyncio.sleep(0.5)
            after = {t.name for t in (await cs.list_tools()).tools if t.name.startswith("memory_")}
            check("after opt-in the other memory tools are listed", len(after) >= 9 and "enabled" in out.lower(), f"{len(after)} memory tools")
            check("client was told the tool list changed", any("ToolListChanged" in c for c in changed), str(changed[-3:]))
            await text(cs, "close_browser")


async def main():
    srv, base = serve()
    try:
        await lean(base)
        await memory_gate(base)
    finally:
        srv.shutdown()
    print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
