"""CloakBrowser MCP server: stealth Chromium exposed as MCP tools (stdio).

Env defaults (all overridable at runtime with the `configure` tool):
  CLOAK_HEADLESS=1|0  CLOAK_HUMANIZE=1|0  CLOAK_PROXY=url  CLOAK_PROFILE=name
  CLOAK_GEOIP=1|0     CLOAK_LOCALE=el-GR  CLOAK_TIMEZONE=Europe/Athens
  CLOAK_PROXIES=url1,url2,...  (rotation list)
"""
import asyncio
import functools
import json
import logging
import os
import re
from collections import deque
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image
from cloakbrowser import launch_context_async, launch_persistent_context_async

HOME = Path(os.environ.get("CLOAK_MCP_HOME", Path.home() / ".cloakbrowser-mcp"))
PROFILES = HOME / "profiles"
OUT_DIR = HOME / "out"
LOG_DIR = HOME / "logs"
for _d in (PROFILES, OUT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_DIR / "server.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("cloakbrowser-mcp")

mcp = FastMCP("cloakbrowser")


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v not in ("0", "false", "False", "")


class State:
    def __init__(self):
        self.cfg = {
            "headless": _env_bool("CLOAK_HEADLESS", True),
            "humanize": _env_bool("CLOAK_HUMANIZE", True),
            "geoip": _env_bool("CLOAK_GEOIP", False),
            "proxy": os.environ.get("CLOAK_PROXY") or None,
            "profile": os.environ.get("CLOAK_PROFILE") or None,
            "locale": os.environ.get("CLOAK_LOCALE") or None,
            "timezone": os.environ.get("CLOAK_TIMEZONE") or None,
            "capture_console": _env_bool("CLOAK_CAPTURE_CONSOLE", False),
        }
        self.proxies = [p for p in os.environ.get("CLOAK_PROXIES", "").split(",") if p]
        self.proxy_idx = -1
        self.ctx = None
        self.pages = {}  # id -> page
        self.cur = None
        self.next_id = 1
        self.console = {}  # page id -> deque
        self.net = {}  # page id -> deque of dicts
        self.responses = {}  # (page id, idx) -> Response
        self.net_counter = 0
        self.dialog_policy = "accept"
        self.dialogs = deque(maxlen=20)
        self.lock = asyncio.Lock()


S = State()
MAX_NET = 400


# ---------------------------------------------------------------- lifecycle
def _register_page(page):
    pid = S.next_id
    S.next_id += 1
    S.pages[pid] = page
    S.console[pid] = deque(maxlen=300)
    S.net[pid] = deque(maxlen=MAX_NET)

    def on_console(m):
        S.console[pid].append({"type": m.type, "text": m.text[:500]})

    def on_pageerror(e):
        S.console[pid].append({"type": "pageerror", "text": str(e)[:500]})

    def on_response(r):
        S.net_counter += 1
        idx = S.net_counter
        try:
            rt = r.request.resource_type
            method = r.request.method
        except Exception:
            rt, method = "?", "?"
        S.net[pid].append({"i": idx, "status": r.status, "method": method, "type": rt, "url": r.url})
        S.responses[(pid, idx)] = r
        old = idx - MAX_NET
        S.responses.pop((pid, old), None)

    async def on_dialog(d):
        S.dialogs.append({"type": d.type, "message": d.message})
        try:
            if S.dialog_policy == "dismiss":
                await d.dismiss()
            else:
                await d.accept()
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)
    page.on("dialog", on_dialog)
    page.on("close", lambda _p=None: S.pages.pop(pid, None))
    S.cur = pid
    return pid


