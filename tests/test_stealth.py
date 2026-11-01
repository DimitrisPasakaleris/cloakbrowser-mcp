"""Stealth regressions: what the server leaves in a page, and what a site can read back."""
import json
import os
import re

import pytest
from helpers import U

ASYNC = pytest.mark.asyncio(loop_scope="session")


def test_language_list_matches_a_real_chrome(srv):
    assert srv._language_list("el-GR") == ["el-GR", "el", "en-US", "en"]
    assert srv._language_list("en-US") == ["en-US", "en"]
    assert srv._language_list("de") == ["de", "en-US", "en"]


def test_marker_names_are_random_not_the_old_fixed_ones(srv):
    assert re.fullmatch(r"data-k[0-9a-f]{10}", srv.REF_ATTR)
    assert "cm" not in srv.REF_ATTR + srv.OVERLAY_ID + srv.CONSOLE_KEY + srv.COUNTER_KEY
    assert "__REF__" not in srv.JS_SNAPSHOT and "__OVL__" not in srv.JS_OVERLAY and "__CON__" not in srv.JS_CONSOLE_HOOK


def test_geoip_is_automatic_by_default(srv):
    assert srv.S.cfg["geoip"] is None, "None means: switched on whenever a proxy is configured"


@ASYNC
async def test_snapshot_leaves_no_fixed_marker(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    await srv.snapshot()
    res = json.loads(U(await srv.evaluate(
        "() => ({old: !!document.querySelector('[data-cm-ref]'), oldGlobal: Object.getOwnPropertyNames(window).some(k => k.startsWith('__cm')),"
        " attrs: [...new Set([...document.querySelectorAll('*')].flatMap(e => e.getAttributeNames()).filter(a => /^data-k[0-9a-f]{10}$/.test(a)))],"
        " counterEnumerable: Object.keys(window).some(k => /^k[0-9a-f]{10}n$/.test(k))})")))
    assert res["old"] is False and res["oldGlobal"] is False
    assert res["attrs"] == [srv.REF_ATTR]
    assert res["counterEnumerable"] is False


@ASYNC
async def test_overlay_is_a_closed_shadow_root(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    await srv.snapshot()
    await srv.S.pages[srv.S.cur].evaluate(srv.JS_OVERLAY)
    try:
        res = json.loads(U(await srv.evaluate(
            "() => { const o = document.getElementById('" + srv.OVERLAY_ID + "'); return {present: !!o, shadow: o ? o.shadowRoot : 'none', kids: o ? o.children.length : -1}; }")))
        assert res == {"present": True, "shadow": None, "kids": 0}, "boxes live in a closed shadow root the page cannot read"
    finally:
        await srv.S.pages[srv.S.cur].evaluate(srv.JS_OVERLAY_REMOVE)


@ASYNC
async def test_console_hook_keeps_native_name_and_hides_its_buffer(srv, base_url):
    await srv.configure(capture_console=True)
    try:
        await srv.navigate(base_url + "/index.html")
        await srv.evaluate("() => { console.log('probe-line'); }")
        logs = U(await srv.console_logs())
        assert "probe-line" in logs
        res = json.loads(U(await srv.evaluate(
            "() => ({name: console.log.name, len: console.log.length, native: /native code/.test(Function.prototype.toString.call(console.log)),"
            " enumerable: Object.keys(window).some(k => /^k[0-9a-f]{10}c$/.test(k)), old: '__cm_console' in window})")))
        assert res["name"] == "log" and res["native"] is True
        assert res["enumerable"] is False and res["old"] is False
    finally:
        await srv.configure(capture_console=False)


@ASYNC
async def test_default_leaves_the_browsers_own_language_list_and_stays_consistent(srv, base_url, monkeypatch):
    monkeypatch.delenv("CLOAK_LANGUAGES", raising=False)
    await srv.close_browser()
    await srv.navigate(base_url + "/index.html")
    langs = json.loads(U(await srv.evaluate("() => navigator.languages")))
    assert len(langs) == 1, "default is OFF: the page and its workers agree on one language"


@ASYNC
async def test_opt_in_language_list_keeps_client_hints_but_the_audit_flags_the_worker_gap(srv, base_url, monkeypatch):
    monkeypatch.setenv("CLOAK_LANGUAGES", "auto")
    await srv.close_browser()
    try:
        await srv.navigate(base_url + "/index.html")
        res = json.loads(U(await srv.evaluate(
            "async () => { const h = await (await fetch('/echo-headers')).json();"
            " return {langs: navigator.languages, h, brands: navigator.userAgentData.brands.map(b => b.brand)}; }")))
        assert len(res["langs"]) >= 2, res["langs"]
        hdr = {k.lower(): v for k, v in res["h"].items()}
        assert [x.split(";")[0] for x in hdr["accept-language"].split(",")] == res["langs"], "header and navigator.languages agree"
        assert hdr.get("sec-ch-ua"), "client-hint headers must survive the override (a bare UA override drops them)"
        assert any(re.search(r"Not.?A.?Brand", b, re.I) for b in res["brands"]), res["brands"]
        out = json.loads(await srv.stealth_audit(network=False))
        gap = [f for f in out["findings"] if f["id"] == "worker-mismatch"]
        assert gap and "languages" in gap[0]["note"], "the known trade-off is detected, not hidden"
    finally:
        monkeypatch.delenv("CLOAK_LANGUAGES")
        await srv.close_browser()


@ASYNC
async def test_stealth_audit_has_no_fail_on_a_local_page(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    out = json.loads(await srv.stealth_audit(network=False))
    fails = [f for f in out["findings"] if f["verdict"] == "FAIL"]
    assert not fails, fails
    assert out["fail"] == 0 and {"findings", "seen_by_sites", "config"} <= set(out)
    assert out["config"]["proxy"] is False
    ids = {f["id"] for f in out["findings"]}
    assert "webdriver" not in ids and "cdp-runtime-enable" not in ids and "worker-mismatch" not in ids


@ASYNC
async def test_stealth_audit_detects_a_planted_leak(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    await srv.evaluate("() => { document.body.setAttribute('data-cm-ref', 'e1'); window.__cm_planted = 1; }")
    try:
        out = json.loads(await srv.stealth_audit(network=False))
        ids = {f["id"] for f in out["findings"] if f["verdict"] == "FAIL"}
        assert {"automation-globals", "legacy-markers"} <= ids
    finally:
        await srv.evaluate("() => { document.body.removeAttribute('data-cm-ref'); delete window.__cm_planted; }")


@ASYNC
async def test_request_human_refuses_when_headless(srv, base_url):
    await srv.navigate(base_url + "/index.html")
    assert "headless" in await srv.request_human(timeout_s=2)


@pytest.mark.skipif(not os.environ.get("CLOAK_TEST_HEADED"), reason="opens a visible window; set CLOAK_TEST_HEADED=1")
@ASYNC
async def test_request_human_returns_when_the_page_navigates_instead_of_erroring(srv, base_url):
    """The captcha-solved case: the page reloads while the banner is open (used to raise 'Execution context was destroyed')."""
    await srv.configure(headless=False)
    try:
        await srv.navigate(base_url + "/index.html")
        await srv.evaluate("() => { setTimeout(() => { location.href = '/page2.html'; }, 1500); }")
        out = await srv.request_human(timeout_s=20)
        assert out.startswith("human finished (page navigated)"), out
        out = await srv.request_human(timeout_s=3, banner=False)
        assert out.startswith("ERROR: timed out"), out
    finally:
        await srv.configure(headless=True)


def test_ip_timezone_lookup_validates_and_caches(srv, monkeypatch):
    import io
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps({"timezone": fake_urlopen.value}).encode())

    monkeypatch.setattr(srv.urllib.request, "urlopen", fake_urlopen)
    srv._TZ_CACHE.clear()
    fake_urlopen.value = "<script>alert(1)</script>"
    assert srv._ip_timezone() is None, "anything that is not an IANA name is refused"
    fake_urlopen.value = "Europe/Athens"
    assert srv._ip_timezone() == "Europe/Athens"
    assert srv._ip_timezone() == "Europe/Athens" and len(calls) == 2, "second call is served from the cache"
    srv._TZ_CACHE.clear()


@ASYNC
async def test_timezone_auto_makes_the_browser_follow_the_ip(srv, base_url, monkeypatch):
    monkeypatch.setattr(srv, "_ip_timezone", lambda: "Asia/Tokyo")
    await srv.configure(timezone="auto")
    try:
        await srv.navigate(base_url + "/index.html")
        assert json.loads(U(await srv.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone"))) == "Asia/Tokyo"
        out = json.loads(await srv.stealth_audit(network=False))
        assert out["seen_by_sites"]["tz"] == "Asia/Tokyo"
    finally:
        await srv.configure(timezone="")
        await srv.close_browser()
