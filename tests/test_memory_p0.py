"""Phase 0: consent gate, storage, sanitizers, permission prompt, CLI. No browser needed except where noted."""
import asyncio
import builtins
import json
import sys
from types import SimpleNamespace

import pytest

from cloakbrowser_mcp import memory as m
from cloakbrowser_mcp.memory import ConsentForm, Memory

pytestmark = pytest.mark.asyncio(loop_scope="session")
STD = {"site_stats", "site_structure", "extractors", "overlays"}


def mk(tmp_path, env=None, **kw):
    return Memory(tmp_path / "mem", env=env if env is not None else {}, **kw)


class FakeCtx:
    """Stands in for the MCP Context: what the client's permission prompt would return."""

    def __init__(self, action="accept", data=None, exc=None, delay=0):
        self.action, self.data, self.exc, self.delay, self.calls = action, data, exc, delay, 0

    async def elicit(self, message, schema):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return SimpleNamespace(action=self.action, data=self.data)


def files(tmp_path):
    base = tmp_path / "mem"
    return sorted(p.name for p in base.iterdir()) if base.exists() else []


# ------------------------------------------------------------------ off by default
async def test_default_off_creates_nothing(tmp_path):
    mem = mk(tmp_path)
    for cat in m.CATEGORIES:
        assert not mem.allow(cat, "shop.example.com")
    st = mem.status()
    assert st["enabled"] is False and st["source"] == "none" and st["preset"] == "off"
    mem.audit("site_stats", "x")  # must be a no-op, not create a database
    assert mem.audit_log() == []
    assert files(tmp_path) == [] and not (tmp_path / "mem").exists()


async def test_corrupt_consent_fails_closed(tmp_path):
    mem = mk(tmp_path)
    (tmp_path / "mem").mkdir()
    (tmp_path / "mem" / "consent.json").write_text("{not json", encoding="utf-8")
    assert not mem.allow("site_stats", "shop.example.com") and mem.status()["enabled"] is False


# ------------------------------------------------------------------ env pre-authorisation
@pytest.mark.parametrize(
    "env,expected",
    [
        ({"CLOAK_MEMORY": "standard"}, STD),
        ({"CLOAK_MEMORY": "minimal"}, {"site_stats"}),
        ({"CLOAK_MEMORY": "full"}, set(m.CATEGORIES)),
        ({"CLOAK_MEMORY": "custom", "CLOAK_MEMORY_CATEGORIES": "notes, recipes,bogus"}, {"notes", "recipes"}),
        ({"CLOAK_MEMORY": "off"}, set()),
        ({"CLOAK_MEMORY": "banana"}, set()),
    ],
)
async def test_env_presets(tmp_path, env, expected):
    c = mk(tmp_path, env).consent()
    assert set(c.enabled) == expected and c.source == "env"


async def test_env_off_is_a_kill_switch(tmp_path):
    mk(tmp_path).apply_consent(STD)
    assert mk(tmp_path).allow("site_stats", "shop.example.com")
    assert not mk(tmp_path, {"CLOAK_MEMORY": "off"}).allow("site_stats", "shop.example.com")


async def test_env_scope_and_controlled_from_registration(tmp_path):
    mem = mk(tmp_path, {"CLOAK_MEMORY": "minimal", "CLOAK_MEMORY_DOMAINS": "skroutz.gr"})
    assert mem.allow("site_stats", "www.skroutz.gr") and not mem.allow("site_stats", "example.com")
    with pytest.raises(PermissionError):
        mem.apply_consent(m.CATEGORIES)
    assert mem.disable("site_stats").startswith("ERROR")
    assert "environment" in await m.request_access(mem, FakeCtx())
    assert files(tmp_path) == []


