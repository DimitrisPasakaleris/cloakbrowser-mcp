"""Serve the local test pages (cookie banner, bot-check page that answers 403, a big product list ...) for the prompt suite.

    python prompt_tests/serve_fixtures.py 8765

Then run the MCP server with CLOAK_ALLOW_PRIVATE=1 (visiting localhost is blocked by default) and CLOAK_MEMORY_ALLOW_IP=1
(so memory may learn from a local site)."""
import http.server
import sys
import threading
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "tests" / "site"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(SITE), **kw)

    def log_message(self, *a, **k):
        pass

    def do_GET(self):
        if self.path.startswith("/challenge"):  # looks like a bot check: 403 + "Just a moment..."
            body = b"<html><head><title>Just a moment...</title></head><body>checking your browser</body></html>"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()


def start(port: int = 0):
    """Start in a background thread; returns (server, base_url)."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving {SITE} on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
