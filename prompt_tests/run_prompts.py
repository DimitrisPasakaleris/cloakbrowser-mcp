"""Run the ten CloakBrowser prompts through a real LLM agent (Codex) and check the results against ground truth.

    python prompt_tests/run_prompts.py                 run all ten
    python prompt_tests/run_prompts.py --only 4,8      run some
    python prompt_tests/run_prompts.py --markdown      print the prompts (paste them into any agent by hand)
    python prompt_tests/run_prompts.py --server-command C:\\path\\to\\other\\cloakbrowser-mcp.exe   A/B another build

Each prompt gets its own empty CLOAK_MCP_HOME (so memory starts from nothing, i.e. the fresh-install defaults), and the
agent's tool calls come from Codex's JSON event stream, so the checks see exactly what the tools returned. Codex usage is
consumed; the report shows how much (input tokens include Codex's own context and are mostly cached)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

os.environ.setdefault("CLOAK_MEMORY_DEFAULT", "on")
from prompts import PROMPTS, Ctx, markdown  # noqa: E402
import serve_fixtures  # noqa: E402


def find_codex() -> str:
    pats = [os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe"), os.path.expanduser("~/.codex/bin/codex*")]
    found = sorted((f for p in pats for f in glob.glob(p)), key=os.path.getmtime, reverse=True)
    if not found:
        sys.exit("codex not found: pass --codex PATH")
    return found[0]


def reachable(host: str) -> bool:
    try:
        socket.create_connection((host, 443), timeout=6).close()
        return True
    except OSError:
        return False


def default_server_command() -> str:
    exe = HERE.parent / ".venv" / "Scripts" / "cloakbrowser-mcp.exe"
    return str(exe) if exe.exists() else "cloakbrowser-mcp"


def posix(p) -> str:
    return str(p).replace("\\", "/")


# ---------------------------------------------------------------- ground truth that needs a browser (computed before the agent runs)
def truth_for(p, base: str) -> dict:
    if p.id != 6:
        return {}
    home = tempfile.mkdtemp(prefix="cloaktruth_")
    code = (
        "import asyncio, json, os\n"
        f"os.environ['CLOAK_MCP_HOME']={home!r}; os.environ['CLOAK_MEMORY_DEFAULT']='off'; os.environ['CLOAK_USAGE_LOG']='0'\n"
        "from cloakbrowser_mcp import server as s\n"
        "async def main():\n"
        "    await s.navigate('https://en.wikipedia.org/wiki/Vinci_SA')\n"
        "    page = await s._ensure()\n"
        "    md = await s._page_markdown(page, 'body', True, 'keep')\n"
        "    await s.close_browser()\n"
        "    print(json.dumps({'md': md}))\n"
        "asyncio.run(main())\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}, cwd=str(HERE.parent))
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


# ---------------------------------------------------------------- one run
def run_codex(codex, prompt_text, home, env, server_command, timeout, workdir):
    cfg = [f'mcp_servers.cloakbrowser.command="{posix(server_command)}"', 'mcp_servers.cloakbrowser.default_tools_approval_mode="approve"']
    for k, v in {"CLOAK_MCP_HOME": posix(home), "CLOAK_USAGE_LOG": "1", "PYTHONUTF8": "1", **env}.items():
        cfg.append(f'mcp_servers.cloakbrowser.env.{k}="{v}"')
    cmd = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "--json", "-C", str(workdir)]
    for c in cfg:
        cmd += ["-c", c]
    cmd.append(prompt_text)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        out, rc = r.stdout, r.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = (e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")), -1
    events = []
    for line in out.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    calls, answer, usage = [], "", {}
    for ev in events:
        it = ev.get("item") or {}
        if ev.get("type") == "item.completed" and it.get("type") == "mcp_tool_call" and it.get("server") == "cloakbrowser":
            res = it.get("result") or {}
            text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict) and c.get("type") == "text")
            if it.get("error") and not text:
                text = f"ERROR: {it['error']}"
            calls.append({"tool": it.get("tool"), "arguments": it.get("arguments") or {}, "result": text})
        elif ev.get("type") == "item.completed" and it.get("type") == "agent_message":
            answer = it.get("text", "")
        elif ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                usage[k] = usage.get(k, 0) + u.get(k, 0)
    return {"calls": calls, "answer": answer, "codex_usage": usage, "rc": rc, "seconds": round(time.time() - t0, 1), "events": len(events)}


def usage_rows(home: Path) -> list:
    f = Path(home) / "logs" / "usage.jsonl"
    if not f.exists():
        return []
    rows = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except ValueError:
            pass
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of prompt numbers")
    ap.add_argument("--codex", default="", help="path to codex")
    ap.add_argument("--server-command", default="", help="cloakbrowser-mcp executable to test (default: this repo's .venv)")
    ap.add_argument("--timeout", type=int, default=480, help="seconds per prompt")
    ap.add_argument("--port", type=int, default=0, help="fixture server port (0 = any)")
    ap.add_argument("--markdown", action="store_true", help="print the prompts and exit")
    ap.add_argument("--report", default="", help="write the JSON report here")
    ns = ap.parse_args()
    if ns.markdown:
        print(markdown())
        return 0

    from cloakbrowser_mcp.memory import Memory  # after sys.path is set

    codex = ns.codex or find_codex()
    server_command = ns.server_command or default_server_command()
    want = {int(x) for x in ns.only.split(",") if x.strip()} or {p.id for p in PROMPTS}
    srv, base = serve_fixtures.start(ns.port)
    root = Path(tempfile.mkdtemp(prefix="cloakprompts_"))
    workdir = root / "work"
    workdir.mkdir()
    print(f"codex: {codex}\nserver: {server_command}\nfixtures: {base}\nhomes: {root}\n")
    report = []
    for p in PROMPTS:
        if p.id not in want:
            continue
        print(f"[{p.id:>2}] {p.title}")
        down = [h for h in p.needs_net if not reachable(h)]
        if down:
            print(f"      SKIPPED: cannot reach {', '.join(down)}\n")
            report.append({"id": p.id, "title": p.title, "status": "skipped", "reason": f"unreachable: {down}"})
            continue
        home = root / f"p{p.id}"
        home.mkdir()
        env = {"CLOAK_ALLOW_PRIVATE": "1", "CLOAK_MEMORY_ALLOW_IP": "1"} if p.fixtures else {}
        truth = truth_for(p, base)
        t_start = time.time() - 1
        res = run_codex(codex, p.text.replace("{base}", base), home, env, server_command, ns.timeout, workdir)
        mem = Memory(home / "memory", env={}, allow_ip=True, default_on=True)
        ctx = Ctx(answer=res["answer"], calls=res["calls"], usage=[u for u in usage_rows(home) if u.get("t", 0) >= t_start], mem=mem, home=home, base=base, truth=truth)
        try:
            checks = p.check(ctx)
        except Exception as e:  # a broken check is a failed test, with the reason
            checks = [("check crashed", False, f"{type(e).__name__}: {e}")]
        passed = all(c[1] for c in checks) and res["rc"] == 0
        cloak_tokens = sum(u.get("tokens", 0) for u in ctx.usage)
        for label, good, detail in checks:
            print(f"      {'ok  ' if good else 'FAIL'} {label}" + (f"   [{detail}]" if not good and detail else ""))
        u = res["codex_usage"]
        print(f"      -> {'PASS' if passed else 'FAIL'} | {len(res['calls'])} cloakbrowser calls, ~{cloak_tokens:,} result tokens, "
              f"codex in={u.get('input_tokens', 0):,} (cached {u.get('cached_input_tokens', 0):,}) out={u.get('output_tokens', 0):,}, {res['seconds']}s")
        if not passed:
            print(f"      answer: {res['answer'][:300]!r}")
        print()
        report.append({
            "id": p.id, "title": p.title, "status": "pass" if passed else "fail", "seconds": res["seconds"], "rc": res["rc"],
            "tool_calls": [c["tool"] for c in res["calls"]], "result_tokens_est": cloak_tokens, "codex_usage": u,
            "checks": [{"label": a, "ok": bool(b), "detail": d} for a, b, d in checks], "answer": res["answer"],
        })
        try:
            mem.store().close()
        except Exception:
            pass
    srv.shutdown()
    done = [r for r in report if r["status"] != "skipped"]
    n_pass = sum(r["status"] == "pass" for r in done)
    print("=" * 78)
    print(f"{'#':>2}  {'result':<7}{'calls':>6}{'result tokens':>15}{'codex input':>13}{'seconds':>9}  title")
    for r in report:
        if r["status"] == "skipped":
            print(f"{r['id']:>2}  {'skipped':<7}{'':>6}{'':>15}{'':>13}{'':>9}  {r['title']}")
        else:
            print(f"{r['id']:>2}  {r['status'].upper():<7}{len(r['tool_calls']):>6}{r['result_tokens_est']:>15,}{r['codex_usage'].get('input_tokens', 0):>13,}{r['seconds']:>9}  {r['title']}")
    print(f"\n{n_pass}/{len(done)} passed" + (f", {len(report) - len(done)} skipped" if len(done) != len(report) else ""))
    out = Path(ns.report) if ns.report else root / "report.json"
    out.write_text(json.dumps({"codex": codex, "server": server_command, "results": report}, indent=2), encoding="utf-8")
    print(f"report: {out}")
    return 0 if n_pass == len(done) and done else 1


if __name__ == "__main__":
    sys.exit(main())