async def _launch():
    c = S.cfg
    kw = {
        "headless": c["headless"],
        "humanize": c["humanize"],
        "geoip": c["geoip"],
        "proxy": c["proxy"],
        "locale": c["locale"],
        "timezone": c["timezone"],
    }
    kw = {k: v for k, v in kw.items() if v is not None}
    if c["profile"]:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", c["profile"])
        ctx = await launch_persistent_context_async(str(PROFILES / safe), **kw)
    else:
        ctx = await launch_context_async(**kw)
    if c["capture_console"]:
        await ctx.add_init_script(JS_CONSOLE_HOOK)
    S.ctx = ctx
    S.pages.clear()
    S.console.clear()
    S.net.clear()
    S.responses.clear()
    for p in ctx.pages:
        _register_page(p)
    ctx.on("page", _register_page)
    log.info("browser launched cfg=%s", {k: v for k, v in c.items() if k != "proxy"})


async def _alive() -> bool:
    if S.ctx is None:
        return False
    try:
        await asyncio.wait_for(S.ctx.cookies(), timeout=5)
        return True
    except Exception:
        return False


async def _shutdown():
    ctx, S.ctx = S.ctx, None
    S.pages.clear()
    S.cur = None
    if ctx is not None:
        try:
            await asyncio.wait_for(ctx.close(), timeout=15)
        except Exception as e:  # already dead or hung
            log.warning("close failed: %s", e)


async def _ensure():
    """Return the current page, (re)launching the browser if needed."""
    async with S.lock:
        if not await _alive():
            if S.ctx is not None:
                log.warning("browser dead, relaunching")
                await _shutdown()
            await _launch()
        if S.cur not in S.pages or S.pages[S.cur].is_closed():
            live = [i for i, p in S.pages.items() if not p.is_closed()]
            if live:
                S.cur = live[-1]
            else:
                await S.ctx.new_page()  # 'page' event registers it
                await asyncio.sleep(0)
        return S.pages[S.cur]


def tool(fn):
    """Register an MCP tool; turn exceptions into short error strings and log them."""

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except Exception as e:
            log.exception("tool %s failed", fn.__name__)
            msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            return f"ERROR in {fn.__name__}: {type(e).__name__}: {msg[:400]}"

    mcp.tool()(wrapper)
    return wrapper


def _clip(s: str, n: int) -> str:
    if s is None:
        return ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n...[truncated {len(s) - n} chars; raise max_chars to see more]"


def _loc(page, target: str):
    """Resolve a snapshot ref (e12) or any Playwright selector to a locator."""
    if re.fullmatch(r"e\d+", target):
        return page.locator(f'[data-cm-ref="{target}"]').first
    return page.locator(target).first


# ---------------------------------------------------------------- JS helpers
JS_SNAPSHOT = """(max) => {
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],[role=option],[onclick],[contenteditable=true]';
  document.querySelectorAll('[data-cm-ref]').forEach(e => e.removeAttribute('data-cm-ref'));
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (n >= max) break;
    const ref = 'e' + (++n); el.setAttribute('data-cm-ref', ref);
    const tag = el.tagName.toLowerCase();
    const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.value || el.title || el.alt || el.name || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    const o = {ref, tag, label};
    if (tag === 'a') o.href = el.getAttribute('href');
    if (tag === 'input') { o.type = el.type; if (el.value && el.type !== 'password') o.value = el.value.slice(0, 40); }
    if (tag === 'select') o.options = [...el.options].slice(0, 12).map(x => x.text.trim());
    if (el.disabled) o.disabled = true;
    out.push(o);
  }
  return {title: document.title, url: location.href, items: out};
}"""

