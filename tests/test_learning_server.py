"""The learning loop through the real tools and a real browser: the load playbook in navigate, pop-up dismissers learned
from clicks and re-used, learned extractors in read_url, observe-only mode and tool gating."""
import json

import pytest
from helpers import U

from cloakbrowser_mcp import learn
from cloakbrowser_mcp import memory as m
from cloakbrowser_mcp.memory import Memory

pytestmark = pytest.mark.asyncio(loop_scope="session")


class Never:
    def random(self):
        return 0.999


@pytest.fixture
def mem(srv, tmp_path, monkeypatch):
    """Default-on memory (nothing configured) on a temp home that can remember localhost."""
    memory = Memory(tmp_path / "mem", env={}, allow_ip=True, default_on=True, rng=Never())
    monkeypatch.setattr(srv, "MEM", memory)
    return memory


def rows(mem, sql, *args):
    return [dict(r) for r in mem.store().conn().execute(sql, args).fetchall()]


# ------------------------------------------------------------------ default-on
async def test_first_navigation_shows_the_notice_once(srv, mem, base_url):
    first = await srv.navigate(base_url + "/index.html")
    assert "MEMORY IS ON" in first and "CLOAK_MEMORY=off" in first and "status=200" in first
    second = await srv.navigate(base_url + "/index.html")
    assert "MEMORY IS ON" not in second
    assert "MEMORY is off" not in first  # and the old opt-in hint is gone


async def test_default_on_records_visits_without_any_prompt(srv, mem, base_url):
    await srv.navigate(base_url + "/index.html")
    assert rows(mem, "SELECT visits FROM sites")[0]["visits"] == 1
    assert rows(mem, "SELECT arm, runs FROM playbook")[0] == {"arm": 0, "runs": 1.0}


async def test_user_can_switch_it_off(srv, mem, base_url):
    mem.disable("all")
    out = await srv.navigate(base_url + "/index.html")
    assert "MEMORY IS ON" not in out
    assert not mem.db_path.exists() or rows(mem, "SELECT COUNT(*) AS n FROM sites")[0]["n"] == 0


async def test_memory_tools_listed_in_default_mode_are_the_light_set(srv, mem, monkeypatch):
    monkeypatch.delenv("CLOAK_MEMORY_TOOLS", raising=False)  # the suite lists every memory tool; here we want the real gate
    assert srv._memory_level() == "light"
    assert srv.tool_enabled("memory_status", memory_on="light") and srv.tool_enabled("memory_recall", memory_on="light")
    assert srv.tool_enabled("memory_request_access", memory_on="light")
    for hidden in ("memory_remember", "memory_forget", "memory_export", "memory_audit", "memory_disable", "memory_incognito"):
        assert not srv.tool_enabled(hidden, memory_on="light"), hidden
        assert srv.tool_enabled(hidden, memory_on=True), hidden
    mem.apply_consent(m.PRESETS["standard"])  # the user chose explicitly: the full set
    assert srv._memory_level() is True
    mem.disable("all")
    assert srv._memory_level() is False


# ------------------------------------------------------------------ Phase 2: the load playbook
async def test_blocked_site_escalates_the_load_strategy(srv, mem, base_url):
    a = await srv.navigate(base_url + "/challenge")
    assert "BLOCKED?" in a and "load strategy" not in a
    b = await srv.navigate(base_url + "/challenge")
    assert "load strategy" not in b  # still arm 0 on the second try
    c = await srv.navigate(base_url + "/challenge")
    assert "memory: learned load strategy load+1500ms" in c  # two failures benched arm 0
    r = {x["arm"]: x for x in rows(mem, "SELECT * FROM playbook")}
    assert r[0]["streak_fail"] == 2 and r[0]["demoted_until"] > 0 and r[1]["runs"] == pytest.approx(1.0, abs=1e-3)


async def test_a_working_site_stays_on_the_cheap_strategy(srv, mem, base_url):
    for _ in range(3):
        out = await srv.navigate(base_url + "/page2.html")
        assert "load strategy" not in out and "status=200" in out
    r = rows(mem, "SELECT arm, runs, ok FROM playbook")
    assert len(r) == 1 and r[0]["arm"] == 0 and r[0]["runs"] == pytest.approx(3.0, abs=1e-3) and r[0]["ok"] == pytest.approx(3.0, abs=1e-3)


