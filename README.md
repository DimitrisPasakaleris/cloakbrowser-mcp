# cloakbrowser-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) a stealth browser. It wraps
[CloakBrowser](https://github.com/CloakHQ/CloakBrowser), a patched Chromium that passes common bot checks, and exposes
39 tools for navigation, reading, extraction, interaction, sessions and debugging.

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

1. `navigate(url)` then `snapshot()` to get refs (`e12`) for interactive elements.
2. `click("e12")`, `type_text("e3", "query", submit=True)`.
3. Read with `get_markdown`, `get_text`, `extract_structured`, `extract_links`.
4. Prefer the JSON API behind a page: `list_network_requests(resource_type="xhr")`, then `get_response_body(i)`.

## Tools

| Group | Tools |
|---|---|
| Config | `configure`, `rotate_proxy`, `close_browser` |
| Tabs | `tabs_list`, `tab_new`, `tab_select`, `tab_close` |
| Navigate | `navigate`, `go_back`, `reload`, `wait_for` |
| Read | `snapshot`, `get_text`, `get_html`, `get_markdown`, `extract_links`, `extract_structured`, `screenshot`, `save_pdf`, `save_html` |
| Act | `click`, `hover`, `type_text`, `fill_form`, `press_key`, `select_option`, `scroll`, `scroll_to_load`, `upload_file`, `handle_dialog`, `evaluate`, `request_human` |
| Cookies | `get_cookies`, `set_cookies`, `clear_cookies` |
| Debug | `console_logs`, `list_network_requests`, `get_response_body`, `check_stealth` |

`extract_structured` example, for product cards:

```
item_selector = "li.card"
fields = {"name": "h2", "price": ".price", "url": "a@href"}
```

## Configuration

Defaults come from environment variables and can be changed at runtime with `configure(...)`
(which restarts the browser, so page state is lost).

| Variable | Default | Meaning |
|---|---|---|
| `CLOAK_HEADLESS` | `1` | `0` shows a real window |
| `CLOAK_HUMANIZE` | `1` | human-like mouse and typing (slower, stealthier) |
| `CLOAK_PROXY` / `CLOAK_PROXIES` | none | one proxy URL / comma-separated rotation list |
| `CLOAK_PROFILE` | none | named persistent profile (cookies, logins) |
| `CLOAK_GEOIP` | `0` | derive locale/timezone from the proxy IP (needs `cloakbrowser[geoip]`) |
| `CLOAK_LOCALE`, `CLOAK_TIMEZONE` | none | override, e.g. `el-GR`, `Europe/Athens` |
| `CLOAK_CAPTURE_CONSOLE` | `0` | enable `console_logs` |
| `CLOAK_MCP_HOME` | `~/.cloakbrowser-mcp` | profiles, output files, `logs/server.log` |

## Notes and limits

- `console_logs` needs `capture_console`: CloakBrowser hides CDP console events for stealth, so capture is an opt-in
  page hook that slightly lowers stealth.
- Refs from `snapshot` cover the main frame only (not iframes) and are re-assigned on every call.
- `request_human` needs a visible window: `configure(headless=False)`.
- The server relaunches the browser if it dies; tool errors come back as `ERROR in <tool>: ...` strings.
- CloakBrowser checks PyPI for updates when it launches.
- Use this only on sites and data you are allowed to access, and respect each site's terms and robots rules.
  A stealth browser does not change what you are permitted to do.
- The CloakBrowser binary has its own license (see its BINARY-LICENSE.md). This repository's code is MIT.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
pytest -q                 # 20 tests against a local static site
python e2e.py             # stdio MCP client against live Skroutz (needs network)
```
