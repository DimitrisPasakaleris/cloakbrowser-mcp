# cloakbrowser-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) a stealth browser. It wraps
[CloakBrowser](https://github.com/CloakHQ/CloakBrowser), a patched Chromium that passes common bot checks, and exposes
61 tools for navigation, reading, extraction, interaction, sessions, identity control, debugging and
per-site memory that learns (on by default, local, one switch to turn off).

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

1. `read_url(url)` opens a page and returns it as clean markdown in one call (or `navigate(url)` to just open it).
   If the page looks like a bot challenge the result says `BLOCKED?` and what to try.
2. `snapshot()` lists interactive elements with refs (`e12`, or `f2e5` inside an iframe). Refs stay stable while the
   page stays loaded; on a big page you already looked at, a repeat `snapshot()` returns only what changed.
3. `click("e12")`, `type_text("e3", "query", submit=True)`, or several actions in one call with `run_steps`.
4. Look for one thing with `find_in_page("price")` instead of re-reading the page. Read with `get_markdown` /
   `get_text` (paged with `offset`), or take structured data: `extract_metadata` (JSON-LD / OpenGraph / schema.org
   product prices), `extract_tables`, `extract_structured`, `extract_links`.
5. Prefer the JSON API behind a page: `list_network_requests(resource_type="xhr")`, then `get_response_body(i)`.

## Keeping token use low

Every tool *definition* is sent to the model on every turn, and every tool *result* adds to the conversation, so
both are worth trimming. Measured on this repo (estimates, characters / 4):

- **Tool definitions:** `CLOAK_TOOLS=lean` exposes 18 tools (about 2,000 tokens) instead of 58 (about 6,000).
  `standard` drops the debug group; `full` (default) has everything. Add single tools to `lean` with
  `CLOAK_EXTRA_TOOLS=configure,evaluate`. Memory tools stay out of the list until memory is switched on.
- **Fewer round trips:** `read_url` = navigate + read; `run_steps` runs up to 25 click/type/fill/select/scroll/wait/goto
  steps in one call and stops at the first failure. Each avoided call saves a full re-send of the conversation.
- **Smaller results:** `get_markdown` returns the main content only (no menus, footers or cookie banners; `main_only=False`
  for the whole page), `links="short"|"none"` shrinks link URLs, reads default to 6,000 characters and continue with
  `offset=`, and `find_in_page` returns just the matching snippets.
- **Cheaper images:** `screenshot` is a quality-55 JPEG (`format="png"` for lossless; `CLOAK_SHOT_FORMAT`,
  `CLOAK_SHOT_QUALITY`). `screenshot_file` saves to disk and returns the path.
- **See where it goes:** `cloakbrowser-mcp stats` summarises the usage log (tool name and result size only, never URLs or
  page text) and the size of each profile's tool definitions. `CLOAK_USAGE_LOG=0` turns the log off,
  `cloakbrowser-mcp stats --reset` deletes it.

For Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.cloakbrowser]
command = "cloakbrowser-mcp"
[mcp_servers.cloakbrowser.env]
PYTHONUTF8 = "1"
CLOAK_TOOLS = "lean"
```

## Tools

Toolsets can be selected with `CLOAK_TOOLSETS` (default: all five), or pick a whole profile with `CLOAK_TOOLS=lean|standard|full`.

| Toolset | Tools |
|---|---|
| `core` | `configure`, `close_browser`, `tabs_list`, `tab_new`, `tab_select`, `tab_close`, `navigate`, `read_url`, `run_steps`, `go_back`, `reload`, `wait_for`, `snapshot`, `get_text`, `get_markdown`, `find_in_page`, `screenshot` (optionally annotated with refs), `click`, `hover`, `type_text`, `fill_form`, `press_key`, `select_option`, `scroll` |
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

## Memory (on by default, local, and it learns)

The server remembers how each site behaves and gets better at the ones you revisit. Since 0.5 this is **on by default
with the standard preset**, because it only helps if it is running; it is local, it never stores page text, cookies or
form values, and it is one setting to turn off:

```
CLOAK_MEMORY=off                       # in the MCP registration (env var), or
cloakbrowser-mcp memory disable        # from a terminal: also deletes what was learned
CLOAK_MEMORY_DEFAULT=off               # keep the old behaviour: nothing is on until you opt in
```

The first `navigate` of a fresh install says so once ("MEMORY IS ON by default ..."), then never again. Anything you
decide (`memory enable`, `disable`, `CLOAK_MEMORY=...`, the permission prompt) replaces the default and is never
overridden. Notes (`memory_remember`) and recipes stay **opt-in** because they hold free text.

### What it learns, and how it uses it

The loop is **observe, distill, apply, verify, prune**, all deterministic and local (no model call):

| Learns (category) | Applied by | How it stays honest |
|---|---|---|
| Which load strategy works per site (`site_stats`): `domcontentloaded+500ms`, `load+1500ms`, `networkidle+2500ms`, `networkidle+4000ms` | `navigate` / `read_url` pick the best-scoring strategy when you do not set `wait_until` / `settle_ms`. A note says so when it differs from the default | each strategy scores `(ok+1)/(runs+2)` minus a small cost per step; two failures in a row bench it for 7 days; one failure is forgiven; ~8% of the time the cheaper strategy is probed again |
| Pop-up and cookie-banner dismissers (`overlays`): a plain-CSS selector learned when you or the assistant clicked a button like "Accept" or "Close" that sat in a banner and really made it disappear | after each load, remembered dismissers that are visible are clicked | only selectors that passed a strict grammar; a dismisser that stops working is benched; ones that worked on 3+ sites become shared candidates for new sites (needs effort medium) |
| Extractor recipes (`extractors`): the `item_selector` + `fields` of an `extract_structured` call that returned data | `read_url` runs a reliable recipe and returns the items first, with the page text cut to 1,500 characters (`use_memory=false` for the full page) | a recipe needs 2 successes at a smoothed 60%+ rate; empty results are marked down and two in a row bench it |
| URL patterns and page types (`site_structure`), recorded visits | the `MEMORY[...]` note on `navigate`, `memory_recall` | numbers and our own labels only |

Evidence **decays** (half-life 30 days) so a site that changed is re-learned, and `memory reflect` (also run
automatically every 25 recorded navigations unless effort is `off`) forgets faded evidence, un-benches expired
strategies and shares dismissers across sites. `cloakbrowser-mcp stats` and `memory_status` show what was learned and
how often applying it worked, with an estimated token saving.

**Safety by construction.** Learned selectors and recipes are only ever handed to the browser; they are never shown to
the model (not in hints, `memory_status`, `memory_recall` or the audit log), so a page cannot use memory to talk to the
assistant. Memory only changes *how* the browser loads or reads (timing, which known selector to click or read); it
cannot touch domain policy, the private-IP block, `evaluate`, or its own consent. Set `CLOAK_MEMORY_APPLY=0` to make it
observe only (it keeps learning but changes nothing).

### What you control

**What you choose** (each category separately):

| Category | Stores | Never stores |
|---|---|---|
| `site_stats` | per-site success/block counts, status codes, load times, which load strategy and browser settings worked, crawl-delay | page content, cookies, form values |
| `site_structure` | URL patterns like `/c/{id}/{slug}` and page types | query values, real IDs, page text |
| `extractors` | the CSS selectors of extraction recipes that worked | the extracted data itself |
| `overlays` | CSS selectors of pop-up dismiss buttons that worked | anything else on the page |
| `recipes` | step templates (not implemented yet) | typed values |
| `notes` | short lessons the assistant writes with `memory_remember` (scrubbed, max 400 chars) | page text, URLs, secrets |

Presets: **minimal** (`site_stats`), **standard** (stats, structure, extractors, overlays; the default), **full**, or
custom. You also choose which sites (all, only a list, or all except a list), how long to keep data (30, 90, 365 days
or forever), and the effort (off, low, medium, high): `off` stops automatic reflection, `medium`/`high` add sharing
dismissers across sites and explore more. Banking, email, health, government, login and adult sites are never
remembered, and login, checkout, cart and account pages are skipped everywhere.

**Asking for more.** The assistant can call `memory_request_access`. Your client then shows its own form (MCP
elicitation), which the model cannot fill in for you. If your client cannot show forms (Codex, for example), run
`cloakbrowser-mcp memory enable --preset full` in a terminal instead. Declining, timing out or any error changes nothing.

**Tools.** While memory runs on its defaults only `memory_request_access`, `memory_status` and `memory_recall` are listed
(to keep tool definitions small); the rest (`memory_remember`, `memory_forget`, `memory_audit`, `memory_export`,
`memory_disable`, `memory_incognito`) appear once you choose memory explicitly. Saved lessons that look like
instructions or contain URLs are set aside for you to review, and recalled text is always wrapped as untrusted data.

**Your controls.**
- The assistant can only ask, narrow or read. It can never turn memory on or widen it.
- `cloakbrowser-mcp memory setup | enable | status | reflect | disable [category] | forget <site> | export | audit | wipe | reset`
- `memory_incognito`: stop reading and writing for the session (only you can end it).
- Everything lives in `~/.cloakbrowser-mcp/memory/` (`consent.json`, `memory.db`, `notice.json`) and is readable with `export`.
  Databases from 0.3/0.4 are upgraded in place; nothing is lost.
- Environment (set in the registration, overrides the file): `CLOAK_MEMORY=off|minimal|standard|full|custom`
  (`off` is a kill switch), `CLOAK_MEMORY_CATEGORIES`, `CLOAK_MEMORY_DOMAINS` / `CLOAK_MEMORY_BLOCKED`,
  `CLOAK_MEMORY_RETENTION`, `CLOAK_MEMORY_EFFORT`, `CLOAK_MEMORY_INCOGNITO=1`, `CLOAK_MEMORY_NEVER_ASK=1`,
  `CLOAK_MEMORY_DEFAULT=off` (no default-on), `CLOAK_MEMORY_APPLY=0` (observe only), `CLOAK_MEMORY_ALLOW_IP=1` (also
  remember localhost / IP sites, for testing).

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
| `CLOAK_TOOLS` | `full` | tool profile: `lean` (18 tools), `standard`, `full`; `CLOAK_EXTRA_TOOLS=a,b` adds tools to `lean` |
| `CLOAK_TOOLSETS` | all | comma list of `core,extract,debug,session,memory` (overrides the profile's groups) |
| `CLOAK_SHOT_FORMAT`, `CLOAK_SHOT_QUALITY` | `jpeg`, `55` | default screenshot format and JPEG quality |
| `CLOAK_MEMORY_DEFAULT`, `CLOAK_MEMORY_APPLY`, `CLOAK_MEMORY_ALLOW_IP` | on, `1`, off | default-on memory, apply what it learned, remember local sites (see Memory) |
| `CLOAK_USAGE_LOG` | `1` | write tool name + result size per call for `cloakbrowser-mcp stats` |
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
pytest -q                 # 220+ tests against a local static site and the MCP protocol
python e2e.py             # stdio MCP client against live Skroutz (needs network)
python e2e_memory.py      # consent prompt + memory over real MCP stdio, in a temp home
python e2e_learning.py    # default-on memory: notice, learned pop-ups / load strategy / extractors, persistence, disable
python e2e_savings.py     # lean profile, read_url/run_steps, scaled screenshots, usage log, opt-in memory tools
python bench_tokens.py URL...   # before/after token estimates on real pages
```
