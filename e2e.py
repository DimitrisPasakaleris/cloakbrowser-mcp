"""End-to-end check: spawn the server over stdio like Claude Code does and call tools via the MCP client."""
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "cloakbrowser_mcp"], env={"PYTHONUTF8": "1"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            print(len(tools), "tools:", ", ".join(t.name for t in tools))
            async def call(name, **a):
                res = await s.call_tool(name, a)
                return res.content[0].text if res.content and hasattr(res.content[0], "text") else res.content
            print(await call("navigate", url="https://www.skroutz.gr/search?keyphrase=creatine+gummies"))
            print((await call("check_stealth"))[:300])
            print((await call("get_markdown", max_chars=700)))
            print((await call("snapshot", max_items=8)))
            await call("close_browser")
asyncio.run(main())
