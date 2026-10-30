"""Token benchmark: the old way of reading a page versus the new defaults, on real URLs.

    python bench_tokens.py https://example.com https://www.vinci.com/en/finance

Token figures are estimates (characters / 4 for text; width x height / 750 for images). The "old" column reproduces what a
client had to do before: navigate, get_markdown(20000 chars, whole page), snapshot(150 items) and a full-size PNG.
"""
import asyncio
import json
import os
import sys
import tempfile

os.environ.setdefault("CLOAK_MCP_HOME", tempfile.mkdtemp(prefix="cloakbench_"))
os.environ["CLOAK_USAGE_LOG"] = "0"
os.environ["CLOAK_MEMORY_TOOLS"] = "all"

from cloakbrowser_mcp import server as s  # noqa: E402
from cloakbrowser_mcp import stats  # noqa: E402


def tok(x) -> int:
    if hasattr(x, "data"):
        w, h = s._image_size(x.data)
        return max(100, min(1600, w * h // 750)) if w else 1000
    return (len(x) + 3) // 4


async def one(url: str) -> dict:
    out = {"url": url}
    # old way: three calls, whole page, big snapshot, full-size PNG
    old_nav = await s.navigate(url)
    old_md = await s.get_markdown(main_only=False, max_chars=20000, links="keep")
    old_snap = await s._snapshot(150, "", False)
    old_img = await s.screenshot(format="png", max_width=0)
    out["old"] = {"calls": 3, "tokens": tok(old_nav) + tok(old_md) + tok(old_snap), "image": tok(old_img)}
    # new way: one call
    new = await s.read_url(url)
    img = await s.screenshot()
    out["new"] = {"calls": 1, "tokens": tok(new), "image": tok(img)}
    # new way, only looking for a fact
    found = await s.read_url(url, query="results OR revenue OR price OR contact", )
    out["query"] = {"calls": 1, "tokens": tok(found)}
    out["blocked"] = "BLOCKED?" in new
    return out


async def main(urls):
    rows = []
    try:
        for u in urls:
            try:
                rows.append(await one(u))
            except Exception as e:  # keep going on a flaky site
                print(f"skip {u}: {type(e).__name__}: {str(e)[:100]}")
    finally:
        await s.close_browser()
    defs = {label: (n, t) for label, n, t in await stats._definition_sizes()}
    print(f"{'page':<46}{'old (3 calls)':>15}{'read_url':>10}{'saved':>7}{'find':>7}{'img old':>9}{'img new':>9}")
    to = tn = 0
    for r in rows:
        o, n = r["old"]["tokens"], r["new"]["tokens"]
        to, tn = to + o, tn + n
        print(f"{r['url'][:44]:<46}{o:>15,}{n:>10,}{1 - n / o:>7.0%}{r['query']['tokens']:>7,}{r['old']['image']:>9,}{r['new']['image']:>9,}")
    if rows:
        print(f"{'TOTAL':<46}{to:>15,}{tn:>10,}{1 - tn / to:>7.0%}")
    print("\nTool definitions sent on every turn:")
    for label, (n, t) in defs.items():
        print(f"  {label:<20}{n:>3} tools  ~{t:,} tokens")
    print(json.dumps({"pages": rows}, indent=1))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["https://example.com"]))
