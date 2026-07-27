"""A tiny local site used by the integration tests.

``/`` and ``/page/N`` serve pages whose links and body text only exist after
JavaScript runs, so a crawler that does not execute JS finds nothing to follow.
That is the property the backends are being tested for.

``/static/N`` serves the same link graph in plain HTML. Nothing about rendering
can be concluded from it -- it exists so that a crawler running *without* a
browser still has a site it can traverse, which is what the no-render benchmark
and its tests need.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PAGE_COUNT = 12

_TEMPLATE = """<!doctype html>
<html>
<head><title>Static Title {n}</title></head>
<body>
  <h1>Loading...</h1>
  <div id="content"></div>
  <script>
    // Everything a crawler should find is injected at DOMContentLoaded, so a
    // plain HTTP fetch sees only the placeholder above.
    document.addEventListener('DOMContentLoaded', function () {{
      document.title = 'JS Title {n}';
      document.querySelector('h1').textContent = 'Rendered Page {n}';
      var html = '<p>marker-js-rendered-{n} unique body text for page {n}</p>';
      {links}
      document.getElementById('content').innerHTML = html;
    }});
  </script>
</body>
</html>
"""


_STATIC_TEMPLATE = """<!doctype html>
<html>
<head><title>Static Page {n}</title></head>
<body>
  <h1>Server Rendered {n}</h1>
  <p>marker-server-rendered-{n} unique body text for page {n}</p>
  {links}
</body>
</html>
"""


def _targets(n: int) -> list[int]:
    return [t for t in (n * 2, n * 2 + 1) if t < PAGE_COUNT]


def _index_of(path: str) -> int | None:
    try:
        return int(path.rsplit("/", 1)[1])
    except ValueError:
        return None


def _render_static(n: int) -> bytes:
    links = "".join(f'<a href="/static/{t}">link to {t}</a>' for t in _targets(n))
    return _STATIC_TEMPLATE.format(n=n, links=links).encode()


def _render(n: int) -> bytes:
    links = "".join(f"html += '<a href=\"/page/{t}\">link to {t}</a>';" for t in _targets(n))
    # A link that must be filtered out by scope/binary rules.
    links += "html += '<a href=\"/asset.pdf\">a pdf</a>';"
    links += "html += '<a href=\"https://external.invalid/x\">offsite</a>';"
    return _TEMPLATE.format(n=n, links=links).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/robots.txt":
            self._send(b"User-agent: *\nDisallow: /private\n", "text/plain")
            return
        if path.startswith("/private"):
            self._send(b"<html><body>secret</body></html>", "text/html")
            return
        if path == "/slow":
            self._send(b"<html><body>slow</body></html>", "text/html")
            return
        if path == "/status/500":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path in ("/", "/index.html"):
            self._send(_render(0), "text/html")
            return
        if path.startswith("/static/"):
            n = _index_of(path)
            if n is None or n >= PAGE_COUNT:
                self._send(b"not found", "text/plain", status=404)
                return
            self._send(_render_static(n), "text/html")
            return
        if path.startswith("/page/"):
            try:
                n = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send(b"bad", "text/plain", status=404)
                return
            if n >= PAGE_COUNT:
                self._send(b"not found", "text/plain", status=404)
                return
            self._send(_render(n), "text/html")
            return

        self._send(b"not found", "text/plain", status=404)

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return  # keep the test output clean


class LocalSite:
    """Context manager running the test site on an ephemeral port."""

    def __init__(self, port: int = 0) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> LocalSite:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serve the JS-rendered test site.")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port.")
    parser.add_argument(
        "--pages", type=int, default=PAGE_COUNT, help="Size of the link graph, for benchmarking."
    )
    args = parser.parse_args()
    PAGE_COUNT = args.pages

    with LocalSite(port=args.port) as site:
        print(f"serving {site.base_url} with {PAGE_COUNT} pages -- Ctrl-C to stop", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
