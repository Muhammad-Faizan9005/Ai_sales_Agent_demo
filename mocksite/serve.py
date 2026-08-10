"""Serve the generated mirror on http://localhost:8080.

    python mocksite/serve.py [--port 8080]

Two behaviours the agent work depends on:

  * /path and /path/ both resolve to path/index.html, so links written either
    way work -- WordPress permalinks are directory-style and the scrape mixes
    both forms.

  * Anything without a file 404s for real, with no fallback page. /pricing/ is
    the case that matters: the model invents it, and the mock has to make that
    visible instead of quietly serving something.
"""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "site"


class MirrorHandler(SimpleHTTPRequestHandler):
    def send_head(self):  # noqa: N802 - stdlib naming
        path = self.translate_path(self.path)
        candidate = Path(path)
        if candidate.is_dir():
            index = candidate / "index.html"
            if not index.exists():
                self.send_error(404, "Not Found")
                return None
        return super().send_head()

    def log_message(self, fmt: str, *args) -> None:
        status = args[1] if len(args) > 1 else ""
        marker = "  <-- 404" if str(status).startswith("4") else ""
        print(f"  {args[0]}  {status}{marker}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if not ROOT.exists():
        print("mocksite/site missing -- run: python mocksite/build.py")
        return 1

    handler = partial(MirrorHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Serving {ROOT.name}/ on http://localhost:{args.port}")
    print("  /pricing/ should 404 -- that is the point")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