JS_MARKDOWN = """(root) => {
  const el = document.querySelector(root) || document.body;
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','IFRAME','TEMPLATE']);
  function walk(n, ctx) {
    if (n.nodeType === 3) return n.textContent.replace(/\\s+/g, ' ');
    if (n.nodeType !== 1 || skip.has(n.tagName)) return '';
    const st = getComputedStyle(n); if (st.display === 'none' || st.visibility === 'hidden') return '';
    const kids = () => [...n.childNodes].map(c => walk(c, ctx)).join('');
    const t = n.tagName;
    if (/^H[1-6]$/.test(t)) return '\\n\\n' + '#'.repeat(+t[1]) + ' ' + kids().trim() + '\\n\\n';
    if (t === 'P' || t === 'DIV' || t === 'SECTION' || t === 'ARTICLE') return '\\n' + kids() + '\\n';
    if (t === 'BR') return '\\n';
    if (t === 'A') { const h = n.getAttribute('href'); const k = kids().trim(); return h && k ? '[' + k + '](' + h + ')' : k; }
    if (t === 'IMG') return n.alt ? '![' + n.alt + ']' : '';
    if (t === 'STRONG' || t === 'B') return '**' + kids().trim() + '**';
    if (t === 'EM' || t === 'I') return '_' + kids().trim() + '_';
    if (t === 'LI') return '\\n- ' + kids().trim();
    if (t === 'UL' || t === 'OL') return '\\n' + kids() + '\\n';
    if (t === 'PRE') return '\\n```\\n' + n.innerText + '\\n```\\n';
    if (t === 'CODE') return '`' + kids() + '`';
    if (t === 'TR') return '\\n| ' + [...n.children].map(c => c.innerText.trim().replace(/\\|/g, '/')).join(' | ') + ' |';
    if (t === 'TABLE') return '\\n' + kids() + '\\n';
    return kids();
  }
  return walk(el, {}).replace(/\\n{3,}/g, '\\n\\n').trim();
}"""

JS_EXTRACT = """([sel, fields, limit]) => [...document.querySelectorAll(sel)].slice(0, limit).map(it => {
  const o = {};
  for (const [k, spec] of Object.entries(fields)) {
    const [css, attr] = spec.split('@');
    const el = css ? it.querySelector(css) : it;
    if (!el) { o[k] = null; continue; }
    o[k] = attr ? el.getAttribute(attr) : (el.innerText || '').trim().replace(/\\s+/g, ' ');
  }
  return o;
})"""

JS_LINKS = """() => [...document.querySelectorAll('a[href]')].map(a => ({text: (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 120), href: a.href}))"""

JS_STEALTH = """() => {
  const c = document.createElement('canvas'); const gl = c.getContext('webgl');
  let vendor = null, renderer = null;
  if (gl) { const e = gl.getExtension('WEBGL_debug_renderer_info'); if (e) { vendor = gl.getParameter(e.UNMASKED_VENDOR_WEBGL); renderer = gl.getParameter(e.UNMASKED_RENDERER_WEBGL); } }
  return {
    webdriver: navigator.webdriver, userAgent: navigator.userAgent, platform: navigator.platform,
    languages: navigator.languages, plugins: navigator.plugins.length, hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory, hasChromeObj: !!window.chrome, webglVendor: vendor, webglRenderer: renderer,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, screen: [screen.width, screen.height],
    outer: [outerWidth, outerHeight], headlessUA: /Headless/i.test(navigator.userAgent),
  };
}"""


# CloakBrowser suppresses CDP console events (stealth), so console capture is an opt-in page hook.
JS_CONSOLE_HOOK = """(() => {
  if (window.__cm_console) return;
  const buf = window.__cm_console = [];
  for (const k of ['log','info','warn','error','debug']) {
    const orig = console[k].bind(console);
    console[k] = (...a) => { try { buf.push({type: k, text: a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ').slice(0, 500)}); if (buf.length > 300) buf.shift(); } catch (e) {} orig(...a); };
  }
  addEventListener('error', e => buf.push({type: 'pageerror', text: String(e.message).slice(0, 500)}));
  addEventListener('unhandledrejection', e => buf.push({type: 'pageerror', text: 'unhandled rejection: ' + String(e.reason).slice(0, 400)}));
})();"""