async def test_explicit_settings_override_and_are_not_planned(srv, mem, base_url):
    for _ in range(2):
        await srv.navigate(base_url + "/challenge")
    out = await srv.navigate(base_url + "/challenge", wait_until="domcontentloaded", settle_ms=500)
    assert "load strategy" not in out  # the caller chose; memory stays out of it
    custom = await srv.navigate(base_url + "/page2.html", wait_until="load", settle_ms=123)
    assert "status=200" in custom
    assert {r["arm"] for r in rows(mem, "SELECT arm FROM playbook")} == {0}  # custom settings (arm None) are not learned from


async def test_learned_strategy_is_applied_to_the_actual_load(srv, mem, base_url, monkeypatch):
    for _ in range(2):
        await srv.navigate(base_url + "/challenge")
    seen = {}
    page = await srv._ensure()
    real_goto = page.goto

    async def spy(url, **kw):
        seen.update(kw)
        return await real_goto(url, **kw)

    monkeypatch.setattr(page, "goto", spy)
    await srv.navigate(base_url + "/challenge")
    assert seen["wait_until"] == "load"


async def test_observe_only_mode_does_not_change_behaviour(srv, tmp_path, monkeypatch, base_url):
    quiet = Memory(tmp_path / "q", env={"CLOAK_MEMORY_APPLY": "0"}, allow_ip=True, default_on=True, rng=Never())
    monkeypatch.setattr(srv, "MEM", quiet)
    for _ in range(3):
        out = await srv.navigate(base_url + "/challenge")
        assert "load strategy" not in out
    assert rows(quiet, "SELECT streak_fail FROM playbook WHERE arm=0")[0]["streak_fail"] == 3  # but it still learned


async def test_navigation_failures_are_recorded_as_failures(srv, mem):
    r = await srv.navigate("http://127.0.0.1:9/never-open")
    assert r.startswith("ERROR")  # the wrapper turns the connection error into a message
    assert rows(mem, "SELECT streak_fail FROM playbook")[0]["streak_fail"] == 1


# ------------------------------------------------------------------ Phase 3: pop-up dismissers
async def test_clicking_a_cookie_button_teaches_the_dismisser(srv, mem, base_url):
    await srv.navigate(base_url + "/banner.html")
    assert U(await srv.evaluate("() => getComputedStyle(document.getElementById('cookie-banner')).display")) == '"block"'
    await srv.click("#accept-cookies")
    assert U(await srv.evaluate("() => getComputedStyle(document.getElementById('cookie-banner')).display")) == '"none"'
    got = rows(mem, "SELECT * FROM overlays")
    assert len(got) == 1 and got[0]["selector"] == "#accept-cookies" and got[0]["hits"] == pytest.approx(1.0, abs=1e-3) and got[0]["state"] == "active"


async def test_only_dismiss_buttons_inside_banners_are_learned(srv, mem, base_url):
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#plain")  # label "OK" but not inside a banner
    await srv.click("#learn-more")  # inside the banner, but not a dismiss action
    assert not mem.db_path.exists() or rows(mem, "SELECT COUNT(*) AS n FROM overlays")[0]["n"] == 0


async def test_a_click_that_does_not_dismiss_teaches_nothing(srv, mem, base_url):
    await srv.navigate(base_url + "/stuck.html")
    await srv.click("#accept-cookies")
    assert not mem.db_path.exists() or rows(mem, "SELECT COUNT(*) AS n FROM overlays")[0]["n"] == 0


async def test_remembered_dismisser_is_used_on_the_next_visit(srv, mem, base_url):
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#accept-cookies")
    out = await srv.navigate(base_url + "/banner.html")
    assert "memory: dismissed 1 remembered pop-up(s)" in out
    assert U(await srv.evaluate("() => getComputedStyle(document.getElementById('cookie-banner')).display")) == '"none"'
    imp = mem.learned()["impact"]["overlay"]
    assert imp["applied"] == 1 and imp["ok"] == 1 and imp["est_tokens_saved"] == 250
    assert rows(mem, "SELECT hits FROM overlays")[0]["hits"] == pytest.approx(2.0, abs=1e-3)


