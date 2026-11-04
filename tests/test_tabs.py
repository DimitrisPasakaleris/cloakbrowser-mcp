"""Multi-tab tools: tab_id targeting, open_many, contact_sheet, scroll_collect, inspect_posts."""
import asyncio
import inspect
import json
import time

import pytest
from helpers import U

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _two_tabs(srv, base_url):
    """Tab A on index.html, tab B on page2.html (B is current). Returns (a, b)."""
    await srv.navigate(base_url + "/index.html")
    a = srv.S.cur
    await srv.tab_new(base_url + "/page2.html")
    return a, srv.S.cur


async def _close(srv, *ids):
    for i in ids:
        if i in srv.S.pages:
            await srv.tab_close(i)


# ---------------------------------------------------------------- tab_id targeting
async def test_tab_id_reads_another_tab_without_switching(srv, base_url):
    a, b = await _two_tabs(srv, base_url)
    try:
        assert "Test Shop" in await srv.get_text(tab_id=a)
        assert "Page two text" in await srv.get_text()  # the current tab is still B
        assert srv.S.cur == b
        assert "Page two text" in await srv.get_text(tab_id=b)
    finally:
        await _close(srv, b)


async def test_tab_id_unknown_tab_is_a_clear_error(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    r = await srv.get_text(tab_id=9999)
    assert r.startswith("ERROR") and "no such tab 9999" in r


async def test_concurrent_calls_each_hit_their_own_tab(srv, base_url):
    a, b = await _two_tabs(srv, base_url)
    try:
        res = await asyncio.gather(*[srv.get_text(tab_id=(a if i % 2 == 0 else b)) for i in range(12)])
        for i, r in enumerate(res):
            assert ("Test Shop" in r) if i % 2 == 0 else ("Page two text" in r), (i, r[:80])
        assert srv.S.cur == b  # nothing flipped the shared current tab
    finally:
        await _close(srv, b)


async def test_tab_id_works_for_screenshot_snapshot_and_evaluate(srv, base_url):
    a, b = await _two_tabs(srv, base_url)
    try:
        img = await srv.screenshot(tab_id=a)
        assert img.data and len(img.data) > 500
        assert "Next page" in await srv.snapshot(tab_id=a)
        assert "Next page" not in await srv.snapshot(tab_id=b)
        assert json.loads(U(await srv.evaluate("document.title", tab_id=a))) == "Test Shop"
        assert json.loads(U(await srv.evaluate("document.title", tab_id=b))) != "Test Shop"
        links = json.loads(U(await srv.extract_links(pattern="page2", tab_id=a)))
        assert links and links[0]["href"].endswith("/page2.html")
        res = json.loads(U(await srv.extract_structured("li.card", {"name": "h2"}, tab_id=a)))
        assert res["count"] == 3
        assert srv.S.cur == b
    finally:
        await _close(srv, b)


async def test_snapshot_state_is_kept_per_tab(srv, base_url):
    a, b = await _two_tabs(srv, base_url)
    try:
        await srv.snapshot(tab_id=a)
        await srv.snapshot(tab_id=b)
        assert a in srv.S.last_snap and b in srv.S.last_snap
        assert srv.S.last_snap[a][0] != srv.S.last_snap[b][0]
    finally:
        await _close(srv, b)


async def test_tab_aware_tools_all_declare_tab_id(srv):
    for name in srv.TAB_AWARE:
        assert "tab_id" in inspect.signature(srv.ALL_TOOLS[name]).parameters, name
    # tab_select / tab_close take a tab_id with their own meaning and must not be treated as "act on that tab"
    assert not srv.TAB_AWARE & {"tab_select", "tab_close"}


# ---------------------------------------------------------------- open_many
async def test_open_many_opens_tabs_in_order_and_keeps_current_tab(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    cur = srv.S.cur
    before = set(srv.S.pages)
    urls = [base_url + p for p in ("/page2.html", "/article.html", "/product.html", "/index.html")]
    rows = json.loads(U(await srv.open_many(urls, parallel=3)))
    try:
        assert [r["url"] for r in rows] == urls
        assert all(r["status"] == 200 and r["tab_id"] in srv.S.pages for r in rows)
        assert len({r["tab_id"] for r in rows}) == 4
        assert srv.S.cur == cur  # open_many never changes the current tab
        assert set(srv.S.pages) - before == {r["tab_id"] for r in rows}
        # each tab really holds its own page
        assert "Page two text" in await srv.get_text(tab_id=rows[0]["tab_id"])
        assert "Test Shop" in await srv.get_text(tab_id=rows[3]["tab_id"])
    finally:
        await _close(srv, *[r["tab_id"] for r in rows])


async def test_open_many_policy_refusals_get_an_error_entry_and_no_tab(srv, base_url):
    before = set(srv.S.pages)
    rows = json.loads(U(await srv.open_many([base_url + "/page2.html", "file:///c:/windows/win.ini", "javascript:alert(1)"])))
    try:
        assert rows[0]["status"] == 200
        assert "error" in rows[1] and "tab_id" not in rows[1]
        assert "error" in rows[2] and "tab_id" not in rows[2]
        assert len(set(srv.S.pages) - before) == 1
    finally:
        await _close(srv, *[r["tab_id"] for r in rows if "tab_id" in r])


async def test_open_many_flags_a_challenge_page(srv, base_url):
    rows = json.loads(U(await srv.open_many([base_url + "/challenge", base_url + "/page2.html"])))
    try:
        assert rows[0]["status"] == 403 and rows[0]["blocked"] and "HTTP 403" in rows[0]["blocked"]
        assert rows[1]["blocked"] is None
    finally:
        await _close(srv, *[r["tab_id"] for r in rows])


async def test_open_many_limits(srv, base_url):
    assert (await srv.open_many([])).startswith("ERROR")
    r = await srv.open_many([base_url + "/page2.html"] * (srv.MANY_MAX + 1))
    assert r.startswith("ERROR") and "at most" in r


async def test_open_many_twelve_tabs_in_parallel(srv, base_url):
    rows = json.loads(U(await srv.open_many([base_url + f"/page2.html?n={i}" for i in range(12)], parallel=4)))
    try:
        assert len(rows) == 12 and all(r["status"] == 200 for r in rows)
        assert [r["url"].rsplit("=", 1)[1] for r in rows] == [str(i) for i in range(12)]
    finally:
        await _close(srv, *[r["tab_id"] for r in rows])


# ---------------------------------------------------------------- contact_sheet
async def _pixels(srv, img, points):
    """RGB at each (x, y) of a JPEG returned by a tool, decoded by the browser itself (no imaging library needed)."""
    import base64
    b64 = base64.b64encode(img.data).decode()
    script = (
        "async () => { const im = new Image(); im.src = 'data:image/jpeg;base64," + b64 + "'; await im.decode();"
        "const c = document.createElement('canvas'); c.width = im.width; c.height = im.height;"
        "const x = c.getContext('2d'); x.drawImage(im, 0, 0);"
        f"return {json.dumps(points)}.map(([a, b]) => Array.from(x.getImageData(a, b, 1, 1).data).slice(0, 3)); }}"
    )
    return json.loads(U(await srv.evaluate(script)))


def _is(rgb, want, tol=45):
    return all(abs(a - b) <= tol for a, b in zip(rgb, want))


RED, GREEN, BLUE, WHITE = (224, 0, 0), (0, 160, 0), (0, 0, 224), (255, 255, 255)


async def test_contact_sheet_puts_each_tab_in_its_own_cell(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    cur, before = srv.S.cur, set(srv.S.pages)
    rows = json.loads(U(await srv.open_many([base_url + p for p in ("/red.html", "/green.html", "/blue.html", "/boxed.html")])))
    ids = [r["tab_id"] for r in rows]
    try:
        img = await srv.contact_sheet(ids, columns=2, cell_width=300, cell_height=200)
        assert srv._image_size(img.data) == (2 * 300 + 4, 2 * 200 + 4)
        centre = lambda c, r: (c * 304 + 150, r * 204 + 100)
        got = await _pixels(srv, img, [centre(0, 0), centre(1, 0), centre(0, 1), centre(1, 1)])
        assert _is(got[0], RED) and _is(got[1], GREEN) and _is(got[2], BLUE), got
        assert _is(got[3], WHITE), got  # the boxed page is white where its red box does not reach
    finally:
        await _close(srv, *ids)
    assert srv.S.cur == cur and set(srv.S.pages) == before  # the scratch tab is gone, the current tab unchanged


async def test_contact_sheet_selector_shoots_just_that_element(srv, base_url):
    rows = json.loads(U(await srv.open_many([base_url + "/boxed.html"])))
    ids = [r["tab_id"] for r in rows]
    try:
        img = await srv.contact_sheet(ids, selector="#box", columns=1, cell_width=300, cell_height=200)
        assert _is((await _pixels(srv, img, [(150, 100)]))[0], RED)
    finally:
        await _close(srv, *ids)


async def test_contact_sheet_a_bad_tab_or_selector_gets_an_error_cell(srv, base_url):
    rows = json.loads(U(await srv.open_many([base_url + "/red.html"])))
    ids = [r["tab_id"] for r in rows]
    try:
        img = await srv.contact_sheet([ids[0], 9999], columns=2, cell_width=300, cell_height=200)
        got = await _pixels(srv, img, [(150, 100), (304 + 6, 6)])
        assert _is(got[0], RED)
        assert all(v < 70 for v in got[1]), got  # dark error cell, not a crash
        t0 = time.monotonic()
        img = await srv.contact_sheet(ids, selector="#does-not-exist", columns=1, cell_width=300, cell_height=200)
        assert srv._image_size(img.data) == (300, 200)
        assert time.monotonic() - t0 < 8  # a missing selector fails fast, it does not wait out locator timeouts
    finally:
        await _close(srv, *ids)


async def test_contact_sheet_validates_input(srv, base_url):
    assert "ERROR" in str(await srv.contact_sheet(columns=0))
    assert "ERROR" in str(await srv.contact_sheet(columns=6, cell_width=500))
    assert "ERROR" in str(await srv.contact_sheet(list(range(1, srv.MANY_MAX + 2))))


# ---------------------------------------------------------------- scroll_collect
async def test_virtual_list_loses_items_without_scroll_collect(srv, base_url):
    """Baseline: the page really unloads rows, so a plain link read after scrolling sees only the window."""
    await srv.navigate(base_url + "/virtual.html")
    await srv.scroll_to_load(max_scrolls=3, pause_ms=100)
    links = json.loads(U(await srv.extract_links(pattern="/item/", limit=500)))
    assert 0 < len(links) < 40


async def test_scroll_collect_gets_every_row_of_a_virtual_list(srv, base_url):
    await srv.navigate(base_url + "/virtual.html")
    res = json.loads(U(await srv.scroll_collect("a.it", max_items=100, max_scrolls=60, pause_ms=100)))
    urls = [i["url"] for i in res["items"]]
    assert res["count"] == 100 and res["stopped"] == "max_items"
    assert urls == [f"{base_url}/item/{i}" for i in range(100)]  # absolute, de-duplicated, in order
    assert res["items"][7]["text"] == "Item 7"


async def test_scroll_collect_stops_at_max_items_and_max_scrolls(srv, base_url):
    await srv.navigate(base_url + "/virtual.html")
    res = json.loads(U(await srv.scroll_collect("a.it", max_items=15, pause_ms=100)))
    assert res["count"] == 15 and res["stopped"] == "max_items"
    await srv.navigate(base_url + "/virtual.html")
    res = json.loads(U(await srv.scroll_collect("a.it", max_items=100, max_scrolls=2, pause_ms=100)))
    assert res["stopped"] == "max_scrolls" and res["scrolls"] == 2 and 15 < res["count"] < 100


async def test_scroll_collect_stops_when_nothing_new_appears(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    res = json.loads(U(await srv.scroll_collect("li.card", {"name": "h2", "price": ".price"}, idle_scrolls=2, pause_ms=100)))
    assert res["count"] == 3 and res["stopped"] == "no new items"
    assert res["items"][0] == {"name": "Alpha gummies", "price": "10,50 €"}


async def test_scroll_collect_scrolls_an_inner_container(srv, base_url):
    await srv.navigate(base_url + "/virtual_box.html")
    res = json.loads(U(await srv.scroll_collect("a.it", container="#box", max_items=60, max_scrolls=60, pause_ms=100)))
    assert res["count"] == 60 and [i["text"] for i in res["items"]] == [f"Row {i}" for i in range(60)]
    bad = await srv.scroll_collect("a.it", container="#nope", max_scrolls=1, pause_ms=100)
    assert bad.startswith("ERROR") and "not found" in bad


async def test_scroll_collect_on_another_tab_and_validation(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    cur = srv.S.cur
    rows = json.loads(U(await srv.open_many([base_url + "/virtual.html"])))
    tid = rows[0]["tab_id"]
    try:
        res = json.loads(U(await srv.scroll_collect("a.it", max_items=30, pause_ms=100, tab_id=tid)))
        assert res["count"] == 30 and srv.S.cur == cur
        assert (await srv.scroll_collect("")).startswith("ERROR")
        assert (await srv.scroll_collect("a", max_items=0)).startswith("ERROR")
        assert (await srv.scroll_collect("a", {"x": "@href"}, key="nope")).startswith("ERROR")
    finally:
        await _close(srv, tid)


# ---------------------------------------------------------------- inspect_posts
async def test_count_parser_handles_the_formats_instagram_prints(srv):
    c = srv._count
    assert [c("12"), c("1,042"), c("1.042"), c("1.2K"), c("3M"), c("45k"), c("2.5 K")] == [12, 1042, 1042, 1200, 3_000_000, 45_000, 2500]
    assert c("") is None and c(None) is None and c("lots") is None


async def test_ai_label_matches_whole_label_lines_only(srv):
    import re
    rx = re.compile(srv.AI_LABEL_RX, re.I)
    for yes in ("AI info", "AI-generated", "AI generated content", "Made with AI", "KI-Info", "Περιεχόμενο τεχνητής νοημοσύνης",
                "Contenido de IA", "Contenu généré par IA"):
        assert rx.match(yes), yes
    for no in ("I love AI info sessions", "AI", "Paris AI-generated art tonight", "Tag your best photos", "Αρέσει σε 5 άτομα"):
        assert not rx.match(no), no


async def test_describe_post_parses_a_greek_locale_og_description(srv):
    raw = {"ogDesc": '140 likes, 3 comments - america__states στις June 18, 2026: "Beautiful Louvre\nline two"',
           "ogUrl": "https://www.instagram.com/america__states/p/DZwaAf7ty8I/", "path": "/p/DZwaAf7ty8I/",
           "big": {"w": 810, "h": 1080, "src": "https://x/y.jpg"}, "aiLine": "Περιεχόμενο τεχνητής νοημοσύνης"}
    d = srv._describe_post(raw, include_image_url=False)
    assert (d["author"], d["likes"], d["comments"], d["views"]) == ("america__states", 140, 3, None)
    assert d["caption"].startswith("Beautiful Louvre") and d["media"] == "image" and "image_url" not in d
    assert d["main_image"] == {"w": 810, "h": 1080} and d["ai_label"] == "Περιεχόμενο τεχνητής νοημοσύνης"
    assert srv._describe_post(raw, include_image_url=True)["image_url"] == "https://x/y.jpg"


async def test_inspect_posts_reads_an_ai_labelled_carousel(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    cur, before = srv.S.cur, set(srv.S.pages)
    rows = json.loads(U(await srv.inspect_posts([base_url + "/post_ai.html"])))
    r = rows[0]
    assert (r["author"], r["likes"], r["comments"], r["views"]) == ("alice", 140, 3, None)
    assert r["date"] == "2026-06-19T05:50:25.000Z" and r["location"] == "Paris, France"  # not the bare "Locations" nav link
    assert r["media"] == "carousel" and r["main_image"] == {"w": 800, "h": 600}
    assert r["ai_label"] == "Περιεχόμενο τεχνητής νοημοσύνης"
    assert r["caption"] == "Louvre at dawn #paris" and r["login_wall"] is False and r["url"].endswith("/post_ai.html")
    assert "tab_id" not in r and set(srv.S.pages) == before and srv.S.cur == cur  # temporary tab closed


async def test_inspect_posts_plain_post_has_no_false_positives(srv, base_url):
    r = json.loads(U(await srv.inspect_posts([base_url + "/post_plain.html"])))[0]
    assert (r["author"], r["likes"], r["comments"]) == ("bob", 1042, 12)
    assert r["ai_label"] is None  # the caption mentions "AI info" but is not the platform label
    assert r["media"] == "image"  # the suggested-posts grid has its own Next arrow; it must not make this a carousel
    assert r["location"] is None


async def test_inspect_posts_video_and_views(srv, base_url):
    r = json.loads(U(await srv.inspect_posts([base_url + "/post_video.html"])))[0]
    assert r["media"] == "video" and r["views"] == 5210 and r["likes"] == 91 and r["author"] == "carol"


async def test_inspect_posts_several_urls_in_order_with_a_refused_one(srv, base_url):
    urls = [base_url + "/post_plain.html", "file:///c:/windows/win.ini", base_url + "/post_ai.html"]
    rows = json.loads(U(await srv.inspect_posts(urls)))
    assert [r.get("author") for r in rows] == ["bob", None, "alice"]
    assert "error" in rows[1] and rows[1]["url"] == urls[1]


async def test_inspect_posts_keep_tabs_feeds_contact_sheet(srv, base_url):
    before = set(srv.S.pages)
    rows = json.loads(U(await srv.inspect_posts([base_url + "/post_plain.html", base_url + "/post_ai.html"], keep_tabs=True, include_image_url=True)))
    ids = [r["tab_id"] for r in rows]
    try:
        assert set(srv.S.pages) - before == set(ids) and rows[0]["image_url"].endswith("/photo.svg")
        img = await srv.contact_sheet(ids, columns=2, cell_width=240, cell_height=200)
        assert srv._image_size(img.data) == (2 * 240 + 4, 200)
    finally:
        await _close(srv, *ids)


async def test_inspect_posts_on_tabs_that_are_already_open_leaves_them_open(srv, base_url):
    opened = json.loads(U(await srv.open_many([base_url + "/post_ai.html"])))
    tid = opened[0]["tab_id"]
    try:
        rows = json.loads(U(await srv.inspect_posts(tab_ids=[tid, 9999])))
        assert rows[0]["author"] == "alice" and rows[0]["tab_id"] == tid and tid in srv.S.pages
        assert rows[1]["tab_id"] == 9999 and rows[1]["error"] == "no such tab"
    finally:
        await _close(srv, tid)


async def test_inspect_posts_non_post_page_and_validation(srv, base_url):
    r = json.loads(U(await srv.inspect_posts([base_url + "/index.html"])))[0]
    assert r["title"] == "Test Shop" and r["media"] is None and r["likes"] is None and r["ai_label"] is None
    assert (await srv.inspect_posts()).startswith("ERROR")
    assert (await srv.inspect_posts([base_url + "/index.html"] * (srv.MANY_MAX + 1))).startswith("ERROR")