# ---------------------------------------------------------------- config
@tool
async def configure(
    headless: bool | None = None,
    humanize: bool | None = None,
    proxy: str | None = None,
    proxies: list[str] | None = None,
    profile: str | None = None,
    locale: str | None = None,
    timezone: str | None = None,
    geoip: bool | None = None,
    capture_console: bool | None = None,
    restart: bool = True,
) -> str:
    """Change browser settings at runtime (restarts the browser). Pass profile='name' for persistent
    cookies/logins, '' to go back to a throwaway session. proxy='' clears the proxy. headless=False
    shows a real window (useful for captchas). capture_console=True enables console_logs (slightly
    reduces stealth). Call with no args to view current settings."""
    changed = False
    for key, val in (
        ("headless", headless), ("humanize", humanize), ("geoip", geoip), ("capture_console", capture_console)
    ):
        if val is not None:
            S.cfg[key] = val
            changed = True
    for key, val in (("proxy", proxy), ("profile", profile), ("locale", locale), ("timezone", timezone)):
        if val is not None:
            S.cfg[key] = val or None
            changed = True
    if proxies is not None:
        S.proxies = proxies
        S.proxy_idx = -1
    if changed and restart and S.ctx is not None:
        await _shutdown()
    shown = dict(S.cfg)
    if shown["proxy"]:
        shown["proxy"] = re.sub(r"//[^@/]*@", "//***@", shown["proxy"])
    return json.dumps({"config": shown, "proxy_list": len(S.proxies), "running": S.ctx is not None})


@tool
async def rotate_proxy() -> str:
    """Switch to the next proxy in the configured list (see configure proxies=[...]) and restart."""
    if not S.proxies:
        return "ERROR: no proxy list; call configure(proxies=[...]) first"
    S.proxy_idx = (S.proxy_idx + 1) % len(S.proxies)
    S.cfg["proxy"] = S.proxies[S.proxy_idx]
    await _shutdown()
    return f"proxy #{S.proxy_idx + 1}/{len(S.proxies)} selected; browser restarts on next call"


@tool
async def close_browser() -> str:
    """Close the browser and free resources (persistent profiles are saved)."""
    await _shutdown()
    return "closed"


# ---------------------------------------------------------------- tabs
@tool
async def tabs_list() -> str:
    """List open tabs with ids, URLs and titles. The current one is marked with *."""
    await _ensure()
    rows = []
    for pid, p in list(S.pages.items()):
        if p.is_closed():
            continue
        try:
            t = await p.title()
        except Exception:
            t = "?"
        rows.append(f"{'*' if pid == S.cur else ' '} {pid}: {p.url} | {t[:80]}")
    return "\n".join(rows)


@tool
async def tab_new(url: str = "") -> str:
    """Open a new tab (optionally navigating to url) and make it current."""
    await _ensure()
    page = await S.ctx.new_page()
    await asyncio.sleep(0)
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    return f"tab {S.cur} opened: {page.url}"


@tool
async def tab_select(tab_id: int) -> str:
    """Make a tab current by id (see tabs_list)."""
    await _ensure()
    if tab_id not in S.pages or S.pages[tab_id].is_closed():
        return f"ERROR: no such tab {tab_id}"
    S.cur = tab_id
    await S.pages[tab_id].bring_to_front()
    return f"tab {tab_id}: {S.pages[tab_id].url}"


@tool
async def tab_close(tab_id: int | None = None) -> str:
    """Close a tab (default: current)."""
    await _ensure()
    tid = tab_id or S.cur
    p = S.pages.get(tid)
    if p is None:
        return f"ERROR: no such tab {tid}"
    await p.close()
    S.pages.pop(tid, None)
    live = list(S.pages)
    S.cur = live[-1] if live else None
    return f"closed {tid}; current={S.cur}"


# ---------------------------------------------------------------- navigation
@tool
async def navigate(url: str, wait_until: str = "domcontentloaded", settle_ms: int = 500) -> str:
    """Open a URL. wait_until: commit|domcontentloaded|load|networkidle. Returns final URL, status, title."""
    page = await _ensure()
    resp = await page.goto(url, wait_until=wait_until, timeout=60000)
    if settle_ms:
        await page.wait_for_timeout(settle_ms)
    return f"{page.url} | status={resp.status if resp else '?'} | title={await page.title()}"


@tool
async def go_back() -> str:
    """Navigate back in history."""
    page = await _ensure()
    await page.go_back(wait_until="domcontentloaded")
    return page.url


