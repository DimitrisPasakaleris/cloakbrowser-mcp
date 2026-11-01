"""One-off experiment: which launch setting removes WebRTC ICE candidates? Run: .venv/Scripts/python.exe audit/experiments.py"""
import asyncio
import json

from cloakbrowser import launch_context_async

JS = """async () => { const pc = new RTCPeerConnection({}); const c = []; pc.onicecandidate = e => { if (e.candidate) c.push(e.candidate.type); }; pc.createDataChannel('x'); await pc.setLocalDescription(await pc.createOffer()); await new Promise(r => setTimeout(r, 2000)); pc.close(); return c.length + ' ' + (pc.localDescription ? 'sdp' : ''); }"""


async def run(label, **kw):
    ctx = await launch_context_async(headless=True, humanize=False, **kw)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://example.com/", wait_until="domcontentloaded")
        print(label, await page.evaluate(JS))
    finally:
        await ctx.close()


async def main():
    await run("stealth_args=False        ", stealth_args=False)
    await run("only --fingerprint seed   ", stealth_args=False, args=["--fingerprint=4242"])
    await run("seed + platform=windows   ", stealth_args=False, args=["--fingerprint=4242", "--fingerprint-platform=windows"])
    await run("default (control)         ")


asyncio.run(main())
