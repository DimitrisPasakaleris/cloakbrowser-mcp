import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_navigate_and_text(srv, base_url):
    r = await srv.navigate(base_url + "/index.html")
    assert "status=200" in r and "Test Shop" in r
    assert "Welcome friend" in await srv.get_text()
    assert "Welcome" in await srv.get_html("p")


async def test_markdown(srv):
    md = await srv.get_markdown()
    assert "# Test Shop" in md and "**friend**" in md and "[Next page](" in md and "- " in md


async def test_snapshot_and_ref_click(srv):
    snap = await srv.snapshot()
    assert "Next page" in snap and "input" in snap
    ref = re.search(r"\[(e\d+)\] a \"Next page\"", snap).group(1)
    r = await srv.click(ref)
    assert "page2.html" in r
    assert "Page two text" in await srv.get_text()
    await srv.go_back()
    assert "Test Shop" in await srv.get_text("h1")


async def test_extract_links_structured(srv):
    links = json.loads(await srv.extract_links(pattern="page2"))
    assert len(links) == 1 and links[0]["href"].endswith("/page2.html")  # unique
    res = json.loads(await srv.extract_structured("li.card", {"name": "h2", "price": ".price", "url": "a@href"}))
    assert res["count"] == 3
    assert res["items"][0]["name"] == "Alpha gummies" and res["items"][0]["price"] == "10,50 €"
    assert res["items"][1]["url"].endswith("/b")


async def test_form_select_press(srv):
    assert "ERROR" not in await srv.type_text("#name", "Bob")
    assert "ERROR" not in await srv.select_option("#sel", "Why")
    await srv.click("#go")
    assert "hello Bob/y" in await srv.get_text("#out")
    r = await srv.fill_form({"#name": "Eve"}, submit_selector="#go")
    assert "ERROR" not in r and "hello Eve/y" in await srv.get_text("#out")
    assert "ERROR" not in await srv.press_key("Escape")
    assert "ERROR" not in await srv.hover("#go")


async def test_wait_for_and_late_content(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    assert await srv.wait_for(selector="#late") == "ok"
    assert await srv.wait_for(text="late content") == "ok"
    assert await srv.wait_for(network_idle=True) == "ok"


async def test_scroll(srv):
    assert int(await srv.scroll("down", 600)) > 0
    assert int(await srv.scroll("bottom")) > 1000
    assert await srv.scroll("top") == "0"
    assert "scrolled" in await srv.scroll_to_load(max_scrolls=3, pause_ms=100)


async def test_dialog(srv):
    assert "accept" in await srv.handle_dialog("accept")
    await srv.click("#dlg")
    assert "alerted" in await srv.get_text("#out")
    assert "boo" in await srv.handle_dialog("accept")
    assert "ERROR" in await srv.handle_dialog("bogus")


async def test_network_and_console(srv):
    await srv.click("#xhr")
    await srv.wait_for(text="api:42")
    reqs = json.loads(await srv.list_network_requests(url_pattern="api.json"))
    assert reqs and reqs[-1]["status"] == 200
    body = await srv.get_response_body(reqs[-1]["i"])
    assert '"answer": 42' in body
    assert "ERROR" in await srv.get_response_body(999999)


async def test_console_capture(srv, base_url):
    assert "ERROR" in await srv.console_logs()  # off by default
    await srv.configure(capture_console=True)
    await srv.navigate(base_url + "/index.html")
    await srv.evaluate("() => { console.warn('w1'); setTimeout(() => { throw new Error('boom') }, 0) }")
    await srv.wait_for(timeout_ms=300)
    logs = json.loads(await srv.console_logs())
    texts = [e["text"] for e in logs]
    assert "hello-console" in texts and "w1" in texts and any("boom" in t for t in texts)
    assert json.loads(await srv.console_logs(clear=True)) and json.loads(await srv.console_logs()) == []
    await srv.configure(capture_console=False)  # restarts the browser
    await srv.navigate(base_url + "/index.html")


async def test_evaluate_and_screenshot(srv):
    assert await srv.evaluate("1+2") == "3"
    assert await srv.evaluate("() => document.title") == '"Test Shop"'
    img = await srv.screenshot()
    assert img.data[:4] == b"\x89PNG"
    el = await srv.screenshot(selector="h1")
    assert el.data[:4] == b"\x89PNG"


async def test_files(srv):
    pdf = await srv.save_pdf("t.pdf")
    html = await srv.save_html("t.html")
    assert Path(pdf).stat().st_size > 500 and "Test Shop" in Path(html).read_text(encoding="utf-8")


async def test_tabs(srv, base_url):
    await srv.click("#pop")  # target=_blank popup
    await srv.wait_for(timeout_ms=800)
    listing = await srv.tabs_list()
    assert listing.count("\n") >= 1
    ids = [int(m) for m in re.findall(r"^[ *] (\d+):", listing, re.M)]
    new = await srv.tab_new(base_url + "/page2.html")
    assert "page2.html" in new
    assert "tab" in await srv.tab_select(ids[0])
    assert "Test Shop" in await srv.get_text("h1")
    assert "ERROR" in await srv.tab_select(9999)
    cur = await srv.tabs_list()
    for tid in [int(m) for m in re.findall(r"^[ *] (\d+):", cur, re.M)][1:]:
        await srv.tab_close(tid)
    assert (await srv.tabs_list()).count("\n") == 0


async def test_cookies(srv, base_url):
    await srv.set_cookies([{"name": "k", "value": "v", "url": base_url}])
    assert '"k"' in await srv.get_cookies()
    assert "[]" == await srv.get_cookies("no-such-domain")
    await srv.clear_cookies()
    assert '"k"' not in await srv.get_cookies()


async def test_stealth(srv):
    info = json.loads(await srv.check_stealth())
    assert info["webdriver"] in (False, None) and not info["headlessUA"]


async def test_error_handling(srv):
    r = await srv.click("#does-not-exist")
    assert r.startswith("ERROR in click")
    assert "ERROR" in await srv.navigate("http://127.0.0.1:1/")


async def test_request_human_needs_headed(srv):
    assert "headless" in await srv.request_human()


async def test_self_heal(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    await srv.S.ctx.close()  # simulate a closed/crashed browser
    r = await srv.navigate(base_url + "/page2.html")
    assert "Page Two" in r


async def test_configure_profile_persists(srv, base_url):
    out = json.loads(await srv.configure(profile="t1"))
    assert out["config"]["profile"] == "t1" and out["running"] is False
    await srv.navigate(base_url + "/index.html")
    await srv.set_cookies([{"name": "keep", "value": "1", "url": base_url, "expires": 4102444800}])
    await srv.close_browser()
    await srv.navigate(base_url + "/index.html")
    assert '"keep"' in await srv.get_cookies()
    await srv.configure(profile="")


async def test_proxy_rotation(srv):
    assert "ERROR" in await srv.rotate_proxy()
    await srv.configure(proxies=["http://u:p@127.0.0.1:9", "http://127.0.0.1:10"])
    assert "#1/2" in await srv.rotate_proxy()
    assert "#2/2" in await srv.rotate_proxy()
    assert "u:p" not in await srv.configure()
    await srv.configure(proxy="", proxies=[])