@tool
async def reload() -> str:
    """Reload the current page."""
    page = await _ensure()
    await page.reload(wait_until="domcontentloaded")
    return page.url


@tool
async def wait_for(
    selector: str = "",
    text: str = "",
    network_idle: bool = False,
    timeout_ms: int = 15000,
) -> str:
    """Wait for a selector/ref to appear, for text to appear on the page, or for network idle."""
    page = await _ensure()
    if selector:
        await _loc(page, selector).wait_for(timeout=timeout_ms)
    if text:
        await page.get_by_text(text).first.wait_for(timeout=timeout_ms)
    if network_idle:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    if not (selector or text or network_idle):
        await page.wait_for_timeout(min(timeout_ms, 10000))
    return "ok"


# ---------------------------------------------------------------- reading
@tool
async def snapshot(max_items: int = 150) -> str:
    """List interactive elements (links, buttons, inputs) with refs like e12. Use the ref as `selector`
    in click/type_text/hover/select_option. Refs are re-assigned on every snapshot call."""
    page = await _ensure()
    d = await page.evaluate(JS_SNAPSHOT, max_items)
    lines = [f"# {d['title']}\n{d['url']}"]
    for it in d["items"]:
        extra = "".join(f" {k}={v!r}" for k, v in it.items() if k not in ("ref", "tag", "label"))
        lines.append(f"[{it['ref']}] {it['tag']} \"{it['label']}\"{extra}")
    return "\n".join(lines)


@tool
async def get_text(selector: str = "body", max_chars: int = 15000) -> str:
    """Visible text of an element (default whole page)."""
    page = await _ensure()
    return _clip(await _loc(page, selector).inner_text(timeout=15000), max_chars)


@tool
async def get_html(selector: str = "body", max_chars: int = 30000) -> str:
    """Outer HTML of an element."""
    page = await _ensure()
    html = await _loc(page, selector).evaluate("e => e.outerHTML")
    return _clip(html, max_chars)


@tool
async def get_markdown(selector: str = "body", max_chars: int = 20000) -> str:
    """Page (or element) converted to compact markdown with headings, links, lists and tables."""
    page = await _ensure()
    return _clip(await page.evaluate(JS_MARKDOWN, selector), max_chars)


@tool
async def extract_links(pattern: str = "", limit: int = 100, unique: bool = True) -> str:
    """All links on the page as JSON [{text, href}]. `pattern` is a regex matched on href or text."""
    page = await _ensure()
    links = await page.evaluate(JS_LINKS)
    if pattern:
        rx = re.compile(pattern, re.I)
        links = [l for l in links if rx.search(l["href"]) or rx.search(l["text"])]
    if unique:
        seen, out = set(), []
        for l in links:
            if l["href"] not in seen:
                seen.add(l["href"])
                out.append(l)
        links = out
    return json.dumps(links[:limit], ensure_ascii=False)


@tool
async def extract_structured(item_selector: str, fields: dict[str, str], limit: int = 50) -> str:
    """Extract repeated items (e.g. product cards) as JSON. fields maps name -> 'css' (inner text),
    'css@attr' (attribute) or '@attr' (attribute of the item itself). Example:
    item_selector='li.card', fields={'name':'h2','price':'.price','url':'a@href'}"""
    page = await _ensure()
    rows = await page.evaluate(JS_EXTRACT, [item_selector, fields, limit])
    return json.dumps({"count": len(rows), "items": rows}, ensure_ascii=False)


@tool
async def screenshot(selector: str = "", full_page: bool = False) -> Image:
    """PNG screenshot of the page or one element."""
    page = await _ensure()
    if selector:
        data = await _loc(page, selector).screenshot(timeout=15000)
    else:
        data = await page.screenshot(full_page=full_page)
    return Image(data=data, format="png")


@tool
async def save_pdf(filename: str = "page.pdf") -> str:
    """Save the page as PDF (headless mode only). Files go to the output dir."""
    page = await _ensure()
    path = OUT_DIR / Path(filename).name
    await page.pdf(path=str(path))
    return str(path)


