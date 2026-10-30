"""`cloakbrowser-mcp stats`: where the tokens go.

Two costs reach the model: (1) the tool *definitions*, re-sent on every turn, and (2) each tool *result*.
The server appends one line per call to ~/.cloakbrowser-mcp/logs/usage.jsonl (tool name and result size only;
never URLs, arguments or page text). Token figures are estimates: characters / 4 for text, a flat guess for images.
Set CLOAK_USAGE_LOG=0 to stop logging.
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path


def usage_files() -> list[Path]:
    home = Path(os.environ.get("CLOAK_MCP_HOME", Path.home() / ".cloakbrowser-mcp"))
    log = home / "logs" / "usage.jsonl"
    return [p for p in (log.with_suffix(".jsonl.1"), log) if p.exists()]


def load(days: float | None = None, files: list[Path] | None = None) -> list[dict]:
    cutoff = time.time() - days * 86400 if days else 0
    rows = []
    for f in usage_files() if files is None else files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("t", 0) >= cutoff and "tool" in r:
                rows.append(r)
    return rows


def summarize(rows: list[dict]) -> dict:
    per: dict[str, dict] = {}
    for r in rows:
        d = per.setdefault(r["tool"], {"calls": 0, "tokens": 0, "max": 0, "errors": 0, "ms": 0})
        d["calls"] += 1
        d["tokens"] += r.get("tokens", 0)
        d["max"] = max(d["max"], r.get("tokens", 0))
        d["errors"] += 1 if r.get("err") else 0
        d["ms"] += r.get("ms", 0)
    for d in per.values():
        d["avg"] = d["tokens"] // d["calls"]
    total = {"calls": sum(d["calls"] for d in per.values()), "tokens": sum(d["tokens"] for d in per.values())}
    return {"tools": dict(sorted(per.items(), key=lambda kv: -kv[1]["tokens"])), "total": total}


def hints(summary: dict) -> list[str]:
    """Plain-language suggestions based on what the log shows."""
    out, tools = [], summary["tools"]
    total = summary["total"]["tokens"] or 1
    for name, d in tools.items():
        share = d["tokens"] / total
        if name in ("get_html", "get_response_body", "get_text") and share > 0.2:
            out.append(f"{name} is {share:.0%} of result tokens: prefer find_in_page, get_markdown(links='none') or read_url(query=...).")
        if name == "screenshot" and d["calls"] >= 5:
            out.append(f"{d['calls']} inline screenshots (~{d['tokens']:,} tokens): use snapshot/find_in_page, or screenshot_file.")
        if name == "snapshot" and d["avg"] > 1500:
            out.append("snapshot averages over 1.5k tokens: pass scope='#css' or a smaller max_items.")
    nav, read = tools.get("navigate", {}).get("calls", 0), tools.get("read_url", {}).get("calls", 0)
    if nav > 5 and nav > read:
        out.append(f"{nav} navigate calls vs {read} read_url: read_url opens and reads in one call (fewer round trips).")
    return out


async def _definition_sizes() -> list[tuple[str, int, int]]:
    """(label, tool count, estimated tokens) of the tool definitions each profile sends on every turn."""
    from mcp.server.fastmcp import FastMCP

    from . import server

    rows = []
    for label, profile, mem_on in (
        ("lean", "lean", False),
        ("standard", "standard", False),
        ("full (memory off)", "full", False),
        ("full (memory on)", "full", True),
    ):
        tmp = FastMCP("sizing")
        for name, fn in server.ALL_TOOLS.items():
            if server.tool_enabled(name, profile=profile, memory_on=mem_on, readonly=False):
                tmp.add_tool(fn, annotations=server._annotations(name))
                server._slim_schema(tmp._tool_manager.get_tool(name).parameters)
        tools = await tmp.list_tools()
        chars = sum(len(json.dumps({"name": t.name, "description": t.description, "inputSchema": t.inputSchema})) for t in tools)
        rows.append((label, len(tools), chars // 4))
    return rows


def render(summary: dict, days: float | None, defs=None, top: int = 12) -> str:
    lines = []
    span = f"last {days:g} days" if days else "all recorded calls"
    t = summary["total"]
    lines.append(f"Tool results, {span}: {t['calls']} calls, ~{t['tokens']:,} estimated tokens")
    if summary["tools"]:
        lines.append(f"{'tool':<22}{'calls':>6}{'tokens':>10}{'avg':>7}{'max':>7}{'errors':>8}")
        for name, d in list(summary["tools"].items())[:top]:
            lines.append(f"{name:<22}{d['calls']:>6}{d['tokens']:>10,}{d['avg']:>7,}{d['max']:>7,}{d['errors']:>8}")
        if len(summary["tools"]) > top:
            lines.append(f"... and {len(summary['tools']) - top} more tools")
    else:
        lines.append("(nothing logged yet; use the server for a while, then run this again)")
    for h in hints(summary):
        lines.append(f"hint: {h}")
    if defs:
        lines.append("")
        lines.append("Tool definitions, re-sent on EVERY turn (set CLOAK_TOOLS to pick one):")
        for label, n, tokens in defs:
            lines.append(f"  {label:<20}{n:>3} tools  ~{tokens:,} tokens")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="cloakbrowser-mcp stats", description="Where the tokens go (estimates).")
    ap.add_argument("--days", type=float, default=None, help="only the last N days")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-defs", action="store_true", help="skip the tool-definition sizes (faster)")
    ap.add_argument("--reset", action="store_true", help="delete the usage log")
    ns = ap.parse_args(argv)
    if ns.reset:
        for f in usage_files():
            f.unlink()
        print("usage log deleted")
        return 0
    summary = summarize(load(ns.days))
    defs = None if ns.no_defs else asyncio.run(_definition_sizes())
    if ns.json:
        print(json.dumps({"results": summary, "definitions": [dict(profile=l, tools=n, tokens=t) for l, n, t in defs or []]}, indent=2))
    else:
        print(render(summary, ns.days, defs))
    return 0
