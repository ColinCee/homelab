"""Answers GitHub's push webhook by ringing the deployer (docs/architecture.md, "Deploys").

Any POST /ring writes the ring file, which deploy-main.path watches. It reads no header or body:
a ring only makes the deployer fetch main from GitHub, so a forged one deploys nothing new.
"""

from __future__ import annotations

import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RING = Path(os.environ.get("RING_FILE", "/ring/rung"))
PORT = 8080
# GitHub's push payloads; read only so closing the socket does not reset the reply away.
BODY_MAX = 1 << 20


class Doorbell(BaseHTTPRequestHandler):
    # Strangers reach this through the tunnel: a slow client is cut off, not waited on.
    timeout = 5

    def do_POST(self) -> None:
        length = min(int(self.headers.get("Content-Length") or 0), BODY_MAX)
        self.rfile.read(length)
        if self.path != "/ring":
            self._answer(404)
            return
        try:
            # Its content changes, so PathModified fires even on a bind mount.
            RING.write_text(f"{time.time()}\n", encoding="utf-8")
        except OSError as e:
            print(f"ERROR: cannot ring: {e}", flush=True)
            self._answer(500)
            return
        self._answer(202)

    def do_GET(self) -> None:
        self._answer(404)

    def _answer(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


def main() -> None:
    ThreadingHTTPServer(("", PORT), Doorbell).serve_forever()


if __name__ == "__main__":
    main()
