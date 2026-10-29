# cloakbrowser-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) a stealth browser. It wraps
[CloakBrowser](https://github.com/CloakHQ/CloakBrowser), a patched Chromium that passes common bot checks, and exposes
49 tools for navigation, reading, extraction, interaction, sessions, identity control and debugging.

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

Toolsets can be selected with `CLOAK_TOOLSETS` (default: all four).

| Toolset | Tools |
|---|---|
| `core` | `configure`, `close_browser`, `tabs_list`, `tab_new`, `tab_select`, `tab_close`, `navigate`, `go_back`, `reload`, `wait_for`, `snapshot`, `get_text`, `get_markdown`, `screenshot` (optionally annotated with refs), `click`, `hover`, `type_text`, `fill_form`, `press_key`, `select_option`, `scroll` |
| `extract` | `get_html`, `extract_links`, `extract_structured`, `extract_metadata`, `extract_tables`, `scroll_to_load`, `save_html`, `save_pdf`, `screenshot_file`, `download_file`, `list_downloads`, `check_robots` |
| `debug` | `console_logs`, `list_network_requests`, `get_response_body`, `check_stealth`, `fingerprint_info`, `evaluate` |
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
| `CLOAK_TOOLSETS` | all | comma list of `core,extract,debug,session` |
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
pytest -q                 # 40 tests against a local static site
python e2e.py             # stdio MCP client against live Skroutz (needs network)
```
