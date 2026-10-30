"""End-to-end check of the self-improving memory over real MCP stdio, with nothing configured (so memory is on by default).

Walks through: the default-on notice, the light memory tool set, learning a pop-up dismisser from a click and re-using it,
the escalating load strategy on a blocked site, a learned extractor in read_url, persistence across server restarts, the CLI
(status / reflect / disable), and that a user's "off" sticks. Uses a local test site and a temp home."""
import asyncio
import functools
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent
FAILS = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        FAILS.append(label)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "tests" / "site"), **kw)

    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        if self.path.startswith("/challenge"):
            body = b"<html><head><title>Just a moment...</title></head><body>checking your browser</body></html>"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


def serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def params(home):
    # No CLOAK_MEMORY* variables: this is what a fresh install looks like. ALLOW_IP only lets it learn the local test site.
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "cloakbrowser_mcp"],
        env={"PYTHONUTF8": "1", "CLOAK_MCP_HOME": home, "CLOAK_HUMANIZE": "0", "CLOAK_ALLOW_PRIVATE": "1", "CLOAK_MEMORY_ALLOW_IP": "1"},
    )


async def call(cs, name, **a):
    res = await cs.call_tool(name, a)
    return "".join(c.text for c in res.content if c.type == "text")


def cli(home, *args):
    env = {**os.environ, "CLOAK_MCP_HOME": home, "PYTHONUTF8": "1"}
    for k in ("CLOAK_MEMORY", "CLOAK_MEMORY_DEFAULT"):
        env.pop(k, None)
    r = subprocess.run([sys.executable, "-m", "cloakbrowser_mcp", "memory", *args], capture_output=True, text=True, env=env)
    return r.stdout.strip()


async def session_one(home, base):
    print("1. first session (nothing configured)")
    async with stdio_client(params(home)) as (r, w):
        async with ClientSession(r, w) as cs:
            await cs.initialize()
            mem_tools = sorted(t.name for t in (await cs.list_tools()).tools if t.name.startswith("memory_"))
            check("light memory tool set listed", mem_tools == ["memory_recall", "memory_request_access", "memory_status"], str(mem_tools))
            first = await call(cs, "navigate", url=base + "/banner.html")
            check("default-on notice on the first navigate", "MEMORY IS ON" in first and "CLOAK_MEMORY=off" in first)
            check("no 'memory is off' prompt hint", "MEMORY is off" not in first)
            await call(cs, "click", selector="#accept-cookies")
            again = await call(cs, "navigate", url=base + "/banner.html")
            check("a pop-up dismissed once is dismissed automatically", "dismissed 1 remembered pop-up(s)" in again and "MEMORY IS ON" not in again, again[-90:])
            hidden = await call(cs, "evaluate", script="() => getComputedStyle(document.getElementById('cookie-banner')).display")
            check("the banner really is gone", "none" in hidden)

            outs = [await call(cs, "navigate", url=base + "/challenge") for _ in range(3)]
            check("a blocked site escalates its load strategy on the 3rd visit", "load strategy" not in outs[0] + outs[1] and "learned load strategy load+1500ms" in outs[2], outs[2][-110:])

            await call(cs, "navigate", url=base + "/bigshop.html")
            for _ in range(2):
                await call(cs, "extract_structured", item_selector="li.card", fields={"name": "h2", "price": ".price"})
            read = await call(cs, "read_url", url=base + "/bigshop.html")
            plain = await call(cs, "read_url", url=base + "/bigshop.html", use_memory=False)
            check("read_url uses the learned extractor (and it is smaller)", "learned extractor, 40 items" in read and "Product 40 gummies" in read and len(read) < len(plain), f"{len(read)} vs {len(plain)} chars")
            check("use_memory=false gives the whole page", "learned extractor" not in plain and "# Big Shop" in plain)

            st = json.loads(await call(cs, "memory_status"))
            lr = st["learned"]
            check("memory_status shows what was learned", st["source"] == "default" and lr["popup_dismissers"] == 1 and lr["extractors"] == 1 and lr["sites_with_playbook"] >= 1, str({k: v for k, v in lr.items() if k != "impact"}))
            check("impact numbers recorded", lr["impact"]["overlay"]["ok"] >= 1 and lr["impact"]["extractor"]["ok"] >= 1, str(lr["impact"]))
            blob = json.dumps(st) + await call(cs, "memory_recall", site="127.0.0.1")
            check("no selectors leak into tool output", "#accept-cookies" not in blob and "li.card" not in blob)
            await call(cs, "close_browser")


async def session_two(home, base):
    print("2. second session (same home): it remembers")
    async with stdio_client(params(home)) as (r, w):
        async with ClientSession(r, w) as cs:
            await cs.initialize()
            first = await call(cs, "navigate", url=base + "/banner.html")
            check("pop-up dismissed on the very first visit of a new session", "dismissed 1 remembered pop-up(s)" in first, first[-90:])
            check("the notice is not repeated", "MEMORY IS ON" not in first)
            nav = await call(cs, "navigate", url=base + "/challenge")
            check("the benched strategy is still used", "learned load strategy" in nav, nav[-100:])
            await call(cs, "close_browser")


async def session_three(home, base):
    print("3. after the user switches memory off")
    async with stdio_client(params(home)) as (r, w):
        async with ClientSession(r, w) as cs:
            await cs.initialize()
            out = await call(cs, "navigate", url=base + "/banner.html")
            check("nothing is applied or announced", "dismissed" not in out and "MEMORY IS ON" not in out and "load strategy" not in out, out[-80:])
            mem_tools = sorted(t.name for t in (await cs.list_tools()).tools if t.name.startswith("memory_"))
            check("only the way back in (memory_request_access) is listed", mem_tools == ["memory_request_access"], str(mem_tools))
            await call(cs, "close_browser")


async def main():
    srv, base = serve()
    home = tempfile.mkdtemp(prefix="cloaklearn-")
    try:
        await session_one(home, base)
        print("CLI")
        status = json.loads(cli(home, "status"))
        check("memory status (CLI) sees the learning", status["source"] == "default" and status["learned"]["popup_dismissers"] == 1)
        reflect = json.loads(cli(home, "reflect"))
        check("memory reflect (CLI) runs a pass", set(reflect) == {"forgot", "unbenched", "promoted"}, str(reflect))
        await session_two(home, base)
        cli_out = cli(home, "disable")
        check("memory disable (CLI) turns it off", "disabled all" in cli_out, cli_out[:60])
        check("and it stays off", json.loads(cli(home, "status"))["enabled"] is False)
        await session_three(home, base)
    finally:
        srv.shutdown()
    print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall checks passed")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