# ------------------------------------------------------------------ granting consent
async def test_apply_consent_creates_files_only_when_something_enabled(tmp_path):
    a = mk(tmp_path / "a")
    a.apply_consent(STD, retention_days=30, effort="medium")
    assert files(tmp_path / "a") == ["consent.json", "memory.db"] or "consent.json" in files(tmp_path / "a")
    assert a.db_path.exists()
    data = json.loads(a.consent_path.read_text(encoding="utf-8"))
    assert data["offered"] == list(m.CATEGORIES) and data["retention_days"] == 30 and data["effort"] == "medium"
    assert a.audit_log()[0]["action"] == "granted"

    b = mk(tmp_path / "b")
    b.apply_consent([], ask="never")  # a "no" is recorded, but no database is created
    assert b.consent_path.exists() and not b.db_path.exists()


async def test_apply_consent_validation(tmp_path):
    mem = mk(tmp_path)
    for bad in (
        dict(categories=["bogus"]),
        dict(categories=STD, scope_mode="allowlist"),  # needs domains
        dict(categories=STD, effort="max"),
        dict(categories=STD, retention_days="-5"),
    ):
        with pytest.raises(ValueError):
            mem.apply_consent(**bad)
    assert files(tmp_path) == []


async def test_gate_category_isolation_and_scope(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(["site_stats"])
    assert mem.allow("site_stats", "shop.example.com")
    assert not mem.allow("notes", "shop.example.com") and not mem.allow("recipes", "shop.example.com")

    mem.apply_consent(["site_stats"], scope_mode="allowlist", scope_domains="skroutz.gr, https://www.foo.org/x")
    assert mem.allow("site_stats", "www.skroutz.gr") and mem.allow("site_stats", "m.skroutz.gr")
    assert mem.allow("site_stats", "foo.org") and not mem.allow("site_stats", "example.com")
    assert not mem.allow("site_stats", "notskroutz.gr")

    mem.apply_consent(["site_stats"], scope_mode="blocklist", scope_domains="shop.example.com")
    assert not mem.allow("site_stats", "shop.example.com") and mem.allow("site_stats", "other.example.com")


@pytest.mark.parametrize(
    "host,sensitive",
    [
        ("www.paypal.com", True), ("login.example.com", True), ("bank.example.com", True), ("secure.shop.com", True),
        ("www.irs.gov", True), ("service.gov.uk", True), ("tax.gov.gr", True), ("mail.google.com", True),
        ("xxx-videos.example", True), ("accounts.google.com", True),
        ("skroutz.gr", False), ("www.github.com", False), ("shop.example.com", False),
    ],
)
async def test_sensitive_sites_excluded_by_default(tmp_path, host, sensitive):
    mem = mk(tmp_path)
    mem.apply_consent(m.CATEGORIES)
    assert mem.is_sensitive(host) is sensitive
    assert mem.allow("site_stats", host) is (not sensitive)


async def test_sensitive_overrides(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(["site_stats"], sensitive_extra="private.test", sensitive_allow="login.example.com")
    assert not mem.allow("site_stats", "www.private.test") and mem.allow("site_stats", "login.example.com")
    mem.apply_consent(["site_stats"])  # re-saving keeps the user's lists
    assert not mem.allow("site_stats", "private.test")


async def test_ips_and_localhost_are_not_remembered(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(["site_stats"])
    for h in ("127.0.0.1", "localhost", "10.0.0.5", "[::1]", "intranet"):
        assert not mem.allow("site_stats", h), h
    assert mk(tmp_path, allow_ip=True).allow("site_stats", "127.0.0.1")  # only the test harness opts in


async def test_incognito_blocks_everything(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(STD)
    assert mem.allow("site_stats", "shop.example.com")
    mem.incognito = True
    assert not mem.allow("site_stats", "shop.example.com") and mem.status()["enabled"] is False
    assert not mk(tmp_path, {"CLOAK_MEMORY_INCOGNITO": "1"}).allow("site_stats", "shop.example.com")


# ------------------------------------------------------------------ re-consent when categories are added
async def test_new_categories_need_fresh_consent(tmp_path):
    base = tmp_path / "mem"
    base.mkdir()
    old_file = {
        "version": 1, "categories": ["site_stats", "notes"], "offered": ["site_stats", "site_structure", "extractors", "overlays", "recipes"],
        "scope": {"mode": "all", "domains": []}, "retention_days": 90, "ask": "ask",
    }
    (base / "consent.json").write_text(json.dumps(old_file), encoding="utf-8")
    mem = Memory(base, env={})
    c = mem.consent()
    assert c.needs_reconsent == ("notes",) and "notes" not in c.enabled and "site_stats" in c.enabled
    assert not mem.allow("notes", "shop.example.com") and mem.allow("site_stats", "shop.example.com")
    assert mem.should_prompt() is True
    mem.apply_consent(["site_stats", "notes"])
    assert mem.consent().needs_reconsent == () and mem.allow("notes", "shop.example.com")


async def test_should_prompt_policy(tmp_path):
    t = [1000.0]
    mem = mk(tmp_path, clock=lambda: t[0])
    assert mem.should_prompt() is True
    assert mk(tmp_path, {"CLOAK_MEMORY_NEVER_ASK": "1"}).should_prompt() is False
    await m.request_access(mem, FakeCtx(data=ConsentForm(decision="not_now")))
    assert mem.should_prompt() is False  # snoozed
    t[0] += 8 * 86400
    assert mem.should_prompt() is True  # a week later
    await m.request_access(mem, FakeCtx(data=ConsentForm(decision="never_ask")))
    t[0] += 400 * 86400
    assert mem.should_prompt() is False and mem.consent().ask == "never"


# ------------------------------------------------------------------ the permission prompt
async def test_prompt_accept_enables_what_was_ticked(tmp_path):
    mem = mk(tmp_path)
    form = ConsentForm(notes=True, recipes=False, overlays=False, scope_mode="only_listed_sites",
                       scope_domains="skroutz.gr", retention_days="forever", effort="high", reflect_when="triggers")
    msg = await m.request_access(mem, FakeCtx(data=form))
    assert msg.startswith("Memory enabled by the user")
    c = mem.consent()
    assert set(c.enabled) == {"site_stats", "site_structure", "extractors", "notes"}
    assert c.scope_mode == "allowlist" and c.scope_domains == ("skroutz.gr",)
    assert c.retention_days is None and c.effort == "high" and c.reflect_when == "triggers"
    assert mem.db_path.exists()


@pytest.mark.parametrize(
    "ctx",
    [
        FakeCtx(action="decline"),
        FakeCtx(action="cancel"),
        FakeCtx(action="accept", data=None),
        FakeCtx(exc=RuntimeError("client does not support elicitation")),
        FakeCtx(delay=1),
    ],
)
async def test_prompt_failures_leave_memory_off(tmp_path, ctx):
    mem = mk(tmp_path)
    msg = await m.request_access(mem, ctx, timeout_s=0.05)
    assert mem.status()["enabled"] is False and files(tmp_path) == []
    assert msg and "OFF" in msg or "off" in msg.lower()


async def test_prompt_unsupported_tells_user_how(tmp_path):
    msg = await m.request_access(mk(tmp_path), FakeCtx(exc=RuntimeError("nope")))
    assert "cloakbrowser-mcp memory setup" in msg and "cannot enable memory yourself" in msg


async def test_prompt_bad_answers_change_nothing(tmp_path):
    mem = mk(tmp_path)
    msg = await m.request_access(mem, FakeCtx(data=ConsentForm(scope_mode="only_listed_sites", scope_domains="")))
    assert "unchanged" in msg and files(tmp_path) == []
    msg = await m.request_access(mem, FakeCtx(data=ConsentForm(site_stats=False, site_structure=False, extractors=False, overlays=False)))
    assert "stays off" in msg and not mem.db_path.exists()


async def test_prompt_is_limited_per_session(tmp_path):
    mem, ctx = mk(tmp_path), FakeCtx(action="decline")
    await m.request_access(mem, ctx)
    await m.request_access(mem, ctx)
    msg = await m.request_access(mem, ctx)
    assert ctx.calls == 2 and "already asked" in msg


async def test_prompt_message_lists_every_category(tmp_path):
    text = m.consent_message()
    for cat in m.CATEGORIES:
        assert cat in text
    assert "Nothing is stored unless you agree" in text


# ------------------------------------------------------------------ narrowing, forgetting, wiping
def seed(mem):
    """Put recognisable rows in every table (as later phases will)."""
    c = mem.store().conn()
    now = mem.clock()
    c.execute("INSERT INTO sites(site, first_seen, last_seen, visits) VALUES('a.com', ?, ?, 1), ('b.com', ?, ?, 1)", (now, now, now, now))
    c.execute("INSERT INTO config_stats(site, config, runs, last_seen) VALUES('a.com','x',1,?)", (now,))
    c.execute("INSERT INTO events(ts, site, kind) VALUES(?, 'a.com', 'visit')", (now,))
    c.execute("INSERT INTO patterns(site, pattern, page_type, last_seen) VALUES('a.com','/c/{id}','category',?), ('b.com','/','home',?)", (now, now))
    c.execute("INSERT INTO notes(ts, site, kind, text, source, status) VALUES(?, 'a.com', 'lesson', 'hello', 'agent', 'active')", (now,))


def count(mem, table, where=""):
    return mem.store().conn().execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


async def test_disable_category_deletes_its_data_only(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(m.CATEGORIES)
    seed(mem)
    assert "deleted" in mem.disable("site_structure")
    assert count(mem, "patterns") == 0 and count(mem, "sites") == 2 and count(mem, "notes") == 1
    assert "site_structure" not in mem.consent().enabled and "site_stats" in mem.consent().enabled
    assert mem.disable("bogus").startswith("ERROR")


async def test_disable_all_asks_before_deleting(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(m.CATEGORIES)
    seed(mem)
    unsupported = await m.disable_with_confirm(mem, "all", FakeCtx(exc=RuntimeError("x")))
    assert "memory wipe" in unsupported and mem.status()["enabled"] is False and count(mem, "sites") == 2  # off, data kept

    mem.apply_consent(m.CATEGORIES)
    cancelled = await m.disable_with_confirm(mem, "all", FakeCtx(action="cancel"))
    assert "unchanged" in cancelled and mem.status()["enabled"] is True

    done = await m.disable_with_confirm(mem, "all", FakeCtx(data=m.ConfirmWipe(delete_data=True)))
    assert mem.status()["enabled"] is False and count(mem, "sites") == 0 and count(mem, "notes") == 0


async def test_forget_wipe_reset(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(m.CATEGORIES)
    seed(mem)
    assert mem.forget_site("https://www.a.com/some/page") >= 4
    assert count(mem, "sites", "WHERE site='a.com'") == 0 and count(mem, "sites", "WHERE site='b.com'") == 1
    assert mem.forget_site("never-seen.com") == 0
    mem.wipe()
    assert not mem.db_path.exists() and mem.consent_path.exists()
    mem.apply_consent(["site_stats"])
    mem.reset_consent()
    assert files(tmp_path) == []
    assert mem.consent().source == "none"


async def test_shrinking_consent_deletes_dropped_categories(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(m.CATEGORIES)
    seed(mem)
    mem.apply_consent(["site_stats"])  # e.g. the user re-ran setup with fewer boxes
    assert count(mem, "notes") == 0 and count(mem, "patterns") == 0 and count(mem, "sites") == 2


async def test_retention_purge(tmp_path):
    t = [10_000_000.0]
    a = mk(tmp_path, clock=lambda: t[0])
    a.apply_consent(m.CATEGORIES, retention_days=30)
    c = a.store().conn()
    old, new = t[0] - 40 * 86400, t[0] - 5 * 86400
    c.execute("INSERT INTO sites(site, first_seen, last_seen) VALUES('old.com',?,?), ('new.com',?,?)", (old, old, new, new))
    c.execute("INSERT INTO events(ts, site, kind) VALUES(?, 'old.com', 'v'), (?, 'new.com', 'v')", (old, new))
    c.execute("INSERT INTO notes(ts, site, kind, text, source, status) VALUES(?, 'old.com','l','x','agent','active')", (old,))
    a.store().close()
    b = mk(tmp_path, clock=lambda: t[0])  # a fresh process: first use purges
    b.store()
    assert count(b, "sites") == 1 and count(b, "events") == 1 and count(b, "notes") == 0


async def test_audit_and_export(tmp_path):
    mem = mk(tmp_path)
    mem.apply_consent(["site_stats", "notes"])
    mem.audit("site_stats", "visit", "a.com", "x" * 500)
    rows = mem.audit_log(5)
    assert rows[0]["action"] == "visit" and len(rows[0]["summary"]) <= 200
    out = json.loads(mem.export(tmp_path / "x" / "dump.json").read_text(encoding="utf-8"))
    assert out["consent"]["categories"]["notes"] is True and "audit" in out["data"]


# ------------------------------------------------------------------ sanitizers
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.skroutz.gr/c/2515/creatines.html?keyphrase=secret&page=2", ("skroutz.gr", "/c/{id}/creatines.html?keyphrase&page", "search")),
        ("https://shop.com/p/1234567/some-long-product-name-here", ("shop.com", "/p/{id}/{slug}", "product")),
        ("https://shop.com/item/3f2a9c1e-1b2c-4d5e-8f90-123456789abc", ("shop.com", "/item/{id}", "product")),
        ("https://example.co.uk/blog/hello", ("example.co.uk", "/blog/hello", "article")),
        ("https://example.com/", ("example.com", "/", "home")),
    ],
)
async def test_url_patterns_drop_ids_and_values(url, expected):
    host, site, pattern, ptype = m.recordable_url(url)
    assert (site, pattern, ptype) == expected
    assert "secret" not in pattern


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.com/login", "https://shop.com/account/orders", "https://shop.com/checkout/step2",
        "https://shop.com/cart", "https://shop.com/user/sign_in?next=/", "https://shop.com/oauth/authorize",
        "https://shop.com/admin/", "ftp://shop.com/file", "javascript:alert(1)", "not a url", "http://127.0.0.1/p",
    ],
)
async def test_sensitive_pages_are_never_recorded(url):
    assert m.recordable_url(url) is None


async def test_scrub_text():
    t = m.scrub_text("mail me at bob@example.com card 4111 1111 1111 1111 token ghp_abcdefghij1234567890 "
                     "and AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA via http://u:pw@h.com/")
    for secret in ("bob@example.com", "4111", "ghp_abcdef", "AAAAAAAA", "u:pw"):
        assert secret not in t


async def test_site_key():
    assert m.site_key("www.shop.example.co.uk") == "example.co.uk"
    assert m.site_key("m.skroutz.gr") == "skroutz.gr" and m.site_key("skroutz.gr") == "skroutz.gr"
    assert m.site_key("127.0.0.1") == "" and m.site_key("localhost") == "" and m.site_key("intranet") == ""
    assert m.site_key("127.0.0.1", allow_ip=True) == "127.0.0.1"


# ------------------------------------------------------------------ command line
def run_cli(tmp_path, *args, env=None):
    out = []
    mem = mk(tmp_path, env)
    rc = m.cli_main(list(args), mem=mem, out=out.append)
    return rc, "\n".join(out), mem


async def test_cli_enable_status_disable(tmp_path):
    rc, out, mem = run_cli(tmp_path, "enable", "--preset", "minimal", "--retention", "30", "--effort", "medium")
    assert rc == 0 and "site_stats" in out and set(mem.consent().enabled) == {"site_stats"}
    rc, out, _ = run_cli(tmp_path, "status")
    st = json.loads(out)
    assert rc == 0 and st["enabled"] and st["retention_days"] == 30 and st["effort"] == "medium"
    rc, out, mem = run_cli(tmp_path, "disable", "all")
    assert rc == 0 and mem.status()["enabled"] is False


async def test_cli_errors_and_confirmations(tmp_path):
    assert run_cli(tmp_path, "enable", "--categories", "bogus")[0] == 1
    assert run_cli(tmp_path, "enable", "--scope", "allowlist")[0] == 1
    assert run_cli(tmp_path, "setup")[0] == 2  # not a terminal and no --yes
    run_cli(tmp_path, "enable")
    assert run_cli(tmp_path, "wipe")[0] == 1  # destructive: needs --yes (no terminal here)
    rc, _, mem = run_cli(tmp_path, "wipe", "--yes")
    assert rc == 0 and mem.consent_path.exists()
    # the file is deleted, or emptied when a running server still holds it open (Windows)
    assert not mem.db_path.exists() or all(v == 0 for t in mem.store().counts().values() for v in t.values())
    rc, _, mem = run_cli(tmp_path, "reset", "--yes")
    assert rc == 0 and not mem.consent_path.exists()


async def test_cli_forget_export_audit(tmp_path):
    rc, _, mem = run_cli(tmp_path, "enable", "--preset", "full")
    seed(mem)
    assert "deleted" in run_cli(tmp_path, "forget", "a.com")[1]
    target = tmp_path / "out.json"
    rc, out, _ = run_cli(tmp_path, "export", str(target))
    assert rc == 0 and json.loads(target.read_text(encoding="utf-8"))["consent"]["enabled"] is True
    rc, out, _ = run_cli(tmp_path, "audit", "-n", "5")
    assert rc == 0 and "granted" in out


async def test_cli_interactive_wizard(tmp_path, monkeypatch):
    answers = iter(["y", "n", "n", "n", "n", "y",      # site_stats, site_structure, extractors, overlays, recipes, notes
                    "allowlist", "skroutz.gr", "365", "high", "triggers"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    rc, out, mem = run_cli(tmp_path, "setup")
    c = mem.consent()
    assert rc == 0 and set(c.enabled) == {"site_stats", "notes"}
    assert c.scope_mode == "allowlist" and c.scope_domains == ("skroutz.gr",)
    assert c.retention_days == 365 and c.effort == "high" and c.reflect_when == "triggers"


# ------------------------------------------------------------------ the server tools (agent-facing surface)
@pytest.fixture
def server_mem(srv, tmp_path, monkeypatch):
    mem = mk(tmp_path, allow_ip=True)
    monkeypatch.setattr(srv, "MEM", mem)
    return mem


async def test_memory_tools_registered_with_hints(srv):
    tools = {t.name: t for t in await srv.mcp.list_tools()}
    names = {n for n in tools if n.startswith("memory_")}
    assert {"memory_request_access", "memory_status", "memory_disable", "memory_forget", "memory_audit",
            "memory_export", "memory_incognito"} <= names
    assert tools["memory_status"].annotations.readOnlyHint is True
    assert tools["memory_request_access"].annotations.readOnlyHint is False
    for t in tools.values():
        assert "ctx" not in (t.inputSchema.get("properties") or {}), t.name  # Context is injected, not a parameter


async def test_agent_tools_cannot_enable_memory(srv, server_mem, tmp_path):
    ctx = FakeCtx(action="decline")
    assert json.loads(await srv.memory_status())["enabled"] is False
    await srv.memory_disable("all", ctx)
    await srv.memory_disable("notes", ctx)
    await srv.memory_forget("example.com")
    await srv.memory_incognito(True)
    assert await srv.memory_audit() == "[]"
    await srv.memory_request_access(ctx)  # user says no
    assert server_mem.status()["enabled"] is False
    assert files(tmp_path) == []  # not even a consent file


async def test_user_prompt_through_the_tool(srv, server_mem):
    out = await srv.memory_request_access(FakeCtx(data=ConsentForm()))
    assert "enabled by the user" in out
    st = json.loads(await srv.memory_status())
    assert st["enabled"] and set(k for k, v in st["categories"].items() if v) == STD
    assert "ERROR" in await srv.memory_incognito(False)  # the agent cannot end incognito
    await srv.memory_incognito(True)
    assert json.loads(await srv.memory_status())["enabled"] is False
    assert "deleted" in await srv.memory_forget("example.com")
    path = await srv.memory_export("pytest-export.json")
    assert json.loads(open(path, encoding="utf-8").read())["consent"]["categories"]["site_stats"] is True


async def test_off_hint_shown_once_per_session(srv, server_mem, base_url):
    first = await srv.navigate(base_url + "/page2.html")
    second = await srv.navigate(base_url + "/index.html")
    assert "MEMORY is off" in first and "memory_request_access" in first
    assert "MEMORY is off" not in second


# ------------------------------------------------------------------ the real MCP protocol (no fake context)
async def test_form_schemas_pass_the_sdk_validator():
    from mcp.server.elicitation import _validate_elicitation_schema  # what the SDK enforces on every prompt

    _validate_elicitation_schema(ConsentForm)
    _validate_elicitation_schema(m.ConfirmWipe)
    props = ConsentForm.model_json_schema()["properties"]
    assert props["effort"]["enum"] == ["off", "low", "medium", "high"] and "Options:" in props["effort"]["description"]
    with pytest.raises(ValueError):
        ConsentForm(effort="max")
    with pytest.raises(ValueError):
        ConsentForm(scope_mode="everywhere")


async def _client(srv, callback):
    from mcp.shared.memory import create_connected_server_and_client_session

    return create_connected_server_and_client_session(srv.mcp._mcp_server, elicitation_callback=callback)


async def test_protocol_accept_flow(srv, server_mem):
    from mcp.types import ElicitResult

    asked = []

    async def accept(context, params):
        asked.append(params)
        return ElicitResult(action="accept", content={
            "decision": "enable", "site_stats": True, "site_structure": False, "extractors": False, "overlays": False,
            "recipes": False, "notes": True, "scope_mode": "only_listed_sites", "scope_domains": "skroutz.gr",
            "retention_days": "30", "effort": "high", "reflect_when": "triggers"})

    async with await _client(srv, accept) as client:
        res = await client.call_tool("memory_request_access", {})
        text = res.content[0].text
    assert len(asked) == 1 and "Nothing is stored unless you agree" in asked[0].message
    assert set(asked[0].requestedSchema["properties"]) >= {"decision", "site_stats", "notes", "retention_days", "effort"}
    assert text.startswith("Memory enabled by the user"), text
    c = server_mem.consent()
    assert set(c.enabled) == {"site_stats", "notes"} and c.scope_domains == ("skroutz.gr",)
    assert c.retention_days == 30 and c.effort == "high" and c.reflect_when == "triggers"


async def test_protocol_decline_and_missing_support(srv, server_mem, tmp_path):
    from mcp.types import ElicitResult

    async def decline(context, params):
        return ElicitResult(action="decline")

    async with await _client(srv, decline) as client:
        text = (await client.call_tool("memory_request_access", {})).content[0].text
    assert "declined" in text and server_mem.status()["enabled"] is False and files(tmp_path) == []

    async with await _client(srv, None) as client:  # a client that cannot show prompts at all
        text = (await client.call_tool("memory_request_access", {})).content[0].text
    assert "cloakbrowser-mcp memory setup" in text and server_mem.status()["enabled"] is False and files(tmp_path) == []


async def test_protocol_invalid_answer_is_rejected(srv, server_mem, tmp_path):
    from mcp.types import ElicitResult

    async def junk(context, params):
        return ElicitResult(action="accept", content={"decision": "enable", "effort": "maximum"})

    async with await _client(srv, junk) as client:
        text = (await client.call_tool("memory_request_access", {})).content[0].text
    assert server_mem.status()["enabled"] is False and files(tmp_path) == [] and text


async def test_protocol_disable_all_confirmation(srv, server_mem):
    from mcp.types import ElicitResult

    server_mem.apply_consent(m.CATEGORIES)
    seed(server_mem)

    async def keep_data(context, params):
        return ElicitResult(action="accept", content={"delete_data": False})

    async with await _client(srv, keep_data) as client:
        await client.call_tool("memory_disable", {"category": "all"})
    assert server_mem.status()["enabled"] is False and count(server_mem, "sites") == 2
