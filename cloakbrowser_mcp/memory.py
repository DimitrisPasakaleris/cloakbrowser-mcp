"""Consent-gated site memory for cloakbrowser-mcp.

Design rules (see README, "Memory"):
  * Off by default. Nothing is written, and no database exists, until the user opts in.
  * Consent can only come from the user: the client's own prompt (MCP elicitation), the
    `cloakbrowser-mcp memory setup` CLI, or environment variables set in the MCP registration.
    No agent-callable tool can widen what is allowed; tools may only ask, narrow, or read.
  * Every read or write goes through `Memory.allow(category, host)`.
  * Only structure is stored (URL patterns, counts, selectors). Never page text, cookies or form values.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field, field_validator

from . import learn

_log = logging.getLogger("cloakbrowser-mcp")
CONSENT_VERSION = 1
SCHEMA_VERSION = 2
DEFAULT_RETENTION = 90

CATEGORIES = ("site_stats", "site_structure", "extractors", "overlays", "recipes", "notes")
CATEGORY_INFO = {
    "site_stats": (
        "Per-site success and block counts, status codes, load times, which browser settings worked",
        "page content, cookies, form values",
    ),
    "site_structure": (
        "URL patterns such as /c/{id}/{slug} and page types (search, product, category)",
        "query values, real IDs, page text",
    ),
    "extractors": (
        "Selectors and field names that extracted data successfully, JSON-LD / API endpoint templates",
        "the extracted data itself",
    ),
    "overlays": ("Selectors of cookie banners and pop-ups that were dismissed", "anything else on the page"),
    "recipes": ("Replayable step sequences with placeholders", "typed values, passwords, cookies"),
    "notes": ("Short lessons the assistant writes about a site", "page text, URLs, secrets (all scrubbed)"),
}
PRESETS = {
    "off": (),
    "minimal": ("site_stats",),
    "standard": ("site_stats", "site_structure", "extractors", "overlays"),
    "full": CATEGORIES,
}
EFFORTS = ("off", "low", "medium", "high")
REFLECT_WHEN = ("manual", "session_end", "triggers")
SCOPE_MODES = ("all", "allowlist", "blocklist")
ASK_MODES = ("ask", "later", "never")

# Category -> tables holding its data (some arrive in later phases).
CATEGORY_TABLES = {
    "site_stats": ("sites", "config_stats", "events", "playbook", "impact", "meta"),
    "site_structure": ("patterns",),
    "extractors": ("extractors",),
    "overlays": ("overlays",),
    "recipes": ("recipes",),
    "notes": ("notes",),
}


def default_base() -> Path:
    home = os.environ.get("CLOAK_MCP_HOME")
    return (Path(home) if home else Path.home() / ".cloakbrowser-mcp") / "memory"


# ---------------------------------------------------------------- sites, URLs, scrubbing
_SECOND_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz",
    "co.jp", "co.kr", "co.in", "com.br", "com.mx", "com.tr", "com.gr", "gov.gr", "edu.gr", "org.gr", "net.gr",
    "com.cy", "co.za", "com.sg", "com.hk", "com.tw", "com.cn", "com.ar", "co.il",
}
_IP_RX = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|\[?[0-9a-f:]*:[0-9a-f:]*\]?)$", re.I)


def site_key(host: str, allow_ip: bool = False) -> str:
    """Registrable-ish site name used as the memory key ('www.shop.example.co.uk' -> 'example.co.uk')."""
    host = (host or "").lower().strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host and host != "localhost":
        return host if allow_ip and host else ""
    if _IP_RX.match(host) or host == "localhost":
        return host if allow_ip else ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


_SKIP_PATH = re.compile(
    r"/(log-?in|sign-?in|sign_in|sign-?up|register|my-?account|account|checkout|cart|basket|payments?|pay|billing|"
    r"password|reset|oauth2?|auth|sso|admin|profile|settings|wallet|orders?|verify|2fa|mfa)(/|$|\.|\?)",
    re.I,
)
_ID_SEG = re.compile(
    r"^(\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I
)
_EXTS = (".html", ".htm", ".php", ".aspx", ".jsp")
_SEARCH_KEYS = {"q", "query", "keyphrase", "search", "s", "keywords", "k", "text", "term"}


def _norm_segment(seg: str) -> str:
    if not seg:
        return seg
    ext = ""
    base = seg
    low = seg.lower()
    for e in _EXTS:
        if low.endswith(e):
            base, ext = seg[: -len(e)], seg[-len(e):]
            break
    if _ID_SEG.match(base):
        return "{id}" + ext
    if re.search(r"\d", base) and len(base) >= 8:
        return "{id}" + ext
    if base.count("-") >= 3 or len(base) >= 24:
        return "{slug}" + ext
    return seg


def guess_page_type(path: str, query_keys: set[str]) -> str:
    p = path.lower()
    if query_keys & _SEARCH_KEYS or re.search(r"/(search|results?|find)(/|$)", p):
        return "search"
    if re.search(r"/(p|product|products|item|items|dp|listing)(/|$)", p):
        return "product"
    if re.search(r"/(c|category|categories|collections?|cat|shop|browse)(/|$)", p):
        return "category"
    if p in ("", "/"):
        return "home"
    if re.search(r"/(blog|news|article|articles|post|posts)(/|$)", p):
        return "article"
    return "other"


def recordable_url(url: str, allow_ip: bool = False):
    """(host, site, pattern, page_type) for a URL that may be remembered, or None.
    Login, checkout, account and similar pages are never recorded."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    site = site_key(host, allow_ip)
    if not site or _SKIP_PATH.search(u.path + ("?" if u.query else "")):
        return None
    segs = [_norm_segment(s) for s in u.path.split("/")]
    path = "/".join(segs) or "/"
    keys = []
    for k, _v in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        if re.fullmatch(r"[A-Za-z0-9_\-\[\]]{1,20}", k) and k.lower() not in ("token", "key", "sig", "password", "auth"):
            keys.append(k)
    pattern = (path + ("?" + "&".join(sorted(set(keys))[:6]) if keys else ""))[:120]
    return host, site, pattern, guess_page_type(u.path, {k.lower() for k in keys})