@tool
async def save_html(filename: str = "page.html") -> str:
    """Save the full page HTML to the output dir."""
    page = await _ensure()
    path = OUT_DIR / Path(filename).name
    path.write_text(await page.content(), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------- interaction
@tool
async def click(selector: str, double: bool = False) -> str:
    """Click an element by snapshot ref (e12) or CSS/text selector."""
    page = await _ensure()
    loc = _loc(page, selector)
    if double:
        await loc.dblclick(timeout=15000)
    else:
        await loc.click(timeout=15000)
    return f"clicked {selector}; url={page.url}"


@tool
async def hover(selector: str) -> str:
    """Hover over an element."""
    page = await _ensure()
    await _loc(page, selector).hover(timeout=15000)
    return "hovered"


@tool
async def type_text(selector: str, text: str, submit: bool = False, clear: bool = True) -> str:
    """Type into an input; optionally clear first and press Enter after."""
    page = await _ensure()
    loc = _loc(page, selector)
    if clear:
        await loc.fill("", timeout=15000)
    await loc.press_sequentially(text, timeout=30000)
    if submit:
        await loc.press("Enter")
    return f"typed into {selector}; url={page.url}"


@tool
async def fill_form(fields: dict[str, str], submit_selector: str = "") -> str:
    """Fill several inputs at once: {selector_or_ref: value}. Optionally click a submit element."""
    page = await _ensure()
    for sel, val in fields.items():
        await _loc(page, sel).fill(val, timeout=15000)
    if submit_selector:
        await _loc(page, submit_selector).click(timeout=15000)
    return f"filled {len(fields)} fields; url={page.url}"


@tool
async def press_key(key: str) -> str:
    """Press a key or combo, e.g. Enter, Escape, Control+A, PageDown."""
    page = await _ensure()
    await page.keyboard.press(key)
    return f"pressed {key}"


@tool
async def select_option(selector: str, value: str) -> str:
    """Select an option in a <select> by value or visible label."""
    page = await _ensure()
    loc = _loc(page, selector)
    try:
        await loc.select_option(value=value, timeout=5000)
    except Exception:
        await loc.select_option(label=value, timeout=15000)
    return f"selected {value}"


@tool
async def scroll(direction: str = "down", amount: int = 800, selector: str = "") -> str:
    """Scroll the page by pixels (down|up|top|bottom), or scroll an element into view."""
    page = await _ensure()
    if selector:
        await _loc(page, selector).scroll_into_view_if_needed(timeout=15000)
    elif direction == "top":
        await page.evaluate("window.scrollTo(0,0)")
    elif direction == "bottom":
        await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
    else:
        dy = amount if direction == "down" else -amount
        await page.evaluate("dy => window.scrollBy({top: dy, behavior: 'instant'})", dy)
    return str(await page.evaluate("Math.round(window.scrollY)"))


@tool
async def scroll_to_load(max_scrolls: int = 10, pause_ms: int = 800) -> str:
    """Scroll to the bottom repeatedly until the page height stops growing (infinite scroll)."""
    page = await _ensure()
    last, n = -1, 0
    for n in range(1, max_scrolls + 1):
        h = await page.evaluate("document.body.scrollHeight")
        if h == last:
            n -= 1
            break
        last = h
        await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)
    return f"scrolled {n} times; height={last}"


@tool
async def upload_file(selector: str, path: str) -> str:
    """Attach a local file to an <input type=file>."""
    page = await _ensure()
    await _loc(page, selector).set_input_files(path, timeout=15000)
    return "uploaded"


@tool
async def handle_dialog(action: str = "accept") -> str:
    """Set how JS alert/confirm/prompt dialogs are handled ('accept' or 'dismiss') and show recent ones."""
    if action not in ("accept", "dismiss"):
        return "ERROR: action must be accept or dismiss"
    S.dialog_policy = action
    return json.dumps({"policy": action, "recent": list(S.dialogs)}, ensure_ascii=False)


