"""End-to-end check: spawn the server over stdio like Claude Code does and call tools via the MCP client.

Runs against the live web (Skroutz) with default settings, so it also exercises the default safety policy."""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "cloakbrowser_mcp"], env={"PYTHONUTF8": "1"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(len(tools), "tools; read-only:", sum(1 for t in tools if t.annotations and t.annotations.readOnlyHint))

            async def call(name, **a):
                res = await s.call_tool(name, a)
                return res.content[0].text if res.content and hasattr(res.content[0], "text") else res.content

            print("private blocked :", (await call("navigate", url="http://127.0.0.1:9/"))[:80])
            print("file blocked    :", (await call("navigate", url="file:///c:/windows/win.ini"))[:80])
            print("navigate        :", await call("navigate", url="https://www.skroutz.gr/c/2515/creatines.html"))
            print("robots          :", await call("check_robots", url="https://www.skroutz.gr/c/2515/creatines.html"))
            snap = await call("snapshot", max_items=6)
            print("snapshot wrapped:", snap.startswith("<untrusted_page_content>"), "|", snap.splitlines()[1][:60])
            meta = await call("extract_metadata", max_chars=600)
            print("metadata        :", meta[:300].replace("\n", " "))
            fp = json.loads(await call("fingerprint_info"))
            print("fingerprint     :", fp["observed"]["platform"], fp["observed"]["webdriver"], fp["observed"]["webglRenderer"][:50])
            await call("close_browser")


asyncio.run(main())
