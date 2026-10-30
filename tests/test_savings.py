"""Token-saving features: paging, main-content markdown, find_in_page, read_url, run_steps, snapshot diff,
JPEG screenshots, tool profiles, schema slimming, usage log and the stats command."""
import json
import re

import pytest
from helpers import U

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ------------------------------------------------------------------ paging
async def test_clip_pages_with_offset(srv):
    text = "".join(str(i % 10) for i in range(1000))
    first = srv._clip(text, 100)
    assert first.startswith(text[:100]) and "chars 0-100 of 1000" in first and "offset=100" in first
    mid = srv._clip(text, 100, offset=900)
    assert mid.startswith(text[900:1000]) and "end: chars 900-1000 of 1000" in mid
    assert srv._clip("short", 100) == "short"  # nothing to say when it all fits
    assert srv._clip(text, 100, offset=5000).startswith("\n...[end")  # past the end is harmless
    assert srv._clip(None, 10) == ""


async def test_get_markdown_pages_and_defaults(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    small = U(await srv.get_markdown(max_chars=300))
    assert "offset=300" in small
    nxt = U(await srv.get_markdown(max_chars=300, offset=300))
    assert nxt.startswith(U(await srv.get_markdown(max_chars=100000))[300:330])
    assert srv.DEFAULT_CHARS <= 6000  # the default read stays small


# ------------------------------------------------------------------ main-content markdown
async def test_main_only_drops_chrome(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    md = U(await srv.get_markdown())
    assert "# Real Article" in md and "Needle" in md
    for noise in ("Nav One", "Logo Home", "Footer legal", "We use cookies", "Related junk"):
        assert noise not in md, noise
    full = U(await srv.get_markdown(main_only=False))
    for kept in ("Nav One", "Logo Home", "Footer legal", "We use cookies", "Related junk"):
        assert kept in full, kept
    assert len(md) < len(full)


async def test_markdown_link_modes(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    assert "[Source link](/x?utm=1&id=7#frag)" in U(await srv.get_markdown(links="keep"))
    short = U(await srv.get_markdown(links="short"))
    assert "[Source link](/x)" in short and "utm" not in short
    none = U(await srv.get_markdown(links="none"))
    assert "Source link" in none and "](" not in none
    assert "ERROR" in await srv.get_markdown(links="bogus")


async def test_main_only_falls_back_on_thin_pages(srv, base_url):
    await srv.navigate(base_url + "/tiny.html")
    assert "Short page" in U(await srv.get_markdown())  # nothing is lost when there is no <main>


async def test_main_only_keeps_explicit_selector(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    assert "Nav One" in U(await srv.get_markdown(selector="nav"))


# ------------------------------------------------------------------ find_in_page
async def test_find_in_page(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    r = U(await srv.find_in_page("needle", context=30))
    assert "1 match(es)" in r and "The Needle sits in the middle" in r
    assert len(r) < 300
    assert "no match" in await srv.find_in_page("zzz-not-here")
    many = U(await srv.find_in_page("Paragraph", context=10, max_matches=3))
    assert "showing" in many and many.count("@") <= 3
    rx = U(await srv.find_in_page(r"Paragraph (2\d):", regex=True, context=5, max_matches=20))
    assert "Paragraph 20:" in rx and "Paragraph 29:" in rx
    assert "ERROR" in await srv.find_in_page("(", regex=True)


# ------------------------------------------------------------------ read_url
async def test_read_url_one_call(srv, base_url):
    out = await srv.read_url(base_url + "/article.html")
    assert "status=200" in out and "title=Article" in out and "# Real Article" in out
    assert "Nav One" not in out and "Footer legal" not in out
    assert "<untrusted_page_content>" in out


async def test_read_url_cheaper_than_navigate_plus_read(srv, base_url):
    classic = await srv.navigate(base_url + "/article.html") + await srv.get_markdown(main_only=False, max_chars=20000)
    assert len(await srv.read_url(base_url + "/article.html", max_chars=6000)) < len(classic)


async def test_read_url_query_and_refs_and_modes(srv, base_url):
    q = await srv.read_url(base_url + "/article.html", query="Needle")
    assert "match(es)" in q and "# Real Article" not in q
    text = await srv.read_url(base_url + "/article.html", mode="text")
    assert "Real Article" in text and "Logo Home" in text  # text mode is the raw page text
    refs = await srv.read_url(base_url + "/index.html", with_refs=5)
    assert re.search(r"\[e\d+\] ", refs) and "Test Shop" in refs
    assert "ERROR" in await srv.read_url(base_url + "/article.html", mode="pdf")


async def test_read_url_blocked_and_policy(srv, base_url, monkeypatch):
    r = await srv.read_url(base_url + "/challenge")
    assert "BLOCKED?" in r and "<untrusted_page_content>" not in r  # no body is dumped for a challenge page
    monkeypatch.setitem(srv.POLICY, "allow_private", False)
    assert srv._policy_check(base_url + "/x") is not None
    assert (await srv.read_url(base_url + "/article.html")).startswith("ERROR")
    assert (await srv.read_url("file:///etc/passwd")).startswith("ERROR")


# ------------------------------------------------------------------ run_steps
async def test_run_steps_batches_actions(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    out = await srv.run_steps(
        [
            {"do": "type", "target": "#name", "text": "Bob"},
            {"do": "select", "target": "#sel", "value": "y"},
            {"do": "click", "target": "#go"},
            {"do": "wait", "text": "hello Bob"},
        ],
        then="text",
        then_chars=2000,
    )
    lines = out.splitlines()
    assert lines[0].startswith("1 type:") and lines[1].startswith("2 select:") and lines[2].startswith("3 click:")
    assert lines[3].startswith("4 wait:") and "hello Bob/y" in out
    assert "ERROR" not in "\n".join(lines[:4])


async def test_run_steps_stops_on_error_and_validates(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    out = await srv.run_steps(
        [{"do": "goto", "url": "file:///etc/passwd"}, {"do": "click", "target": "#go"}], then=""
    )
    assert out.count("\n") == 0 and "1 goto: ERROR" in out  # step 2 never ran
    assert "unknown step" in await srv.run_steps([{"do": "fly"}])
    assert "unknown field" in await srv.run_steps([{"do": "click", "target": "#go", "bogus": 1}])
    assert "at most" in await srv.run_steps([{"do": "scroll"}] * (srv.MAX_STEPS + 1))
    assert (await srv.run_steps([])).startswith("ERROR")
    assert "ERROR: then" in await srv.run_steps([{"do": "scroll", "direction": "top"}], then="bogus")


async def test_run_steps_goto_back_fill_press(srv, base_url):
    out = await srv.run_steps(
        [
            {"do": "goto", "url": base_url + "/index.html"},
            {"do": "fill", "fields": {"#name": "Zed"}},
            {"do": "press", "key": "Enter"},
            {"do": "scroll", "direction": "bottom"},
            {"do": "goto", "url": base_url + "/page2.html"},
            {"do": "back"},
        ],
        then="snapshot",
    )
    assert "ERROR" not in out and "6 back:" in out and "index.html" in out
    assert re.search(r"\[e\d+\] ", out)
    blocked = await srv.run_steps([{"do": "goto", "url": "file:///etc/passwd"}])
    assert "ERROR" in blocked


async def test_run_steps_hidden_in_readonly_mode(srv):
    assert not srv.tool_enabled("run_steps", readonly=True)
    assert srv.tool_enabled("run_steps", readonly=False)
    assert srv.tool_enabled("find_in_page", readonly=True) and srv.tool_enabled("read_url", readonly=True)


# ------------------------------------------------------------------ snapshot
async def test_snapshot_reports_truncation(srv, base_url):
    await srv.navigate(base_url + "/many.html")
    snap = U(await srv.snapshot(max_items=10, diff=False))
    assert snap.count("] button") == 10 and "list cut at 10" in snap
    full = U(await srv.snapshot(max_items=200, diff=False))
    assert full.count("] button") == 60 and "list cut" not in full


async def test_snapshot_auto_diff_on_big_pages(srv, base_url):
    await srv.navigate(base_url + "/many.html")
    first = U(await srv.snapshot(max_items=200))
    assert first.count("] button") == 60  # first look is always the full list
    await srv.evaluate("() => { const b = document.createElement('button'); b.textContent = 'Late Btn'; document.body.appendChild(b); }")
    again = U(await srv.snapshot(max_items=200))  # default: changes only
    assert "Late Btn" in again and "changes only" in again and "Item 3" not in again and again.count("] button") == 1
    forced = U(await srv.snapshot(max_items=200, diff=False))
    assert forced.count("] button") == 61 and "Item 3" in forced
    await srv.navigate(base_url + "/many.html")
    assert U(await srv.snapshot(max_items=200)).count("] button") == 60  # new load of the page: no stale diff


async def test_snapshot_small_pages_stay_full(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    a, b = U(await srv.snapshot()), U(await srv.snapshot())
    assert a.count("[e") == b.count("[e") and "changes only" not in b


async def test_snapshot_shortens_long_hrefs(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    await srv.evaluate("() => { const a = document.createElement('a'); a.href = '/x?' + 'q'.repeat(300); a.textContent = 'Long'; document.body.appendChild(a); }")
    line = next(ln for ln in U(await srv.snapshot(diff=False)).splitlines() if '"Long"' in ln)
    assert len(line) < 150


# ------------------------------------------------------------------ screenshots
async def test_screenshot_formats_and_quality(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    assert (await srv.screenshot()).data[:2] == b"\xff\xd8"
    assert (await srv.screenshot(format="png")).data[:4] == b"\x89PNG"
    low, high = await srv.screenshot(quality=10, max_width=0), await srv.screenshot(quality=95, max_width=0)
    assert len(low.data) < len(high.data)
    bad = await srv.screenshot(format="gif")
    assert isinstance(bad, str) and bad.startswith("ERROR")


async def test_screenshot_is_scaled_down_by_default(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    vw = int(U(await srv.evaluate("() => innerWidth")))
    assert vw > srv.SHOT_MAX_WIDTH  # the default browser window is wider than the cap, so there is something to save
    w, h = srv._image_size((await srv.screenshot()).data)
    fw, fh = srv._image_size((await srv.screenshot(max_width=0)).data)
    assert (fw, fh) == (vw, int(U(await srv.evaluate("() => innerHeight"))))  # max_width=0 is the real size
    assert abs(w - srv.SHOT_MAX_WIDTH) <= 1 and abs(h - fh * w / fw) <= 1  # scaled, same aspect ratio
    assert w * h < 0.5 * fw * fh  # at least 2x fewer pixels, i.e. fewer image tokens
    w2, _ = srv._image_size((await srv.screenshot(format="png", max_width=500)).data)
    assert abs(w2 - 500) <= 1
    small = await srv.screenshot(max_width=vw + 100)  # already narrower than the cap: untouched
    assert srv._image_size(small.data)[0] == vw


async def test_scaled_screenshot_element_and_full_page(srv, base_url):
    await srv.navigate(base_url + "/article.html")
    el_w, _ = srv._image_size((await srv.screenshot(selector="article", max_width=300)).data)
    assert abs(el_w - 300) <= 1
    page_h = int(U(await srv.evaluate("() => document.documentElement.scrollHeight")))
    fw, fh = srv._image_size((await srv.screenshot(full_page=True, max_width=0)).data)
    sw, sh = srv._image_size((await srv.screenshot(full_page=True, max_width=600)).data)
    assert fh >= page_h - 2 and fh > 947 and abs(sw - 600) <= 1 and abs(sh - fh * sw / fw) <= 2  # whole page, scaled
    ann = await srv.screenshot(annotate=True)
    assert srv._image_size(ann.data)[0] <= srv.SHOT_MAX_WIDTH + 1
    assert U(await srv.evaluate("() => document.getElementById('__cm_overlay') === null")) == "true"


async def test_screenshot_file_follows_extension(srv, base_url):
    from pathlib import Path

    await srv.navigate(base_url + "/article.html")
    vw = int(U(await srv.evaluate("() => innerWidth")))
    first = Path(await srv.screenshot_file())
    assert first.read_bytes()[:2] == b"\xff\xd8" and srv._image_size(first.read_bytes())[0] == vw  # files stay full size
    assert Path(await srv.screenshot_file("a.png")).read_bytes()[:4] == b"\x89PNG"
    assert Path(await srv.screenshot_file("b.jpeg", full_page=True)).read_bytes()[:2] == b"\xff\xd8"
    scaled = Path(await srv.screenshot_file("c.jpg", max_width=400)).read_bytes()
    assert abs(srv._image_size(scaled)[0] - 400) <= 1


async def test_image_size_reader(srv):
    import struct

    png_hdr = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 640, 480)
    assert srv._image_size(png_hdr) == (640, 480)
    jpg = b"\xff\xd8\xff\xc0" + struct.pack(">HBHH", 17, 8, 300, 500) + b"\x00" * 12
    assert srv._image_size(jpg) == (500, 300)
    assert srv._image_size(b"") == (0, 0) and srv._image_size(b"GIF89a....") == (0, 0)


# ------------------------------------------------------------------ tool profiles and definitions
async def test_lean_profile_is_small_and_complete(srv):
    lean = {n for n in srv.ALL_TOOLS if srv.tool_enabled(n, profile="lean", readonly=False)}
    assert lean == srv.LEAN_TOOLS and len(lean) <= 20
    assert {"read_url", "run_steps", "find_in_page", "snapshot", "click", "type_text", "screenshot"} <= lean
    assert not lean & {"configure", "evaluate", "get_html"} and not any(n.startswith("memory_") for n in lean)


async def test_lean_extra_tools_env(srv, monkeypatch):
    monkeypatch.setenv("CLOAK_EXTRA_TOOLS", "configure, evaluate")
    assert srv.tool_enabled("configure", profile="lean", readonly=False)
    assert not srv.tool_enabled("get_html", profile="lean", readonly=False)


async def test_standard_and_full_profiles(srv):
    assert not srv.tool_enabled("evaluate", profile="standard", memory_on=False, readonly=False)  # debug group
    assert srv.tool_enabled("evaluate", profile="full", memory_on=False, readonly=False)
    assert srv.tool_enabled("get_html", profile="standard", memory_on=False, readonly=False)
    assert srv.tool_enabled("request_human", profile="standard", memory_on=False, readonly=False)


async def test_memory_tools_wait_for_opt_in(srv):
    off = {n for n in srv.ALL_TOOLS if n.startswith("memory_") and srv.tool_enabled(n, profile="full", memory_on=False, readonly=False)}
    assert off == {"memory_request_access"}
    on = {n for n in srv.ALL_TOOLS if n.startswith("memory_") and srv.tool_enabled(n, profile="full", memory_on=True, readonly=False)}
    assert on == {n for n in srv.ALL_TOOLS if n.startswith("memory_")} and len(on) >= 9


async def test_unknown_profile_falls_back_to_full(srv, monkeypatch):
    monkeypatch.setenv("CLOAK_TOOLS", "bogus")
    assert srv._profile() == "full"
    monkeypatch.setenv("CLOAK_TOOLS", "LEAN")
    assert srv._profile() == "lean"


async def test_memory_tools_appear_after_opt_in(srv):
    name = "memory_status"
    fn = srv.ALL_TOOLS[name]
    srv.mcp.remove_tool(name)
    srv.DEFERRED_MEMORY[name] = fn

    class Session:
        notified = 0

        async def send_tool_list_changed(self):
            Session.notified += 1

    class Ctx:
        session = Session()

    assert name not in {t.name for t in await srv.mcp.list_tools()}
    await srv._expose_memory_tools(Ctx())
    assert name in {t.name for t in await srv.mcp.list_tools()} and not srv.DEFERRED_MEMORY and Session.notified == 1
    await srv._expose_memory_tools(Ctx())  # nothing left to expose: no second notification
    assert Session.notified == 1


async def test_expose_memory_tools_survives_clients_without_notifications(srv):
    name = "memory_status"
    srv.mcp.remove_tool(name)
    srv.DEFERRED_MEMORY[name] = srv.ALL_TOOLS[name]
    await srv._expose_memory_tools(object())  # no .session at all
    assert name in {t.name for t in await srv.mcp.list_tools()}


async def test_slim_schema_drops_titles_but_keeps_properties_named_title(srv):
    schema = {
        "title": "fooArguments",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "max_chars": {"title": "Max Chars", "type": "integer", "default": 5},
            "items": {"title": "Items", "type": "array", "items": {"title": "Item", "type": "string"}},
        },
        "required": ["title"],
    }
    srv._slim_schema(schema)
    assert "title" not in schema
    assert set(schema["properties"]) == {"title", "max_chars", "items"}
    assert all("title" not in v for v in schema["properties"].values()) and "title" not in schema["properties"]["items"]["items"]
    assert schema["properties"]["max_chars"]["default"] == 5


async def test_registered_schemas_have_no_titles(srv):
    for t in await srv.mcp.list_tools():
        stripped = json.dumps(t.inputSchema)
        assert '"title"' not in stripped, t.name


async def test_definitions_shrink_with_profiles():
    from cloakbrowser_mcp import stats

    rows = {label: (n, tokens) for label, n, tokens in await stats._definition_sizes()}
    assert rows["lean"][1] < rows["standard"][1] < rows["full (memory off)"][1] < rows["full (memory on)"][1]
    assert rows["lean"][0] <= 20 and rows["lean"][1] < 0.5 * rows["full (memory on)"][1]


# ------------------------------------------------------------------ usage log + stats
@pytest.fixture
def usage(srv, tmp_path, monkeypatch):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(srv, "USAGE_LOG", path)
    monkeypatch.setenv("CLOAK_USAGE_LOG", "1")
    return path


async def test_usage_log_records_sizes_not_content(srv, base_url, usage):
    await srv.navigate(base_url + "/article.html")
    await srv.get_markdown()
    await srv.navigate("file:///etc/passwd")  # an error result (blocked scheme), returned instantly
    rows = [json.loads(ln) for ln in usage.read_text(encoding="utf-8").splitlines()]
    assert [r["tool"] for r in rows] == ["navigate", "get_markdown", "navigate"]
    assert rows[1]["tokens"] > 50 and rows[1]["chars"] > 200 and rows[2]["err"] is True and rows[1]["err"] is False
    raw = usage.read_text(encoding="utf-8")
    assert "article.html" not in raw and "Real Article" not in raw and base_url not in raw  # no URLs or page text


async def test_usage_log_can_be_disabled_and_rotates(srv, base_url, usage, monkeypatch):
    monkeypatch.setenv("CLOAK_USAGE_LOG", "0")
    await srv.get_text()
    assert not usage.exists()
    monkeypatch.setenv("CLOAK_USAGE_LOG", "1")
    monkeypatch.setattr(srv, "USAGE_MAX_BYTES", 50)
    for _ in range(4):
        await srv.get_text()
    assert usage.with_suffix(".jsonl.1").exists() and usage.stat().st_size < 400


async def test_image_results_are_estimated(srv, base_url, usage):
    await srv.navigate(base_url + "/article.html")
    img = await srv.screenshot()
    w, h = srv._image_size(img.data)
    last = lambda: json.loads(usage.read_text(encoding="utf-8").splitlines()[-1])
    row = last()
    assert row["tool"] == "screenshot" and row["chars"] == len(img.data)
    assert row["tokens"] == min(1600, max(100, w * h // 750))  # billed by pixels, not by bytes
    await srv.screenshot(max_width=0)
    assert last()["tokens"] > row["tokens"]  # the full-size capture is estimated as costlier


async def test_stats_summarize_render_and_hints():
    from cloakbrowser_mcp import stats

    now = 10_000_000_000
    rows = (
        [{"t": now, "tool": "get_html", "chars": 40000, "tokens": 10000, "ms": 10, "err": False}] * 3
        + [{"t": now, "tool": "screenshot", "chars": 90000, "tokens": 1200, "ms": 10, "err": False}] * 5
        + [{"t": now, "tool": "navigate", "chars": 100, "tokens": 25, "ms": 10, "err": True}] * 6
    )
    s = stats.summarize(rows)
    assert list(s["tools"])[0] == "get_html" and s["tools"]["get_html"]["avg"] == 10000
    assert s["total"] == {"calls": 14, "tokens": 30000 + 6000 + 150}
    assert s["tools"]["navigate"]["errors"] == 6
    text = stats.render(s, days=7, defs=[("lean", 18, 2000)])
    assert "14 calls" in text and "get_html" in text and "lean" in text and "last 7 days" in text
    hs = " ".join(stats.hints(s))
    assert "find_in_page" in hs and "screenshot" in hs and "read_url" in hs
    assert stats.hints(stats.summarize([])) == []


async def test_stats_cli_end_to_end(tmp_path, monkeypatch, capsys):
    from cloakbrowser_mcp import stats

    monkeypatch.setenv("CLOAK_MCP_HOME", str(tmp_path))
    logdir = tmp_path / "logs"
    logdir.mkdir()
    import time

    recent, old = int(time.time()), int(time.time()) - 40 * 86400
    (logdir / "usage.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"t": recent, "tool": "get_markdown", "chars": 4000, "tokens": 1000, "ms": 5, "err": False}),
                json.dumps({"t": old, "tool": "get_html", "chars": 40000, "tokens": 10000, "ms": 5, "err": False}),
                "not json at all",
            ]
        ),
        encoding="utf-8",
    )
    assert stats.main(["--no-defs", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"]["total"]["calls"] == 2
    assert stats.main(["--no-defs", "--json", "--days", "7"]) == 0
    assert json.loads(capsys.readouterr().out)["results"]["total"]["calls"] == 1
    assert stats.main(["--no-defs"]) == 0 and "get_markdown" in capsys.readouterr().out
    assert stats.main(["--reset"]) == 0 and not (logdir / "usage.jsonl").exists()
    assert stats.main(["--no-defs"]) == 0 and "nothing logged" in capsys.readouterr().out
