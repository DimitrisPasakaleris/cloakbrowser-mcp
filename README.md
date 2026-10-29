# cloakbrowser-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) a stealth browser. It wraps
[CloakBrowser](https://github.com/CloakHQ/CloakBrowser), a patched Chromium that passes common bot checks, and exposes
58 tools for navigation, reading, extraction, interaction, sessions, identity control, debugging and
(optional, opt-in) per-site memory.

## Install

Requires Python 3.10+. The ~200 MB browser binary downloads on first launch and is cached in `~/.cloakbrowser`.

```bash
pip install git+https://github.com/DimitrisPasakaleris/cloakbrowser-mcp
python -m cloakbrowser install        # optional: pre-download the browser binary
```

### Claude Code

```bash
claude mcp add cloakbrowser -s user -- cloakbrowser-mcp
```

### Claude Desktop / other clients

```json
{
  "mcpServers": {
    "cloakbrowser": { "command": "cloakbrowser-mcp", "env": { "PYTHONUTF8": "1" } }
  }
}
```

With `uv`: `claude mcp add cloakbrowser -s user -- uvx --from git+https://github.com/DimitrisPasakaleris/cloakbrowser-mcp cloakbrowser-mcp`.

## Typical flow

1. `navigate(url)`. If the page looks like a bot challenge the result says `BLOCKED?` and what to try.
2. `snapshot()` lists interactive elements with refs (`e12`, or `f2e5` inside an iframe). Refs stay stable while the
   page stays loaded; `snapshot(diff=True)` shows only what changed.
3. `click("e12")`, `type_text("e3", "query", submit=True)`.
4. Read with `get_markdown`, `get_text`, or structured data: `extract_metadata` (JSON-LD / OpenGraph / schema.org
   product prices), `extract_tables`, `extract_structured`, `extract_links`.
5. Prefer the JSON API behind a page: `list_network_requests(resource_type="xhr")`, then `get_response_body(i)`.

## Tools

Toolsets can be selected with `CLOAK_TOOLSETS` (default: all five).

| Toolset | Tools |
|---|---|
| `core` | `configure`, `close_browser`, `tabs_list`, `tab_new`, `tab_select`, `tab_close`, `navigate`, `go_back`, `reload`, `wait_for`, `snapshot`, `get_text`, `get_markdown`, `screenshot` (optionally annotated with refs), `click`, `hover`, `type_text`, `fill_form`, `press_key`, `select_option`, `scroll` |
| `extract` | `get_html`, `extract_links`, `extract_structured`, `extract_metadata`, `extract_tables`, `scroll_to_load`, `save_html`, `save_pdf`, `screenshot_file`, `download_file`, `list_downloads`, `check_robots` |
| `debug` | `console_logs`, `list_network_requests`, `get_response_body`, `check_stealth`, `fingerprint_info`, `evaluate` |
| `memory` | `memory_request_access`, `memory_status`, `memory_recall`, `memory_remember`, `memory_disable`, `memory_forget`, `memory_audit`, `memory_export`, `memory_incognito` |
| `session` | `rotate_proxy`, `new_identity`, `request_human`, `handle_dialog`, `upload_file`, `get_cookies`, `set_cookies`, `clear_cookies`, `save_session`, `load_session` |

Read-only tools carry the MCP `readOnlyHint` annotation so clients can auto-approve them.

`extract_structured` example, for product cards:

```
item_selector = "li.card"
fields = {"name": "h2", "price": ".price", "url": "a@href"}
```

## Identity and sessions

- `configure(profile="name")` keeps cookies and logins in `~/.cloakbrowser-mcp/profiles/name`.
- `save_session("name")` / `load_session("name")` export and restore cookies + localStorage to a file, so a login can
  move to a throwaway browser. Treat these files like passwords.
- `configure(fingerprint_seed=..., platform=..., gpu_vendor=..., hardware_concurrency=..., screen_width=..., ...)`
  pins or overrides the browser fingerprint. `new_identity()` picks a fresh one. `fingerprint_info()` shows what the
  live browser reports.
- `configure(human_preset="careful", human_config={...})` tunes the humanized mouse and typing.
- `navigate(url, retries=N)` retries a blocked page with backoff and a fresh identity (next proxy if you configured
  a list, otherwise a new fingerprint).
- `configure(headless=False)` + `request_human()` lets you solve a captcha in a real window. This server does not
  use captcha-solving services.

## Memory (off by default, opt-in)

The server can learn from browsing and get better at sites you revisit, **but only if you say yes, and only for the
things you pick.** Until then no file is created and nothing is recorded.

**How you are asked.** The assistant can call `memory_request_access`. Your client then shows its own form (MCP
elicitation), which the model cannot fill in for you. If your client cannot show forms, the tool tells you to run
`cloakbrowser-mcp memory setup` in a terminal. Declining, timing out or any error leaves memory **off**. You can
also pre-authorise it yourself in the MCP registration with `CLOAK_MEMORY=standard`.

**What you choose** (each category separately):

| Category | Stores | Never stores |
|---|---|---|
| `site_stats` | per-site success/block counts, status codes, load times, which browser settings worked, crawl-delay | page content, cookies, form values |
| `site_structure` | URL patterns like `/c/{id}/{slug}` and page types | query values, real IDs, page text |
| `extractors`, `overlays`, `recipes` | selectors, banner selectors, step templates (arrive in later releases) | extracted data, typed values |
| `notes` | short lessons the assistant writes with `memory_remember` (scrubbed, max 400 chars) | page text, URLs, secrets |

Presets: **minimal** (`site_stats`), **standard** (stats, structure, extractors, overlays), **full**, or custom.
You also choose which sites (all, only a list, or all except a list), how long to keep data (30, 90, 365 days or
forever), and a reflection effort (off, low, medium, high) for later phases. Banking, email, health, government,
login and adult sites are never remembered, and login, checkout, cart and account pages are skipped everywhere.

**What it does today.** `navigate` adds a short note such as
`MEMORY[site_stats]: 3 visits, 2 blocked; WARNING: this setup (headless=1,...) was blocked 2x here; worked before:
headless=0,...`, so a blocked site is handled differently next time. `memory_recall` shows what is known about a
site, and `memory_remember` saves a lesson. Saved lessons that look like instructions or contain URLs are set aside
for you to review, never recalled, and recalled text is always wrapped as untrusted data.

**Your controls.**
- The assistant can only ask, narrow or read. It can never turn memory on or widen it.
- `cloakbrowser-mcp memory setup | status | disable [category] | forget <site> | export | audit | wipe | reset`
- `memory_incognito`: stop reading and writing for the session (only you can end it).
- New categories added by a future version are off until you are asked again.
- Everything lives in `~/.cloakbrowser-mcp/memory/` (`consent.json`, `memory.db`) and is readable with `export`.
- Environment (set in the registration, overrides the file): `CLOAK_MEMORY=off|minimal|standard|full|custom`
  (`off` is a kill switch), `CLOAK_MEMORY_CATEGORIES`, `CLOAK_MEMORY_DOMAINS` / `CLOAK_MEMORY_BLOCKED`,
  `CLOAK_MEMORY_RETENTION`, `CLOAK_MEMORY_EFFORT`, `CLOAK_MEMORY_INCOGNITO=1`, `CLOAK_MEMORY_NEVER_ASK=1`.

## Safety defaults

Browsing is risky for an agent because pages can contain text that tries to instruct it. Mitigations:

- Page-derived output is wrapped in `<untrusted_page_content>` markers, and a page cannot close the marker early.
  Treat it as data, never as instructions. Disable with `CLOAK_WRAP_UNTRUSTED=0`.
- Only `http`/`https` URLs. `file://`, `chrome://`, `javascript:` and similar are refused.
- Localhost and private/link-local addresses (including `169.254.169.254`) are refused unless `CLOAK_ALLOW_PRIVATE=1`.
  Redirects are checked after they happen. Hostnames that only *resolve* to private addresses are not detected.
- `CLOAK_ALLOWED_DOMAINS` / `CLOAK_BLOCKED_DOMAINS` restrict where the browser may go (subdomains included).
- `CLOAK_ALLOW_EVAL=0` disables the `evaluate` tool; `CLOAK_READONLY=1` removes every tool that acts on pages.
- `check_robots(url)` reports whether robots.txt allows a URL, plus any crawl-delay.
- Logs (`~/.cloakbrowser-mcp/logs/server.log`, rotated) redact proxy credentials, tokens and passwords.

These reduce risk; they do not make browsing untrusted pages safe. Use only on sites and data you are allowed to
access, and respect each site's terms.

## Configuration

Defaults come from environment variables and can be changed at runtime with `configure(...)`
(which restarts the browser, so page state is lost).

| Variable | Default | Meaning |
|---|---|---|
| `CLOAK_HEADLESS` | `1` | `0` shows a real window |
| `CLOAK_HUMANIZE` | `1` | human-like mouse and typing (slower, stealthier) |
| `CLOAK_PROXY` / `CLOAK_PROXIES` | none | one proxy URL / comma-separated rotation list |
| `CLOAK_PROFILE` | none | named persistent profile |
| `CLOAK_FINGERPRINT` | random | fixed fingerprint seed |
| `CLOAK_GEOIP` | `0` | derive locale/timezone from the proxy IP (needs `cloakbrowser[geoip]`) |
| `CLOAK_LOCALE`, `CLOAK_TIMEZONE` | none | override, e.g. `el-GR`, `Europe/Athens` |
| `CLOAK_CAPTURE_CONSOLE` | `0` | enable `console_logs` |
| `CLOAK_TOOLSETS` | all | comma list of `core,extract,debug,session,memory` |
| `CLOAK_READONLY`, `CLOAK_ALLOW_EVAL`, `CLOAK_ALLOW_PRIVATE`, `CLOAK_ALLOWED_DOMAINS`, `CLOAK_BLOCKED_DOMAINS`, `CLOAK_WRAP_UNTRUSTED` | | see Safety defaults |
| `CLOAK_MCP_HOME` | `~/.cloakbrowser-mcp` | profiles, sessions, output files, logs |

## Known limits

- `console_logs` needs `capture_console`: CloakBrowser hides CDP console events for stealth, so capture is an opt-in
  page hook that slightly lowers stealth.
- CloakBrowser cancels the browser's own download handling, so clicking a download link saves nothing. Use
  `download_file`, which fetches inside the page (same cookies and proxy; for cross-origin files visit that site first).
- `request_human` needs a visible window: `configure(headless=False)`.
- The free CloakBrowser binary allows one concurrent session; this server uses one browser with tabs.
- CloakBrowser checks PyPI for updates when it launches.
- The CloakBrowser binary has its own license (see its BINARY-LICENSE.md). This repository's code is MIT.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
pytest -q                 # 170+ tests against a local static site and the MCP protocol
python e2e.py             # stdio MCP client against live Skroutz (needs network)
python e2e_memory.py      # consent prompt + memory over real MCP stdio, in a temp home
```
