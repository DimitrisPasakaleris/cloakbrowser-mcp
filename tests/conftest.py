import functools
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


@pytest.fixture(scope="session")
def base_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "tests" / "site"))
    handler.func.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def srv():
    from cloakbrowser_mcp import server

    yield server
    await server.close_browser()
