"""Stealth audit harness: launches CloakBrowser the way server.py does and runs leak probes.

Usage:  .venv/Scripts/python.exe audit/probe.py [--headed] [--proxy URL] [--locale xx-XX] [--tz Area/City] [--site URL]
Prints one JSON document. Exit code is 1 when any probe is marked FAIL.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from cloakbrowser import launch_context_async

PROBES = (Path(__file__).resolve().parent.parent / "cloakbrowser_mcp" / "probes.js").read_text(encoding="utf-8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--proxy")
    ap.add_argument("--locale")
    ap.add_argument("--tz")
    ap.add_argument("--geoip", action="store_true")
    ap.add_argument("--site", default="https://example.com/")
    ap.add_argument("--no-humanize", action="store_true")
    a = ap.parse_args()
    kw = {"headless": not a.headed, "humanize": not a.no_humanize, "geoip": a.geoip, "proxy": a.proxy, "locale": a.locale, "timezone": a.tz}
    kw = {k: v for k, v in kw.items() if v is not None and v is not False or k in ("headless", "humanize")}
    ctx = await launch_context_async(**kw)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(a.site, wait_until="domcontentloaded")
        res = await page.evaluate(PROBES, {"network": True})
        print(json.dumps({"config": {k: v for k, v in kw.items() if k != "proxy"}, "result": res}, ensure_ascii=False, indent=1))
        return 1 if any(f.get("verdict") == "FAIL" for f in res.get("findings", [])) else 0
    finally:
        await ctx.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
