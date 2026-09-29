"""The loopback HTTP server every real-process test serves with.

    python loopserve.py PORT

Why not ``python -m http.server``: ``HTTPServer.server_bind`` binds the socket,
then calls ``socket.getfqdn(host)`` — a reverse-DNS lookup of 127.0.0.1 — and
only *then* does ``TCPServer.__init__`` call ``listen()``. On GitHub's hosted
macOS runners that lookup takes ~35 s, and for all of it the port is bound but
not listening: connects are refused and no LISTEN socket exists, which is
exactly "nothing of ours is listening" to a readiness probe. 1.1.0's new
supervisor tests wait 15–30 s, so every one of them failed there while passing
on any host with a fast resolver. The server name is only used to fill a CGI
variable nothing here reads, so the lookup is skipped, not sped up.

Scripts spawned by the harness import :class:`LoopbackHTTPServer` from here
rather than restating it, so there is one definition of a test server.
"""

import http.server
import socketserver
import sys


class LoopbackHTTPServer(http.server.ThreadingHTTPServer):
    """``ThreadingHTTPServer`` minus the reverse-DNS lookup between bind and listen."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


if __name__ == "__main__":
    LoopbackHTTPServer(
        ("127.0.0.1", int(sys.argv[1])), http.server.SimpleHTTPRequestHandler
    ).serve_forever()