async def test_dismisser_is_not_clicked_when_the_banner_is_absent(srv, mem, base_url):
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#accept-cookies")
    out = await srv.navigate(base_url + "/page2.html")  # same site, no banner here
    assert "dismissed" not in out
    r = rows(mem, "SELECT hits, misses FROM overlays")[0]
    assert r["hits"] == pytest.approx(1.0, abs=1e-3) and r["misses"] == 0  # absence is not a failure


async def test_a_dismisser_that_stops_working_loses_trust(srv, mem, base_url):
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#accept-cookies")
    for _ in range(3):  # the same selector exists on this page but no longer closes anything
        await srv.navigate(base_url + "/stuck.html")
    r = rows(mem, "SELECT * FROM overlays")[0]
    assert r["state"] == "demoted" and r["misses"] == pytest.approx(3.0, abs=1e-3)
    assert "dismissed" not in await srv.navigate(base_url + "/banner.html")


async def test_dismissal_is_off_without_the_overlays_category(srv, tmp_path, monkeypatch, base_url):
    memory = Memory(tmp_path / "o", env={}, allow_ip=True, rng=Never())
    memory.apply_consent(("site_stats",))
    monkeypatch.setattr(srv, "MEM", memory)
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#accept-cookies")
    assert rows(memory, "SELECT COUNT(*) AS n FROM overlays")[0]["n"] == 0


async def test_clicks_still_work_normally_when_memory_is_off(srv, tmp_path, monkeypatch, base_url):
    monkeypatch.setattr(srv, "MEM", Memory(tmp_path / "off", env={}, allow_ip=True))
    await srv.navigate(base_url + "/banner.html")
    assert "clicked" in await srv.click("#accept-cookies")
    assert not (tmp_path / "off").exists()


# ------------------------------------------------------------------ Phase 3: learned extractors
FIELDS = {"name": "h2", "price": ".price", "url": "a@href"}


async def test_extract_structured_teaches_a_recipe(srv, mem, base_url):
    await srv.navigate(base_url + "/index.html")
    await srv.extract_structured("li.card", FIELDS)
    r = rows(mem, "SELECT * FROM extractors")
    assert len(r) == 1 and r[0]["ok"] == pytest.approx(1.0, abs=1e-3) and r[0]["items"] == 3 and json.loads(r[0]["spec"])["item"] == "li.card"


async def test_bad_selectors_are_not_remembered(srv, mem, base_url):
    await srv.navigate(base_url + "/index.html")
    out = U(await srv.extract_structured("li.nothing-here", {"name": "h2"}))
    assert json.loads(out)["count"] == 0
    assert rows(mem, "SELECT COUNT(*) AS n FROM extractors")[0]["n"] == 0
    await srv.extract_structured("li.card", {"name": "span.missing"})  # matches items, finds nothing in them
    assert rows(mem, "SELECT COUNT(*) AS n FROM extractors")[0]["n"] == 0


BIG = "/bigshop.html"  # 40 product cards plus text: big enough that a recipe is cheaper than reading the page


async def teach(srv, base_url, path=BIG, times=2):
    await srv.navigate(base_url + path)
    for _ in range(times):
        await srv.extract_structured("li.card", FIELDS)


async def test_read_url_uses_the_learned_recipe(srv, mem, base_url):
    await teach(srv, base_url)
    plain = await srv.read_url(base_url + BIG, use_memory=False)
    out = await srv.read_url(base_url + BIG)
    assert "learned extractor, 40 items" in out and "Product 1 gummies" in out and "Product 40 gummies" in out and "1.50" in out
    assert "recipe that worked here before" in out and "<untrusted_page_content>" in out
    assert len(out) < len(plain)  # that is the point: fewer characters for the same facts
    imp = mem.learned()["impact"]["extractor"]
    assert imp["applied"] == 1 and imp["ok"] == 1 and imp["est_tokens_saved"] == 900


