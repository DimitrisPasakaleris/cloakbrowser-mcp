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
