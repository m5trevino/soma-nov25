"""Minimal CORS proxy for the Turn-Key profile builder.

Bridges the browser (served from a local file or dev server) to the
engine at https://engine.save-aichats.com by adding CORS headers
that the browser requires for cross-origin fetch calls.

Run with:  python cors_proxy.py [port]
Default port: 8765
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

ENGINE_URL = "https://engine.save-aichats.com"

# Inference routes require this key. It is injected here rather than in the
# browser so the credential never reaches the client.
ENGINE_KEY = os.environ.get("ENGINE_KEY", "")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Forward requests to the engine and inject CORS headers."""

    # Suppress default stderr logging for a quieter output.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        sys.stderr.write(f"[proxy] {fmt % args}\n")
        sys.stderr.flush()

    # ------------------------------------------------------------------
    def _cors_headers(self) -> None:
        """Add CORS headers to every response."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "3600")

    # ------------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        code = 204  # No Content — standard for preflight
        self.send_response(code)
        self._cors_headers()
        self.end_headers()

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        """Proxy a GET to the engine."""
        self._forward("GET")

    def do_POST(self) -> None:  # noqa: N802
        """Proxy a POST to the engine."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None
        self._forward("POST", body)

    # ------------------------------------------------------------------
    def _forward(self, method: str, body: bytes | None = None) -> None:
        """Send *method* (+ optional body) to the real engine."""
        path = self.path
        if path == "/":
            path = "/v1/models"

        url = f"{ENGINE_URL}{path}"
        headers: dict[str, str] = {}
        for key in ("Content-Type", "Authorization", "X-Peacock-Key"):
            val = self.headers.get(key)
            if val:
                headers[key] = val

        if ENGINE_KEY:
            headers["X-Peacock-Key"] = ENGINE_KEY

        req = urllib.request.Request(url, method=method, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                # Copy over relevant response headers
                for hdr in ("Content-Type", "Content-Length"):
                    val = resp.headers.get(hdr)
                    if val:
                        self.send_header(hdr, val)
                self._cors_headers()
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.URLError as exc:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            error_body = json.dumps({"error": f"Engine unreachable: {exc}"}).encode()
            self.wfile.write(error_body)
        except Exception as exc:  # noqa: BLE001
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self._cors_headers()
            self.end_headers()
            error_body = json.dumps({"error": str(exc)}).encode()
            self.wfile.write(error_body)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = http.server.HTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[proxy] CORS proxy running on http://127.0.0.1:{port}")
    print(f"[proxy] Forwarding to {ENGINE_URL}")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] Stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