async def test_small_pages_are_read_in_full_even_with_a_recipe(srv, mem, base_url):
    await teach(srv, base_url, "/index.html")
    out = await srv.read_url(base_url + "/index.html")
    assert "learned extractor" not in out and "# Test Shop" in out  # the page is shorter than the recipe's output
    assert "extractor" not in mem.learned()["impact"]  # nothing was applied, so no saving is claimed
    assert rows(mem, "SELECT runs FROM extractors")[0]["runs"] > 2.5  # but the recipe still counted as working


async def test_use_memory_false_reads_the_whole_page(srv, mem, base_url):
    await teach(srv, base_url)
    out = await srv.read_url(base_url + BIG, use_memory=False)
    assert "learned extractor" not in out and "# Big Shop" in out


async def test_recipe_that_finds_nothing_falls_back_and_is_marked_down(srv, mem, base_url):
    await srv.navigate(base_url + "/index.html")
    for _ in range(2):
        await srv.extract_structured("li.card", FIELDS)
    mem.store().conn().execute("UPDATE extractors SET page_type='other'")  # same page type as /page2.html
    for _ in range(2):
        out = await srv.read_url(base_url + "/page2.html")  # this page has no li.card
        assert "learned extractor" not in out and "Page two text" in out
    assert rows(mem, "SELECT state FROM extractors")[0]["state"] == "demoted"
    assert mem.best_extractor(base_url + "/page2.html") is None


async def test_read_url_with_a_query_or_offset_skips_the_recipe(srv, mem, base_url):
    await teach(srv, base_url)
    assert "learned extractor, 40 items" in await srv.read_url(base_url + BIG)  # control: it does apply
    assert "learned extractor" not in await srv.read_url(base_url + BIG, query="Section 3")
    assert "learned extractor" not in await srv.read_url(base_url + BIG, offset=5)
    assert "learned extractor" not in await srv.read_url(base_url + BIG, mode="text")


async def test_observe_only_mode_never_swaps_the_page_for_a_recipe(srv, tmp_path, monkeypatch, base_url):
    quiet = Memory(tmp_path / "q2", env={"CLOAK_MEMORY_APPLY": "0"}, allow_ip=True, default_on=True, rng=Never())
    monkeypatch.setattr(srv, "MEM", quiet)
    await teach(srv, base_url)
    out = await srv.read_url(base_url + BIG)
    assert "learned extractor" not in out and "# Big Shop" in out


# ------------------------------------------------------------------ everything together
async def test_the_loop_improves_a_second_visit(srv, mem, base_url):
    """First visit: a banner in the way and a long product page. After learning, both are handled for free."""
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#accept-cookies")
    await teach(srv, base_url)
    plain = await srv.read_url(base_url + BIG, use_memory=False)
    learned = await srv.read_url(base_url + BIG)
    assert "learned extractor, 40 items" in learned and "Product 7 gummies" in learned
    assert len(learned) < 0.8 * len(plain)
    again = await srv.navigate(base_url + "/banner.html")
    assert "dismissed 1 remembered pop-up(s)" in again
    st = mem.status()
    assert st["learned"]["popup_dismissers"] == 1 and st["learned"]["extractors"] == 1
    assert st["learned"]["impact"]["overlay"]["ok"] == 1 and st["learned"]["impact"]["extractor"]["ok"] == 1


async def test_nothing_learned_leaks_into_tool_output(srv, mem, base_url):
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#accept-cookies")
    out = await srv.navigate(base_url + "/banner.html")
    assert "#accept-cookies" not in out and "accept-btn" not in out
    status = await srv.memory_status()
    assert "#accept-cookies" not in status
    assert "#accept-cookies" not in await srv.memory_recall(site="127.0.0.1")


async def test_memory_status_tool_reports_what_was_learned(srv, mem, base_url):
    await srv.navigate(base_url + "/banner.html")
    await srv.click("#accept-cookies")
    st = json.loads(await srv.memory_status())
    assert st["source"] == "default" and st["enabled"] is True and st["learned"]["popup_dismissers"] == 1
    assert st["auto_apply"] is True


async def test_learning_constants_are_sane():
    assert learn.ARMS[0].wait_until == "domcontentloaded" and len(learn.ARMS) == 4
    assert m.Memory.REFLECT_EVERY >= 10 and m.SCHEMA_VERSION == 2