@tool
async def evaluate(script: str) -> str:
    """Run a JS expression or arrow function in the page; result returned as JSON."""
    page = await _ensure()
    res = await page.evaluate(script)
    return _clip(json.dumps(res, ensure_ascii=False, default=str), 20000)


@tool
async def request_human(message: str = "Please complete the step, then click Done.", timeout_s: int = 180) -> str:
    """Pause for a human (captcha, login, 2FA). Needs a visible window: configure(headless=False) first.
    Shows a banner with a Done button and waits for the click."""
    page = await _ensure()
    if S.cfg["headless"]:
        return "ERROR: browser is headless; call configure(headless=False) and navigate again, then retry"
    js = """(msg) => new Promise(res => {
      const b = document.createElement('div');
      b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#fde68a;color:#000;padding:10px;font:14px sans-serif;display:flex;gap:12px;align-items:center';
      b.textContent = msg; const btn = document.createElement('button'); btn.textContent = 'Done';
      btn.onclick = () => { b.remove(); res('done'); }; b.appendChild(btn); document.documentElement.appendChild(b);
    })"""
    try:
        await asyncio.wait_for(page.evaluate(js, message), timeout=timeout_s)
        return f"human finished; url={page.url}"
    except asyncio.TimeoutError:
        return "ERROR: timed out waiting for human"


# ---------------------------------------------------------------- cookies
@tool
async def get_cookies(url_filter: str = "") -> str:
    """Cookies of the browser context as JSON (optionally filtered by domain substring)."""
    await _ensure()
    cs = await S.ctx.cookies()
    if url_filter:
        cs = [c for c in cs if url_filter in c.get("domain", "")]
    return _clip(json.dumps(cs, ensure_ascii=False), 20000)


@tool
async def set_cookies(cookies: list[dict]) -> str:
    """Add cookies (Playwright format: name, value, domain, path ... or name, value, url)."""
    await _ensure()
    await S.ctx.add_cookies(cookies)
    return f"added {len(cookies)}"


@tool
async def clear_cookies() -> str:
    """Delete all cookies in the current context."""
    await _ensure()
    await S.ctx.clear_cookies()
    return "cleared"


# ---------------------------------------------------------------- debug
@tool
async def console_logs(limit: int = 50, clear: bool = False) -> str:
    """Recent console messages and JS errors of the current tab. Requires configure(capture_console=True)
    because CloakBrowser hides CDP console events for stealth."""
    page = await _ensure()
    if not S.cfg["capture_console"]:
        return "ERROR: console capture is off; call configure(capture_console=True) and reload the page"
    out = await page.evaluate("(c) => { const b = window.__cm_console || []; const r = b.slice(); if (c) b.length = 0; return r; }", clear)
    return json.dumps(out[-limit:], ensure_ascii=False)


@tool
async def list_network_requests(
    url_pattern: str = "", resource_type: str = "", limit: int = 50
) -> str:
    """Recent network responses of the current tab. Filter by URL regex and resource_type
    (xhr|fetch|document|script|image|...). Use the `i` with get_response_body to read an API reply."""
    await _ensure()
    rows = list(S.net.get(S.cur, []))
    if url_pattern:
        rx = re.compile(url_pattern, re.I)
        rows = [r for r in rows if rx.search(r["url"])]
    if resource_type:
        rows = [r for r in rows if r["type"] == resource_type]
    rows = [{**r, "url": r["url"][:200]} for r in rows[-limit:]]
    return json.dumps(rows, ensure_ascii=False)


@tool
async def get_response_body(index: int, max_chars: int = 20000) -> str:
    """Body of a recorded network response (index `i` from list_network_requests)."""
    await _ensure()
    r = S.responses.get((S.cur, index))
    if r is None:
        return "ERROR: no such response (too old or wrong tab)"
    return _clip(await r.text(), max_chars)


@tool
async def check_stealth() -> str:
    """Report automation-detection signals of the live session (webdriver flag, UA, WebGL, plugins...)."""
    page = await _ensure()
    return json.dumps(await page.evaluate(JS_STEALTH), ensure_ascii=False)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