_SCRUB = [
    (re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b"), "[redacted]"),
    (re.compile(r"(?<![\w.])(?:\d[ -]?){13,19}(?![\w])"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]{10,}\b"), "[redacted]"),
    (re.compile(r"(?i)\b(?:ghp_|github_pat_|sk-|xox[bp]-)[A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{28,}\b"), "[redacted]"),
    (re.compile(r"(?i)(//)[^/@\s:]+:[^/@\s]+@"), r"\1***@"),
]


def scrub_text(text: str) -> str:
    for rx, sub in _SCRUB:
        text = rx.sub(sub, text)
    return text


# ---------------------------------------------------------------- sensitive sites
_SENS_SUFFIX = (
    "paypal.com", "stripe.com", "wise.com", "revolut.com", "chase.com", "bankofamerica.com", "wellsfargo.com",
    "citi.com", "hsbc.com", "barclays.co.uk", "lloydsbank.com", "santander.com", "nbg.gr", "piraeusbank.gr",
    "eurobank.gr", "alpha.gr", "accounts.google.com", "myaccount.google.com", "mail.google.com", "outlook.live.com",
    "outlook.office.com", "outlook.office365.com", "login.microsoftonline.com", "login.live.com", "mail.yahoo.com",
    "proton.me", "protonmail.com", "icloud.com", "appleid.apple.com", "mygov.gr", "taxisnet.gr", "gsis.gr",
    "irs.gov", "healthcare.gov", "mychart.com",
)
_SENS_LABEL = re.compile(
    r"(^|\.)(bank|banking|ebanking|netbanking|onlinebanking|login|signin|sso|auth|accounts|account|secure|patient|"
    r"health|mychart)\.",
    re.I,
)
_SENS_GOV = re.compile(r"\.(gov|mil)$|\.gov\.[a-z]{2}$", re.I)
_SENS_WORD = re.compile(r"(porn|xxx|onlyfans|escort)", re.I)


def _norm_entry(e: str) -> str:
    e = e.strip().lower()
    e = re.sub(r"^[a-z]+://", "", e).split("/")[0]
    return e[4:] if e.startswith("www.") else e


def _matches(host: str, entries) -> bool:
    h = host[4:] if host.startswith("www.") else host
    return any(h == e or h.endswith("." + e) for e in entries if e)


# ---------------------------------------------------------------- consent
@dataclass(frozen=True)
class Consent:
    source: str = "none"  # none | file | env
    preset: str = "off"
    categories: frozenset = frozenset()
    offered: frozenset = frozenset(CATEGORIES)
    scope_mode: str = "all"
    scope_domains: tuple = ()
    retention_days: int | None = DEFAULT_RETENTION
    effort: str = "low"
    reflect_when: str = "manual"
    ask: str = "ask"
    ask_after: float = 0.0
    sensitive_extra: tuple = ()
    sensitive_allow: tuple = ()
    decided_at: float = 0.0

    @property
    def needs_reconsent(self) -> tuple:
        """Categories added by a newer version that the user has not been asked about yet."""
        return tuple(c for c in CATEGORIES if c not in self.offered) if self.source == "file" else ()

    @property
    def enabled(self) -> frozenset:
        return frozenset(c for c in self.categories if c in self.offered)


# Used when the user has made no choice at all (no consent file, no CLOAK_MEMORY) and default-on is allowed. It is a
# virtual consent: nothing is written to consent.json, so the first explicit choice (or `memory disable`) replaces it.
DEFAULT_CONSENT = Consent(
    source="default",
    preset="standard",
    categories=frozenset(("site_stats", "site_structure", "extractors", "overlays")),
    effort="medium",
    reflect_when="triggers",
    ask="never",
)
NOTICE = (
    "MEMORY IS ON by default (standard: per-site load stats, URL patterns, and pop-up / extractor selectors; no page "
    "text, cookies or form values; local only; banking, email, health and government sites excluded). It learns which "
    "load settings, pop-up dismissers and extractors work per site and reuses them. Turn it off: set CLOAK_MEMORY=off "
    "in the MCP registration, or run `cloakbrowser-mcp memory disable`."
)


def _preset_name(cats) -> str:
    cats = tuple(c for c in CATEGORIES if c in cats)
    for name, members in PRESETS.items():
        if tuple(c for c in CATEGORIES if c in members) == cats:
            return name
    return "custom"


def _parse_retention(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("forever", "none", "0", ""):
        return None
    n = int(s)
    if n < 1:
        raise ValueError("retention must be a positive number of days or 'forever'")
    return n


def _csv(v) -> tuple:
    if not v:
        return ()
    items = v.split(",") if isinstance(v, str) else list(v)
    return tuple(x for x in (_norm_entry(i) for i in items) if x)


def _load_file(path: Path) -> Consent | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        cats = frozenset(c for c in d.get("categories", []) if c in CATEGORIES)
        offered = d.get("offered")
        offered = frozenset(c for c in offered if c in CATEGORIES) if isinstance(offered, list) else frozenset(CATEGORIES)
        scope = d.get("scope") or {}
        mode = scope.get("mode", "all")
        return Consent(
            source="file",
            preset=_preset_name(cats),
            categories=cats,
            offered=offered,
            scope_mode=mode if mode in SCOPE_MODES else "all",
            scope_domains=_csv(scope.get("domains")),
            retention_days=_parse_retention(d.get("retention_days", DEFAULT_RETENTION)),
            effort=d.get("effort") if d.get("effort") in EFFORTS else "low",
            reflect_when=d.get("reflect_when") if d.get("reflect_when") in REFLECT_WHEN else "manual",
            ask=d.get("ask") if d.get("ask") in ASK_MODES else "ask",
            ask_after=float(d.get("ask_after", 0) or 0),
            sensitive_extra=_csv(d.get("sensitive_extra")),
            sensitive_allow=_csv(d.get("sensitive_allow")),
            decided_at=float(d.get("decided_at", 0) or 0),
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return None  # missing or corrupt: behave as if nothing was ever consented to


def _from_env(env) -> Consent | None:
    mode = (env.get("CLOAK_MEMORY") or "").strip().lower()
    if not mode:
        return None
    if mode in ("0", "off", "false", "no"):
        return Consent(source="env", ask="never")
    if mode == "custom":
        cats = frozenset(c.strip() for c in (env.get("CLOAK_MEMORY_CATEGORIES") or "").split(",") if c.strip() in CATEGORIES)
    elif mode in PRESETS:
        cats = frozenset(PRESETS[mode])
    else:
        return Consent(source="env", ask="never")  # unknown value: fail closed
    dom = _csv(env.get("CLOAK_MEMORY_DOMAINS"))
    blk = _csv(env.get("CLOAK_MEMORY_BLOCKED"))
    try:
        retention = _parse_retention(env.get("CLOAK_MEMORY_RETENTION", DEFAULT_RETENTION))
    except ValueError:
        retention = DEFAULT_RETENTION
    return Consent(
        source="env",
        preset=_preset_name(cats),
        categories=cats,
        scope_mode="allowlist" if dom else ("blocklist" if blk else "all"),
        scope_domains=dom or blk,
        retention_days=retention,
        effort=env.get("CLOAK_MEMORY_EFFORT") if env.get("CLOAK_MEMORY_EFFORT") in EFFORTS else "low",
        reflect_when=env.get("CLOAK_MEMORY_REFLECT") if env.get("CLOAK_MEMORY_REFLECT") in REFLECT_WHEN else "manual",
        ask="never",
    )


def default_on_from_env(env=None) -> bool:
    """Is the default-on preset allowed? CLOAK_MEMORY_DEFAULT=off restores the old opt-in behaviour."""
    return str((os.environ if env is None else env).get("CLOAK_MEMORY_DEFAULT") or "on").strip().lower() not in ("0", "off", "false", "no")


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- storage
_SCHEMA = """
CREATE TABLE sites(
  site TEXT PRIMARY KEY, first_seen REAL, last_seen REAL,
  visits INTEGER DEFAULT 0, ok_count INTEGER DEFAULT 0, block_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
  last_status INTEGER, avg_ms REAL DEFAULT 0, human_needed INTEGER DEFAULT 0,
  crawl_delay REAL, robots_blocks INTEGER DEFAULT 0
);
CREATE TABLE config_stats(
  site TEXT, config TEXT, runs INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, last_seen REAL,
  PRIMARY KEY(site, config)
);
CREATE TABLE events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, site TEXT, kind TEXT, status INTEGER, ok INTEGER, blocked INTEGER,
  ms INTEGER, config TEXT
);
CREATE TABLE patterns(
  site TEXT, pattern TEXT, page_type TEXT, visits INTEGER DEFAULT 1, last_seen REAL, PRIMARY KEY(site, pattern)
);
CREATE TABLE notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, site TEXT, kind TEXT, text TEXT, source TEXT, status TEXT,
  confidence REAL, reason TEXT
);
CREATE TABLE audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, category TEXT, action TEXT, site TEXT, summary TEXT
);
CREATE INDEX events_ts ON events(ts);
CREATE INDEX notes_site ON notes(site);
"""
# Schema 2 (self-improving memory). Counts are REAL because old evidence decays (learn.HALF_LIFE_DAYS). Everything stored
# here is a number, our own label, or a selector that passed learn.safe_selector; never page text.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS playbook(
  site TEXT, arm INTEGER, runs REAL DEFAULT 0, ok REAL DEFAULT 0, streak_fail INTEGER DEFAULT 0,
  demoted_until REAL DEFAULT 0, last_seen REAL, PRIMARY KEY(site, arm)
);
CREATE TABLE IF NOT EXISTS overlays(
  site TEXT, selector TEXT, hits REAL DEFAULT 0, misses REAL DEFAULT 0, streak_miss INTEGER DEFAULT 0,
  state TEXT DEFAULT 'active', last_seen REAL, PRIMARY KEY(site, selector)
);
CREATE TABLE IF NOT EXISTS extractors(
  site TEXT, page_type TEXT, spec TEXT, kind TEXT DEFAULT 'structured', runs REAL DEFAULT 0, ok REAL DEFAULT 0,
  streak_fail INTEGER DEFAULT 0, state TEXT DEFAULT 'active', items INTEGER DEFAULT 0, last_seen REAL,
  PRIMARY KEY(site, page_type, spec)
);
CREATE TABLE IF NOT EXISTS impact(
  kind TEXT PRIMARY KEY, applied INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, saved_tokens INTEGER DEFAULT 0, last_seen REAL
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
NON_SITE_TABLES = {"impact", "meta"}  # no per-site rows: skipped when forgetting a site
MAX_EVENTS = 20000
MAX_AUDIT = 5000


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    def exists(self) -> bool:
        return self.path.exists()

    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(self.path), timeout=5, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            if ver == 0:
                c.executescript(_SCHEMA)
            if ver < 2:  # new databases and v1 databases (kept as they are) both gain the learning tables
                c.executescript(_SCHEMA_V2)
            if ver < SCHEMA_VERSION:
                c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn = c
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def wipe(self) -> str:
        """Delete the database file. If another process (a running server) has it open, which Windows does not
        allow, empty every table instead so the data is gone either way. Returns 'deleted' or 'emptied'."""
        self.close()
        try:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(self.path) + suffix).unlink()
                except FileNotFoundError:
                    pass
            return "deleted"
        except PermissionError:
            c = self.conn()
            for t in self.tables():
                if not t.startswith("sqlite_"):
                    c.execute(f"DELETE FROM {t}")
            try:
                c.execute("VACUUM")
            except sqlite3.Error:
                pass
            return "emptied"

    # -- audit
    def audit_add(self, ts, category, action, site="", summary=""):
        c = self.conn()
        c.execute(
            "INSERT INTO audit(ts, category, action, site, summary) VALUES(?,?,?,?,?)",
            (ts, category, action, site or "", (summary or "")[:200]),
        )
        c.execute("DELETE FROM audit WHERE id <= (SELECT MAX(id) FROM audit) - ?", (MAX_AUDIT,))

    def audit_list(self, limit=20):
        rows = self.conn().execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- housekeeping
    def tables(self):
        return {r[0] for r in self.conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def counts(self) -> dict:
        have = self.tables()
        out = {}
        for cat, tables in CATEGORY_TABLES.items():
            out[cat] = {t: self.conn().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables if t in have}
        return out

    def delete_category(self, category: str):
        have = self.tables()
        for t in CATEGORY_TABLES.get(category, ()):
            if t in have:
                self.conn().execute(f"DELETE FROM {t}")

    def delete_site(self, site: str) -> int:
        have, n = self.tables(), 0
        for tables in CATEGORY_TABLES.values():
            for t in tables:
                if t in have and t not in NON_SITE_TABLES:
                    n += self.conn().execute(f"DELETE FROM {t} WHERE site=?", (site,)).rowcount
        return n

    def purge(self, now: float, days: int | None):
        if not days:
            return
        cutoff = now - days * 86400
        c, have = self.conn(), self.tables()
        for t, col in (("events", "ts"), ("notes", "ts"), ("patterns", "last_seen"), ("config_stats", "last_seen"),
                       ("sites", "last_seen"), ("audit", "ts"), ("playbook", "last_seen"), ("overlays", "last_seen"),
                       ("extractors", "last_seen")):
            if t in have:
                c.execute(f"DELETE FROM {t} WHERE {col} < ?", (cutoff,))
        c.execute("DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id), 0) FROM events) - ?", (MAX_EVENTS,))

    def dump(self) -> dict:
        have, out = self.tables(), {}
        for t in sorted(have):
            if t == "audit":
                out[t] = self.audit_list(200)
            else:
                out[t] = [dict(r) for r in self.conn().execute(f"SELECT * FROM {t}").fetchall()]
        return out


# ---------------------------------------------------------------- notes: screening
MAX_NOTE = 400
_NOTE_RULES = [
    (re.compile(r"https?://|ftp://|\bwww\.", re.I), "contains a URL (describe page types instead)"),
    (re.compile(r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all|any|your)\b.{0,30}\b(instruction|rule|prompt|polic|guideline|message)", re.I), "tries to override instructions"),
    (re.compile(r"\b(system|developer) (prompt|message)\b|\bnew instructions\b|\byou are now\b", re.I), "addresses the assistant's instructions"),
    (re.compile(r"\byou (must|should|have to|need to|will)\b", re.I), "gives orders to the assistant"),
    (re.compile(r"\b(do not|don't|never|without)\b.{0,20}\b(tell|inform|ask|mention|notify|warn|show)\b.{0,20}\b(user|human|owner)\b", re.I), "asks to hide something from the user"),
    (re.compile(r"\b(disable|turn off|bypass|skip|circumvent|remove)\b.{0,25}\b(safety|policy|consent|permission|guardrail|security|protection|memory|incognito)", re.I), "tries to weaken a safety setting"),
    (re.compile(r"\b(enable|grant|allow)\b.{0,20}\b(memory|yourself|all categories)\b|memory_request_access|apply_consent|CLOAK_[A-Z_]+", re.I), "tries to change memory permissions"),
    (re.compile(r"\b(send|post|upload|email|forward|exfiltrate|leak|copy)\b.{0,30}\b(cookie|credential|password|token|secret|session|api key|private)", re.I), "asks to move secrets"),
    (re.compile(r"\b(curl|wget|powershell|bash -c|eval\(|exec\(|base64 -d|rm -rf)", re.I), "contains a command"),
    (re.compile(r"<\s*/?\s*(script|iframe|untrusted_page_content|system)", re.I), "contains markup that looks like an injection"),
]


def screen_note(text: str) -> str | None:
    """Reason a note must not be saved as an ordinary lesson, or None. Cheap deterministic rules; a model-based
    judge is layered on top in a later phase."""
    for rx, reason in _NOTE_RULES:
        if rx.search(text):
            return reason
    return None


def config_key(headless: bool, humanize: bool, proxy: bool) -> str:
    return f"headless={int(bool(headless))},humanize={int(bool(humanize))},proxy={int(bool(proxy))}"


def _host_of(site_or_url: str) -> str:
    v = (site_or_url or "").strip()
    host = urllib.parse.urlparse(v if "://" in v else "//" + v).hostname
    return (host or "").lower()


# ---------------------------------------------------------------- the facade
class Memory:
    def __init__(
        self,
        base: Path | None = None,
        env=None,
        allow_ip: bool = False,
        clock: Callable[[], float] = time.time,
        default_on: bool = False,
        rng: random.Random | None = None,
    ):
        self.base = Path(base) if base else default_base()
        self.env = os.environ if env is None else env
        self.allow_ip = allow_ip
        self.clock = clock
        self.default_on = default_on  # the standard preset applies until the user decides otherwise
        self.rng = rng or random.Random()
        self.incognito = _truthy(self.env.get("CLOAK_MEMORY_INCOGNITO"))
        self.prompts = 0  # permission requests made this session
        self.hinted = False  # "memory is off" hint shown this session
        self._store: Store | None = None
        self._cc: tuple | None = None
        self._purged = False

    @property
    def apply_on(self) -> bool:
        """Learned settings are applied automatically unless CLOAK_MEMORY_APPLY=0 (then memory only observes)."""
        return (self.env.get("CLOAK_MEMORY_APPLY") or "1").strip().lower() not in ("0", "false", "off", "no")

    # -- paths
    @property
    def consent_path(self) -> Path:
        return self.base / "consent.json"

    @property
    def db_path(self) -> Path:
        return self.base / "memory.db"

    # -- consent
    def consent(self) -> Consent:
        c = _from_env(self.env)
        if c is not None:
            return c
        try:
            mtime = self.consent_path.stat().st_mtime_ns
        except OSError:
            self._cc = None
            return DEFAULT_CONSENT if self.default_on else Consent()
        if self._cc and self._cc[0] == mtime:
            c = self._cc[1]
        else:
            c = _load_file(self.consent_path) or Consent()
            self._cc = (mtime, c)
        eff = self.env.get("CLOAK_MEMORY_EFFORT")
        if eff in EFFORTS and eff != c.effort:
            c = dataclasses.replace(c, effort=eff)
        return c

    def is_sensitive(self, host: str, c: Consent | None = None) -> bool:
        c = c or self.consent()
        h = host.lower().strip(".")
        if _matches(h, c.sensitive_allow):
            return False
        if _matches(h, c.sensitive_extra) or _matches(h, _SENS_SUFFIX):
            return True
        return bool(_SENS_LABEL.search(h) or _SENS_GOV.search("." + h) or _SENS_WORD.search(h))

    def allow(self, category: str, host: str) -> bool:
        """The single gate: may this category be read or written for this host right now?"""
        c = self.consent()
        if self.incognito or category not in c.enabled:
            return False
        h = (host or "").lower().strip(".")
        if not site_key(h, self.allow_ip) or self.is_sensitive(h, c):
            return False
        if c.scope_mode == "allowlist":
            return _matches(h, c.scope_domains)
        if c.scope_mode == "blocklist":
            return not _matches(h, c.scope_domains)
        return True

    def any_enabled(self) -> bool:
        return bool(self.consent().enabled)

    def should_prompt(self) -> bool:
        """Should the assistant be told once that memory is off and can be requested?"""
        if _truthy(self.env.get("CLOAK_MEMORY_NEVER_ASK")):
            return False
        c = self.consent()
        if c.source in ("env", "default"):
            return False
        if c.source == "none":
            return True
        if c.needs_reconsent and c.ask != "never":
            return True
        return c.ask == "later" and self.clock() >= c.ask_after and not c.enabled

    @property
    def notice_path(self) -> Path:
        return self.base / "notice.json"

    def notice(self) -> str:
        """The default-on disclosure, returned once ever (then remembered in notice.json); '' otherwise."""
        if self.consent().source != "default" or self.incognito or self.notice_path.exists():
            return ""
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            self.notice_path.write_text(json.dumps({"shown_at": self.clock()}), encoding="utf-8")
        except OSError:
            return ""
        return NOTICE

    # -- store (created lazily and only after consent)
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.db_path)
        s = self._store
        if not self._purged:
            self._purged = True
            s.purge(self.clock(), self.consent().retention_days)
        return s

    def audit(self, category, action, site="", summary=""):
        if self.any_enabled() or self.db_path.exists():
            self.store().audit_add(self.clock(), category, action, site, summary)

    # -- applying a decision (always initiated by the user: prompt, CLI)
    def apply_consent(
        self,
        categories,
        scope_mode: str = "all",
        scope_domains=(),
        retention_days=DEFAULT_RETENTION,
        effort: str = "low",
        reflect_when: str = "manual",
        ask: str = "ask",
        ask_days: int = 7,
        sensitive_extra=None,
        sensitive_allow=None,
        delete_removed: bool = True,
    ) -> Consent:
        if _from_env(self.env) is not None:
            raise PermissionError("memory is controlled by the CLOAK_MEMORY environment variable in the MCP registration")
        cats = frozenset(categories)
        bad = cats - set(CATEGORIES)
        if bad:
            raise ValueError(f"unknown categories: {sorted(bad)}")
        if scope_mode not in SCOPE_MODES or effort not in EFFORTS or reflect_when not in REFLECT_WHEN or ask not in ASK_MODES:
            raise ValueError("invalid scope_mode, effort, reflect_when or ask value")
        domains = _csv(scope_domains)
        if scope_mode != "all" and not domains:
            raise ValueError(f"scope '{scope_mode}' needs at least one domain")
        old = self.consent()
        now = self.clock()
        prev_extra = old.sensitive_extra if sensitive_extra is None else _csv(sensitive_extra)
        prev_allow = old.sensitive_allow if sensitive_allow is None else _csv(sensitive_allow)
        data = {
            "version": CONSENT_VERSION,
            "decided_at": now,
            "categories": [c for c in CATEGORIES if c in cats],
            "offered": list(CATEGORIES),
            "scope": {"mode": scope_mode, "domains": list(domains)},
            "retention_days": _parse_retention(retention_days),
            "effort": effort,
            "reflect_when": reflect_when,
            "ask": ask,
            "ask_after": now + ask_days * 86400 if ask == "later" else 0,
            "sensitive_extra": list(prev_extra),
            "sensitive_allow": list(prev_allow),
        }
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.consent_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.consent_path)
        self._cc = None
        if delete_removed and self.db_path.exists():
            for cat in old.enabled - cats:
                self.store().delete_category(cat)
                self.store().audit_add(now, cat, "deleted", "", "category turned off")
        if cats:
            self.store().audit_add(now, "consent", "granted", "", ",".join(data["categories"]))
        elif self.db_path.exists():
            self.store().audit_add(now, "consent", "revoked", "", "all categories off")
        return self.consent()

    # -- narrowing actions (safe for the agent to call)
    def disable(self, category: str, delete: bool = True) -> str:
        c = self.consent()
        if c.source == "env":
            return "ERROR: memory is controlled by the CLOAK_MEMORY environment variable; the user must change the MCP registration"
        if category == "all":
            cats = frozenset()
        elif category in CATEGORIES:
            cats = c.enabled - {category}
        else:
            return f"ERROR: unknown category '{category}' (choose one of {', '.join(CATEGORIES)} or 'all')"
        if c.source == "none":
            return "memory is already off"
        self.apply_consent(
            cats, c.scope_mode, c.scope_domains, c.retention_days, c.effort, c.reflect_when, c.ask,
            delete_removed=delete,
        )
        return f"disabled {category}" + ("; its stored data was deleted" if delete else "; stored data kept")

    def forget_site(self, site: str) -> int:
        key = site_key(urllib.parse.urlparse(site).hostname or site, self.allow_ip) or _norm_entry(site)
        if not self.db_path.exists():
            return 0
        n = self.store().delete_site(key)
        self.store().audit_add(self.clock(), "forget", "deleted", key, f"{n} rows")
        return n

    def wipe(self) -> str:
        """Delete the database. The consent file stays (it records the user's choices)."""
        if not self.db_path.exists():
            return "deleted"
        store = self._store or Store(self.db_path)
        self._store = None
        return store.wipe()

    def reset_consent(self) -> None:
        self.wipe()
        for p in (self.consent_path, self.notice_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self._cc = None

    # -- passive learning (Phase 1): every method is gated and silent when memory is off
    def record_navigation(self, url, status, blocked, ms, config="", failed=False, arm=None) -> bool:
        rec = recordable_url(url, self.allow_ip)
        if not rec:
            return False
        host, site, pattern, ptype = rec
        wrote = False
        now = self.clock()
        if arm is not None and self.allow("site_stats", host):
            self._record_arm(site, arm, bool(not failed and not blocked and status is not None and status < 400), now)
        if self.allow("site_stats", host):
            ok = int(not failed and not blocked and status is not None and status < 400)
            blk, fail = int(bool(blocked)), int(bool(failed))
            st = self.store()
            c = st.conn()
            c.execute(
                "INSERT INTO sites(site, first_seen, last_seen, visits, ok_count, block_count, fail_count, last_status, avg_ms)"
                " VALUES(?,?,?,1,?,?,?,?,?) ON CONFLICT(site) DO UPDATE SET last_seen=excluded.last_seen,"
                " avg_ms=(avg_ms*visits + excluded.avg_ms)/(visits+1), visits=visits+1, ok_count=ok_count+excluded.ok_count,"
                " block_count=block_count+excluded.block_count, fail_count=fail_count+excluded.fail_count,"
                " last_status=excluded.last_status",
                (site, now, now, ok, blk, fail, status, ms or 0),
            )
            if config:
                c.execute(
                    "INSERT INTO config_stats(site, config, runs, ok, blocked, last_seen) VALUES(?,?,1,?,?,?)"
                    " ON CONFLICT(site, config) DO UPDATE SET runs=runs+1, ok=ok+excluded.ok,"
                    " blocked=blocked+excluded.blocked, last_seen=excluded.last_seen",
                    (site, config, ok, blk, now),
                )
            c.execute(
                "INSERT INTO events(ts, site, kind, status, ok, blocked, ms, config) VALUES(?,?,?,?,?,?,?,?)",
                (now, site, "navigate", status, ok, blk, ms, config),
            )
            c.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (MAX_EVENTS,))
            st.audit_add(now, "site_stats", "visit", site, f"status={status} {'blocked' if blk else 'failed' if fail else 'ok'}")
            wrote = True
        if self.allow("site_structure", host):
            st = self.store()
            st.conn().execute(
                "INSERT INTO patterns(site, pattern, page_type, visits, last_seen) VALUES(?,?,?,1,?)"
                " ON CONFLICT(site, pattern) DO UPDATE SET visits=visits+1, last_seen=excluded.last_seen,"
                " page_type=excluded.page_type",
                (site, pattern, ptype, now),
            )
            st.audit_add(now, "site_structure", "pattern", site, f"{pattern} ({ptype})")
            wrote = True
        if wrote:
            self._maybe_reflect()
        return wrote

    # -- Phase 2: the per-site load playbook (category: site_stats)
    def _arm_stats(self, site: str, now: float) -> dict:
        out = {}
        for r in self.store().conn().execute("SELECT * FROM playbook WHERE site=?", (site,)):
            out[r["arm"]] = learn.ArmStat(
                runs=learn.decay(r["runs"], r["last_seen"], now),
                ok=learn.decay(r["ok"], r["last_seen"], now),
                demoted_until=r["demoted_until"] or 0.0,
                last_seen=r["last_seen"] or 0.0,
            )
        return out

    def plan_navigation(self, url) -> learn.Plan | None:
        """Which load strategy to use for this site, from what has worked here before. None = memory has no say."""
        if not self.apply_on:
            return None
        rec = recordable_url(url, self.allow_ip)
        if not rec or not self.allow("site_stats", rec[0]):
            return None
        now = self.clock()
        stats = self._arm_stats(rec[1], now) if self.db_path.exists() else {}
        explore = learn.EXPLORE.get(self.consent().effort, learn.EXPLORE["medium"])
        return learn.choose_arm(stats, now, self.rng, explore)

    def _record_arm(self, site: str, arm: int, ok: bool, now: float) -> None:
        c = self.store().conn()
        row = c.execute("SELECT * FROM playbook WHERE site=? AND arm=?", (site, arm)).fetchone()
        runs = learn.decay(row["runs"], row["last_seen"], now) if row else 0.0
        oks = learn.decay(row["ok"], row["last_seen"], now) if row else 0.0
        streak, until = learn.next_streak(row["streak_fail"] if row else 0, ok, now)
        c.execute(
            "INSERT INTO playbook(site, arm, runs, ok, streak_fail, demoted_until, last_seen) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(site, arm) DO UPDATE SET runs=excluded.runs, ok=excluded.ok, streak_fail=excluded.streak_fail,"
            " demoted_until=excluded.demoted_until, last_seen=excluded.last_seen",
            (site, arm, runs + 1, oks + (1 if ok else 0), streak, until, now),
        )
        if until and not (row and row["demoted_until"] and row["demoted_until"] > now):
            self.store().audit_add(now, "site_stats", "benched", site, f"{learn.ARMS[arm].label} failed {streak}x; skipped for {learn.COOLDOWN_DAYS:g} days")

    def note_impact(self, kind: str, ok: bool, saved_tokens: int = 0) -> None:
        """Count one automatic application of something learned (for `stats` / memory_status)."""
        if not self.db_path.exists() and not self.any_enabled():
            return
        c = self.store().conn()
        c.execute(
            "INSERT INTO impact(kind, applied, ok, saved_tokens, last_seen) VALUES(?,1,?,?,?)"
            " ON CONFLICT(kind) DO UPDATE SET applied=applied+1, ok=ok+excluded.ok, saved_tokens=saved_tokens+excluded.saved_tokens,"
            " last_seen=excluded.last_seen",
            (kind, int(bool(ok)), int(saved_tokens) if ok else 0, self.clock()),
        )

    # -- Phase 3: pop-up dismissers (category: overlays)
    def wants_overlay(self, url) -> bool:
        rec = recordable_url(url, self.allow_ip)
        return bool(rec and self.allow("overlays", rec[0]))

    def overlay_candidates(self, url, limit: int = 4) -> list[str]:
        """Selectors to try on this page: ones that dismissed a pop-up here before, then ones that worked on 3+ sites."""
        if not self.apply_on or not self.db_path.exists():
            return []
        rec = recordable_url(url, self.allow_ip)
        if not rec or not self.allow("overlays", rec[0]):
            return []
        rows = self.store().conn().execute(
            "SELECT selector, site FROM overlays WHERE site IN (?, '*') AND state IN ('active', 'candidate')"
            " ORDER BY (site='*'), hits DESC, last_seen DESC LIMIT ?",
            (rec[1], limit),
        ).fetchall()
        return [r["selector"] for r in rows]

    def record_overlay(self, url, selector, ok: bool, applied: bool = False) -> bool:
        """A pop-up dismisser worked (ok) or did not. applied=True means we clicked it ourselves from memory."""
        rec = recordable_url(url, self.allow_ip)
        sel = learn.safe_selector(selector)
        if not rec or sel is None or not self.allow("overlays", rec[0]):
            return False
        site, now = rec[1], self.clock()
        c = self.store().conn()
        row = c.execute("SELECT * FROM overlays WHERE site=? AND selector=?", (site, sel)).fetchone()
        shared = c.execute("SELECT * FROM overlays WHERE site='*' AND selector=?", (sel,)).fetchone()
        if row is None and not ok:
            if applied and shared is not None:  # a shared dismisser clicked here without effect: count it against the shared entry
                streak = shared["streak_miss"] + 1
                c.execute(
                    "UPDATE overlays SET misses=misses+1, streak_miss=?, state=?, last_seen=? WHERE site='*' AND selector=?",
                    (streak, "demoted" if streak >= learn.DEMOTE_AFTER + 1 else shared["state"], now, sel),
                )
                self.note_impact("overlay", False)
                return True
            return False  # never learn from a failure we did not already believe in
        hits = learn.decay(row["hits"], row["last_seen"], now) if row else 0.0
        misses = learn.decay(row["misses"], row["last_seen"], now) if row else 0.0
        streak = 0 if ok else row["streak_miss"] + 1
        state = "active" if ok else ("demoted" if streak >= learn.DEMOTE_AFTER + 1 else row["state"])
        c.execute(
            "INSERT INTO overlays(site, selector, hits, misses, streak_miss, state, last_seen) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(site, selector) DO UPDATE SET hits=excluded.hits, misses=excluded.misses,"
            " streak_miss=excluded.streak_miss, state=excluded.state, last_seen=excluded.last_seen",
            (site, sel, hits + (1 if ok else 0), misses + (0 if ok else 1), streak, state, now),
        )
        if shared is not None and ok and applied:  # a shared candidate just proved itself on a new site: confirm it
            c.execute("UPDATE overlays SET state='active', hits=hits+1, streak_miss=0, last_seen=? WHERE site='*' AND selector=?", (now, sel))
        self.store().audit_add(now, "overlays", "applied" if applied else "learned", site, f"{'ok' if ok else 'missed'} ({state})")
        if applied:
            self.note_impact("overlay", ok, 250)  # roughly one saved snapshot + click round trip
        return True

    # -- Phase 3: extractors that worked (category: extractors)
    def record_extractor(self, url, item_selector, fields, ok: bool, items: int = 0, applied: bool = False) -> bool:
        rec = recordable_url(url, self.allow_ip)
        spec = learn.canonical_extractor(item_selector, fields)
        if not rec or spec is None or not self.allow("extractors", rec[0]):
            return False
        host, site, _pattern, ptype = rec
        now = self.clock()
        c = self.store().conn()
        row = c.execute("SELECT * FROM extractors WHERE site=? AND page_type=? AND spec=?", (site, ptype, spec)).fetchone()
        if row is None and not ok:
            return False  # a recipe that never worked (a guess, a typo) is not worth remembering
        runs = learn.decay(row["runs"], row["last_seen"], now) if row else 0.0
        oks = learn.decay(row["ok"], row["last_seen"], now) if row else 0.0
        streak = 0 if ok else (row["streak_fail"] + 1 if row else 1)
        state = "active" if ok else ("demoted" if streak >= learn.DEMOTE_AFTER else (row["state"] if row else "active"))
        c.execute(
            "INSERT INTO extractors(site, page_type, spec, kind, runs, ok, streak_fail, state, items, last_seen)"
            " VALUES(?,?,?,'structured',?,?,?,?,?,?) ON CONFLICT(site, page_type, spec) DO UPDATE SET runs=excluded.runs,"
            " ok=excluded.ok, streak_fail=excluded.streak_fail, state=excluded.state, items=excluded.items,"
            " last_seen=excluded.last_seen",
            (site, ptype, spec, runs + 1, oks + (1 if ok else 0), streak, state, int(items), now),
        )
        self.store().audit_add(now, "extractors", "applied" if applied else "learned", site, f"{ptype} {'ok' if ok else 'empty'} ({state}, {items} items)")
        if applied:
            self.note_impact("extractor", ok, 900)  # roughly the page text the model no longer has to read
        return True

    def best_extractor(self, url) -> dict | None:
        """The recipe that has worked most reliably for this kind of page on this site, or None."""
        if not self.apply_on or not self.db_path.exists():
            return None
        rec = recordable_url(url, self.allow_ip)
        if not rec or not self.allow("extractors", rec[0]):
            return None
        now = self.clock()
        best = None
        for r in self.store().conn().execute(
            "SELECT * FROM extractors WHERE site=? AND page_type=? AND state='active'", (rec[1], rec[3])
        ):
            runs, oks = learn.decay(r["runs"], r["last_seen"], now), learn.decay(r["ok"], r["last_seen"], now)
            if learn.reliable(oks, runs) and (best is None or oks > best[0]):
                best = (oks, r["spec"])
        if best is None:
            return None
        try:
            d = json.loads(best[1])
            return {"item_selector": d["item"], "fields": d["fields"]}
        except (ValueError, KeyError, TypeError):
            return None

    # -- Phase 4: reflection (verify, prune, generalise)
    REFLECT_EVERY = 25  # recorded navigations between automatic passes

    def _meta_get(self, key, default=0):
        r = self.store().conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return type(default)(r["value"]) if r else default

    def _meta_set(self, key, value):
        self.store().conn().execute(
            "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
        )

    def _maybe_reflect(self) -> None:
        if self.consent().effort == "off":
            return
        n = self._meta_get("since_reflect", 0) + 1
        if n >= self.REFLECT_EVERY:
            self.reflect()
        else:
            self._meta_set("since_reflect", n)

    def reflect(self) -> dict:
        """One learning pass: forget what has faded, let benched strategies go, and share what worked across sites.
        Deterministic and local: no model call. Safe to run any time (`cloakbrowser-mcp memory reflect`)."""
        if not self.any_enabled():
            return {"memory": "off"}
        now, effort = self.clock(), self.consent().effort
        c = self.store().conn()
        out = {"forgot": 0, "unbenched": 0, "promoted": 0}
        for table, col in (("playbook", "runs"), ("extractors", "runs")):
            for r in c.execute(f"SELECT rowid AS rid, {col} AS v, last_seen FROM {table}").fetchall():
                if learn.decay(r["v"], r["last_seen"], now) < learn.MIN_EVIDENCE:
                    c.execute(f"DELETE FROM {table} WHERE rowid=?", (r["rid"],))
                    out["forgot"] += 1
        for r in c.execute("SELECT rowid AS rid, hits, last_seen FROM overlays").fetchall():
            if learn.decay(r["hits"], r["last_seen"], now) < learn.MIN_EVIDENCE:
                c.execute("DELETE FROM overlays WHERE rowid=?", (r["rid"],))
                out["forgot"] += 1
        out["unbenched"] = c.execute(
            "UPDATE playbook SET demoted_until=0, streak_fail=0 WHERE demoted_until>0 AND demoted_until<=?", (now,)
        ).rowcount
        out["unbenched"] += c.execute(
            "DELETE FROM overlays WHERE state='demoted' AND last_seen<?", (now - 30 * 86400,)
        ).rowcount
        if effort in ("medium", "high") and self.consent().enabled >= {"overlays"}:
            for r in c.execute(
                "SELECT selector FROM overlays WHERE site<>'*' AND state='active' AND hits>=?"
                " GROUP BY selector HAVING COUNT(DISTINCT site)>=3",
                (learn.MIN_OK,),
            ).fetchall():
                if c.execute("SELECT 1 FROM overlays WHERE site='*' AND selector=?", (r["selector"],)).fetchone() is None:
                    c.execute(
                        "INSERT INTO overlays(site, selector, hits, misses, streak_miss, state, last_seen) VALUES('*',?,1,0,0,'candidate',?)",  # 1 unit of evidence: survives until tried, fades if never confirmed
                        (r["selector"], now),
                    )
                    out["promoted"] += 1
        self._meta_set("since_reflect", 0)
        self._meta_set("last_reflect", now)
        self.store().audit_add(now, "reflect", "pass", "", f"forgot {out['forgot']}, unbenched {out['unbenched']}, shared {out['promoted']}")
        return out

    def learned(self) -> dict:
        """What has been learned so far, as plain numbers (no selectors, no page content)."""
        if not self.db_path.exists():
            return {}
        c = self.store().conn()
        q = lambda sql: c.execute(sql).fetchone()[0]
        out = {
            "sites_with_playbook": q("SELECT COUNT(DISTINCT site) FROM playbook"),
            "benched_strategies": q(f"SELECT COUNT(*) FROM playbook WHERE demoted_until>{self.clock()}"),
            "popup_dismissers": q("SELECT COUNT(*) FROM overlays WHERE site<>'*' AND state='active'"),
            "shared_dismissers": q("SELECT COUNT(*) FROM overlays WHERE site='*' AND state IN ('active','candidate')"),
            "extractors": q("SELECT COUNT(*) FROM extractors WHERE state='active'"),
        }
        out["impact"] = {r["kind"]: {"applied": r["applied"], "ok": r["ok"], "est_tokens_saved": r["saved_tokens"]}
                         for r in c.execute("SELECT * FROM impact")}
        return out

    def _site_row(self, site: str):
        return self.store().conn().execute("SELECT * FROM sites WHERE site=?", (site,)).fetchone()

    def _ensure_site(self, site: str, now: float):
        self.store().conn().execute(
            "INSERT OR IGNORE INTO sites(site, first_seen, last_seen, visits) VALUES(?,?,?,0)", (site, now, now)
        )

    def record_robots(self, url, crawl_delay, allowed) -> bool:
        rec = recordable_url(url, self.allow_ip)
        if not rec or not self.allow("site_stats", rec[0]):
            return False
        site, now = rec[1], self.clock()
        self._ensure_site(site, now)
        self.store().conn().execute(
            "UPDATE sites SET crawl_delay=?, robots_blocks=robots_blocks+?, last_seen=? WHERE site=?",
            (crawl_delay, 0 if allowed else 1, now, site),
        )
        self.store().audit_add(now, "site_stats", "robots", site, f"allowed={allowed} crawl_delay={crawl_delay}")
        return True

    def record_human(self, url) -> bool:
        """A person had to step in (captcha, login): remember that this site needed it."""
        rec = recordable_url(url, self.allow_ip)
        if not rec or not self.allow("site_stats", rec[0]):
            return False
        site, now = rec[1], self.clock()
        self._ensure_site(site, now)
        self.store().conn().execute("UPDATE sites SET human_needed=human_needed+1, last_seen=? WHERE site=?", (now, site))
        self.store().audit_add(now, "site_stats", "human", site, "needed a person to step in")
        return True

    def hint(self, url, config="") -> str:
        """One short line shown with navigate results. Numbers and our own labels only, never page text."""
        rec = recordable_url(url, self.allow_ip)
        if not rec or not self.db_path.exists() or not self.any_enabled():
            return ""
        host, site, pattern, ptype = rec
        parts, used = [], []
        c = self.store().conn()
        if self.allow("site_stats", host):
            row = self._site_row(site)
            if row and row["visits"]:
                used.append("site_stats")
                bits = [f"{row['visits']} visits"]
                if row["block_count"]:
                    bits.append(f"{row['block_count']} blocked")
                if row["fail_count"]:
                    bits.append(f"{row['fail_count']} failed")
                parts.append(", ".join(bits))
                cur = c.execute("SELECT * FROM config_stats WHERE site=? AND config=?", (site, config)).fetchone() if config else None
                if cur and cur["blocked"] >= 2 and cur["ok"] == 0:
                    parts.append(f"WARNING: this setup ({config}) was blocked {cur['blocked']}x here")
                best = c.execute(
                    "SELECT * FROM config_stats WHERE site=? AND config<>? AND ok>=2 AND blocked=0 ORDER BY ok DESC LIMIT 1",
                    (site, config),
                ).fetchone()
                if best and (cur is None or cur["blocked"] > cur["ok"]):
                    parts.append(f"worked before: {best['config']} ({best['ok']} ok)")
            if row and row["human_needed"]:
                parts.append(f"a person had to step in {row['human_needed']}x")
            if row and row["crawl_delay"]:
                parts.append(f"robots crawl-delay {row['crawl_delay']:g}s")
            if row and row["robots_blocks"]:
                parts.append(f"robots.txt disallowed {row['robots_blocks']} checked URL(s)")
        if self.allow("site_structure", host):
            p = c.execute("SELECT * FROM patterns WHERE site=? AND pattern=?", (site, pattern)).fetchone()
            if p:
                used.append("site_structure")
                parts.append(f"known page type: {p['page_type']} (seen {p['visits']}x)")
        if self.allow("notes", host):
            n = c.execute("SELECT COUNT(*) FROM notes WHERE site=? AND status='active'", (site,)).fetchone()[0]
            if n:
                used.append("notes")
                parts.append(f"{n} saved note(s): call memory_recall")
        if not parts:
            return ""
        return (f"MEMORY[{','.join(used)}]: " + "; ".join(parts))[:400]

    # -- notes the assistant writes (category: notes)
    def remember(self, note: str, site: str, kind: str = "lesson") -> tuple[bool, str]:
        host = _host_of(site)
        key = site_key(host, self.allow_ip)
        if not key:
            return False, "give the site as a domain or URL (IP addresses and localhost are not remembered)"
        if not self.allow("notes", host):
            return False, ("notes memory is off or not allowed for this site; only the user can enable it "
                           "(memory_request_access asks them)")
        text = " ".join((note or "").split())
        if not text:
            return False, "the note is empty"
        if len(text) > MAX_NOTE:
            return False, f"note too long ({len(text)} > {MAX_NOTE} characters); shorten it to one general lesson"
        original = text
        text = scrub_text(text)
        kind = re.sub(r"[^a-z_]", "", (kind or "lesson").lower())[:20] or "lesson"
        now = self.clock()
        st = self.store()
        c = st.conn()
        reason = screen_note(original) or screen_note(text)
        if reason:
            c.execute(
                "INSERT INTO notes(ts, site, kind, text, source, status, confidence, reason) VALUES(?,?,?,?,?,?,?,?)",
                (now, key, kind, text, "agent", "quarantined", 0.0, reason),
            )
            st.audit_add(now, "notes", "quarantined", key, reason)
            return False, f"not saved: {reason}. It was set aside for the user to review (cloakbrowser-mcp memory export)."
        norm = re.sub(r"\W+", " ", text.lower()).strip()
        for (old,) in c.execute("SELECT text FROM notes WHERE site=? AND status='active'", (key,)).fetchall():
            if re.sub(r"\W+", " ", old.lower()).strip() == norm:
                return True, "already remembered"
        c.execute(
            "INSERT INTO notes(ts, site, kind, text, source, status, confidence) VALUES(?,?,?,?,?,?,?)",
            (now, key, kind, text, "agent", "active", 0.5),
        )
        st.audit_add(now, "notes", "saved", key, f"{kind}, {len(text)} chars")
        return True, f"saved for {key}"

    def recall(self, query: str = "", site: str = "", limit: int = 10) -> dict:
        """What memory knows. Everything is gated per category and site; note text is untrusted data."""
        if not self.any_enabled() or not self.db_path.exists():
            return {"memory": "off or empty", "hint": "the user can enable it; see memory_request_access"}
        out: dict = {}
        c = self.store().conn()
        limit = max(1, min(int(limit), 50))
        host = _host_of(site)
        key = site_key(host, self.allow_ip) if host else ""
        if key:
            out["site"] = key
            if self.allow("site_stats", host):
                row = self._site_row(key)
                if row:
                    out["stats"] = {k: row[k] for k in row.keys() if k != "site"}
                out["configs"] = [dict(r) for r in c.execute(
                    "SELECT config, runs, ok, blocked FROM config_stats WHERE site=? ORDER BY runs DESC LIMIT ?", (key, limit))]
            if self.allow("site_structure", host):
                out["page_types"] = [dict(r) for r in c.execute(
                    "SELECT pattern, page_type, visits FROM patterns WHERE site=? ORDER BY visits DESC LIMIT ?", (key, limit))]
        if (key and self.allow("notes", host)) or (query and not key):
            sql, args = "SELECT id, site, kind, text, confidence, ts FROM notes WHERE status='active'", []
            if key:
                sql, args = sql + " AND site=?", [key]
            for tok in re.findall(r"\w+", query.lower())[:6]:
                sql, args = sql + " AND lower(text) LIKE ?", args + [f"%{tok}%"]
            rows = [dict(r) for r in c.execute(sql + " ORDER BY ts DESC LIMIT ?", args + [limit * 3])]
            out["notes"] = [r for r in rows if self.allow("notes", r["site"])][:limit]
        return out or {"memory": "nothing to show for this request"}

    # -- reporting
    def status(self) -> dict:
        c = self.consent()
        db = {"path": str(self.db_path), "exists": self.db_path.exists()}
        counts, learned = {}, {}
        if db["exists"]:
            db["size_kb"] = round(self.db_path.stat().st_size / 1024, 1)
            counts = self.store().counts()
            learned = self.learned()
        return {
            "enabled": bool(c.enabled) and not self.incognito,
            "incognito": self.incognito,
            "source": c.source,
            "auto_apply": self.apply_on,
            "learned": learned,
            "preset": c.preset if c.source != "none" else "off",
            "categories": {cat: cat in c.enabled for cat in CATEGORIES},
            "scope": {"mode": c.scope_mode, "domains": list(c.scope_domains)},
            "retention_days": c.retention_days,
            "effort": c.effort,
            "reflect_when": c.reflect_when,
            "ask": c.ask,
            "needs_reconsent": list(c.needs_reconsent),
            "db": db,
            "counts": counts,
        }

    def audit_log(self, limit: int = 20) -> list:
        if not self.db_path.exists():
            return []
        return self.store().audit_list(limit)

    def export(self, path: Path) -> Path:
        data = {"consent": self.status(), "data": self.store().dump() if self.db_path.exists() else {}}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path


# ---------------------------------------------------------------- the permission prompt (MCP elicitation)
# The MCP SDK only allows str/int/float/bool/list[str] fields in a prompt, so choices are plain strings that
# advertise their options as a JSON-schema enum (clients can render dropdowns) and are validated here.
FORM_CHOICES = {
    "decision": ("enable", "not_now", "never_ask"),
    "scope_mode": ("all_sites", "only_listed_sites", "all_except_listed"),
    "retention_days": ("30", "90", "365", "forever"),
    "effort": EFFORTS,
    "reflect_when": REFLECT_WHEN,
}


def _choice(default: str, key: str, description: str):
    opts = FORM_CHOICES[key]
    return Field(default, description=f"{description} Options: {', '.join(opts)}.", json_schema_extra={"enum": list(opts)})


class ConsentForm(BaseModel):
    decision: str = _choice(
        "enable", "decision",
        "enable = turn on what is ticked below; not_now = ask again in a week; never_ask = stop asking.",
    )
    site_stats: bool = Field(True, description="Remember per-site success/block counts, status codes, load times and which settings worked")
    site_structure: bool = Field(True, description="Remember URL patterns like /c/{id}/{slug} and page types (no query values or IDs)")
    extractors: bool = Field(True, description="Remember selectors and JSON-LD/API endpoints that extracted data (not the data itself)")
    overlays: bool = Field(True, description="Remember cookie-banner and pop-up selectors that were dismissed")
    recipes: bool = Field(False, description="Remember replayable step sequences (placeholders only, never typed values)")
    notes: bool = Field(False, description="Allow the assistant to save short, scrubbed lessons about a site")
    scope_mode: str = _choice(
        "all_sites", "scope_mode",
        "Which sites may be remembered (banking, email, health, government and adult sites are always excluded).",
    )
    scope_domains: str = Field("", description="Comma-separated domains for the two 'listed' options, e.g. skroutz.gr, example.com")
    retention_days: str = _choice("90", "retention_days", "Delete remembered data after this many days.")
    effort: str = _choice(
        "low", "effort", "How much reflection the assistant is asked to do to improve memory quality (off = none).",
    )
    reflect_when: str = _choice(
        "manual", "reflect_when",
        "When to remind the assistant to reflect: only on request, at session end, or after repeated failures.",
    )

    @field_validator("decision", "scope_mode", "retention_days", "effort", "reflect_when")
    @classmethod
    def _one_of_the_options(cls, v: str, info):
        if v not in FORM_CHOICES[info.field_name]:
            raise ValueError(f"{info.field_name} must be one of: {', '.join(FORM_CHOICES[info.field_name])}")
        return v


_SCOPE_MAP = {"all_sites": "all", "only_listed_sites": "allowlist", "all_except_listed": "blocklist"}


def consent_message() -> str:
    lines = [
        "Allow cloakbrowser-mcp to remember things about the sites you browse?",
        "Nothing is stored unless you agree. Data stays on this computer (~/.cloakbrowser-mcp/memory) and you can "
        "change or delete it anytime with `cloakbrowser-mcp memory status | disable | wipe`.",
        "",
    ]
    for cat in CATEGORIES:
        stores, never = CATEGORY_INFO[cat]
        lines.append(f"- {cat}: {stores}. Never stores: {never}.")
    return "\n".join(lines)


def apply_form(mem: Memory, form: ConsentForm) -> str:
    """Turn a user's answers to the prompt into stored consent. Returns a short summary."""
    now = mem.clock()
    c = mem.consent()
    if form.decision == "never_ask":
        mem.apply_consent(c.enabled, c.scope_mode, c.scope_domains, c.retention_days, c.effort, c.reflect_when, "never")
        return "The user chose never to be asked about memory. Memory stays as it was."
    if form.decision == "not_now":
        mem.apply_consent(c.enabled, c.scope_mode, c.scope_domains, c.retention_days, c.effort, c.reflect_when, "later")
        return "The user said not now. Memory stays as it was; do not ask again this session."
    cats = [cat for cat in CATEGORIES if getattr(form, cat)]
    mode = _SCOPE_MAP[form.scope_mode]
    new = mem.apply_consent(
        cats,
        mode,
        form.scope_domains if mode != "all" else (),
        form.retention_days,
        form.effort,
        form.reflect_when,
        "ask",
    )
    if not new.enabled:
        return "The user left every category unticked, so memory stays off."
    return (
        f"Memory enabled by the user. Categories: {', '.join(c for c in CATEGORIES if c in new.enabled)}. "
        f"Scope: {new.scope_mode}{' ' + ', '.join(new.scope_domains) if new.scope_domains else ''}. "
        f"Retention: {new.retention_days or 'forever'} days. Effort: {new.effort}."
    )


FALLBACK = (
    "Could not show the permission prompt in this client. Memory stays OFF. Ask the user to run "
    "`cloakbrowser-mcp memory setup` in a terminal (or set CLOAK_MEMORY=standard in the MCP registration). "
    "You cannot enable memory yourself."
)


async def request_access(mem: Memory, ctx, timeout_s: float = 300) -> str:
    """Ask the user, through the client's own UI, whether memory may be enabled. Fails closed."""
    if _from_env(mem.env) is not None:
        return "Memory is controlled by the CLOAK_MEMORY environment variable set by the user in the MCP registration; it cannot be changed here."
    if mem.prompts >= 2:
        return "The user was already asked twice this session. Do not ask again; they can run `cloakbrowser-mcp memory setup`."
    mem.prompts += 1
    mem.hinted = True
    try:
        res = await asyncio.wait_for(ctx.elicit(message=consent_message(), schema=ConsentForm), timeout_s)
    except asyncio.TimeoutError:
        return "The user did not answer the permission prompt. Memory stays OFF."
    except Exception as e:
        _log.warning("permission prompt failed: %s: %s", type(e).__name__, str(e)[:300])
        return FALLBACK
    if getattr(res, "action", None) != "accept" or getattr(res, "data", None) is None:
        return "The user declined or dismissed the permission prompt. Memory stays OFF; do not ask again this session."
    try:
        return apply_form(mem, res.data)
    except (ValueError, PermissionError) as e:
        return f"Memory stays unchanged: {e}"


# ---------------------------------------------------------------- command line (run by the user)
def _prompt_choice(question, options, default):
    while True:
        ans = input(f"{question} [{'/'.join(options)}] ({default}): ").strip().lower() or default
        if ans in options:
            return ans
        print("  please choose one of:", ", ".join(options))


def _prompt_yes(question, default):
    ans = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not ans else ans.startswith("y")


def _setup_args(p):
    p.add_argument("--yes", action="store_true", help="do not prompt; use the flags below")
    p.add_argument("--preset", choices=[k for k in PRESETS if k != "off"] + ["off"], default=None)
    p.add_argument("--categories", default=None, help="comma list, overrides --preset")
    p.add_argument("--scope", choices=SCOPE_MODES, default="all")
    p.add_argument("--domains", default="")
    p.add_argument("--retention", default=str(DEFAULT_RETENTION), help="days or 'forever'")
    p.add_argument("--effort", choices=EFFORTS, default="low")
    p.add_argument("--reflect", choices=REFLECT_WHEN, default="manual")


def cli_main(argv: list[str], mem: Memory | None = None, out=print) -> int:
    mem = mem or Memory(default_on=default_on_from_env())
    ap = argparse.ArgumentParser(prog="cloakbrowser-mcp memory", description="Manage cloakbrowser-mcp site memory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _setup_args(sub.add_parser("setup", help="choose what may be remembered (interactive, or flags with --yes)"))
    _setup_args(sub.add_parser("enable", help="same as setup --yes"))
    sub.add_parser("status", help="show what is on and what is stored")
    sub.add_parser("reflect", help="run one learning pass now (forget faded evidence, un-bench, share what worked across sites)")
    d = sub.add_parser("disable", help="turn a category (or all) off and delete its data")
    d.add_argument("category", nargs="?", default="all")
    f = sub.add_parser("forget", help="delete everything remembered about a site")
    f.add_argument("site")
    e = sub.add_parser("export", help="write everything stored to a readable JSON file")
    e.add_argument("file", nargs="?", default="memory-export.json")
    w = sub.add_parser("wipe", help="delete the whole database (keeps your consent choices)")
    w.add_argument("--yes", action="store_true")
    r = sub.add_parser("reset", help="delete the database AND the consent record (back to the first-run state)")
    r.add_argument("--yes", action="store_true")
    a = sub.add_parser("audit", help="show the most recent writes")
    a.add_argument("-n", type=int, default=20)
    ns = ap.parse_args(argv)

    try:
        if ns.cmd in ("setup", "enable"):
            interactive = ns.cmd == "setup" and not ns.yes
            if interactive and not sys.stdin.isatty():
                out("stdin is not a terminal: use `memory enable` (or `setup --yes`) with flags")
                return 2
            if interactive:
                out(consent_message() + "\n")
                cats = [c for c in CATEGORIES if _prompt_yes(f"Allow {c}?", c in PRESETS["standard"])]
                scope = _prompt_choice("Which sites", SCOPE_MODES, "all")
                domains = input("Domains (comma separated): ").strip() if scope != "all" else ""
                retention = _prompt_choice("Keep data for days", ("30", "90", "365", "forever"), "90")
                effort = _prompt_choice("Reflection effort", EFFORTS, "low")
                reflect = _prompt_choice("Remind to reflect", REFLECT_WHEN, "manual")
            else:
                if ns.categories:
                    cats = [c.strip() for c in ns.categories.split(",") if c.strip()]
                else:
                    cats = list(PRESETS.get(ns.preset or "standard", ()))
                scope, domains, retention, effort, reflect = ns.scope, ns.domains, ns.retention, ns.effort, ns.reflect
            before = mem.consent().enabled
            new = mem.apply_consent(cats, scope, domains, retention, effort, reflect, "ask")
            dropped = sorted(before - new.enabled)
            out(f"Saved. Enabled: {', '.join(c for c in CATEGORIES if c in new.enabled) or 'nothing'}")
            if dropped:
                out(f"Data deleted for categories turned off: {', '.join(dropped)}")
        elif ns.cmd == "status":
            out(json.dumps(mem.status(), indent=2))
        elif ns.cmd == "reflect":
            out(json.dumps(mem.reflect()))
        elif ns.cmd == "disable":
            out(mem.disable(ns.category))
        elif ns.cmd == "forget":
            out(f"deleted {mem.forget_site(ns.site)} rows about {ns.site}")
        elif ns.cmd == "export":
            out(f"wrote {mem.export(Path(ns.file))}")
        elif ns.cmd in ("wipe", "reset"):
            if not ns.yes and not (sys.stdin.isatty() and _prompt_yes(f"Really {ns.cmd}?", False)):
                out("not confirmed (use --yes)")
                return 1
            if ns.cmd == "reset":
                mem.reset_consent()
                result = "deleted"
            else:
                result = mem.wipe()
            out("done" if result == "deleted" else "done (a running server has the database open, so it was emptied instead of deleted)")
        elif ns.cmd == "audit":
            for row in mem.audit_log(ns.n):
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"]))
                out(f"{stamp}  {row['category']:<14} {row['action']:<9} {row['site']:<24} {row['summary']}")
    except (ValueError, PermissionError) as e:
        out(f"error: {e}")
        return 1
    return 0


class ConfirmWipe(BaseModel):
    delete_data: bool = Field(True, description="Also delete everything already remembered")


async def disable_with_confirm(mem: Memory, category: str, ctx, timeout_s: float = 120) -> str:
    """Narrow memory. Turning off ALL of it asks the user whether to delete the stored data too."""
    if category != "all":
        return mem.disable(category, delete=True)
    try:
        res = await asyncio.wait_for(
            ctx.elicit(message="Turn all site memory off? Choose whether to delete what was already remembered.", schema=ConfirmWipe),
            timeout_s,
        )
    except Exception:
        return mem.disable("all", delete=False) + " (could not ask about deleting; run `cloakbrowser-mcp memory wipe` to delete it)"
    if getattr(res, "action", None) != "accept" or getattr(res, "data", None) is None:
        return "cancelled by the user; memory unchanged"
    return mem.disable("all", delete=res.data.delete_data)
