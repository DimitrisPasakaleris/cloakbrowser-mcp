import http.server
import os
import sys
import threading
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["CLOAK_MCP_HOME"] = str(ROOT / ".testhome")
os.environ["CLOAK_HUMANIZE"] = "0"  # keep tests fast
os.environ["CLOAK_HEADLESS"] = "1"
os.environ["CLOAK_ALLOW_PRIVATE"] = "1"  # the test site is on loopback
os.environ["CLOAK_MEMORY_NEVER_ASK"] = "1"  # keep the "memory is off" hint out of unrelated tests
os.environ["CLOAK_MEMORY_TOOLS"] = "all"  # the tests exercise every memory tool, not just the opt-in gate
os.environ["CLOAK_USAGE_LOG"] = "0"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "tests" / "site"), **kw)

    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        if self.path.startswith("/challenge"):
            body = b"<html><head><title>Just a moment...</title></head><body>checking your browser</body></html>"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/page2.html")
            self.end_headers()
        else:
            super().do_GET()


@pytest.fixture(scope="session")
def base_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def srv():
    from cloakbrowser_mcp import server

    yield server
    await server.close_browser()
