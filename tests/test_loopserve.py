"""The test server must listen without waiting on reverse DNS.

GitHub's hosted macOS runners ship an ``/etc/hosts`` whose ``127.0.0.1`` line
names no host, so ``socket.getfqdn("127.0.0.1")`` goes to the network resolver
and takes ~35 s (measured: 35.01 s; interpreter startup 0.02 s). Stock
``HTTPServer`` makes that call between ``bind()`` and ``listen()``, so every
real-process test waiting 15–30 s for it failed there and nowhere else.

These tests recreate that host on any machine by making ``getfqdn`` slow in the
child, then prove the harness server answers anyway — and, as the negative
control, that a stock ``http.server`` under the same condition does not, so the
condition is real and the first test can fail.
"""

from __future__ import annotations

import subprocess
import sys
import time

from suphelp import LOOPSERVE, answers, free_port, wait_until

# Stand-in for the runner's resolver: getfqdn blocks well past any wait below.
SLOW_DNS = "import socket, time; socket.getfqdn = lambda *a: (time.sleep(30), 'localhost')[1]\n"


def _serve(code: str, port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code, str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(timeout=10)


def test_loopserve_answers_while_reverse_dns_hangs():
    port = free_port()
    code = (SLOW_DNS
            + "import runpy, sys\n"
            + f"sys.argv = [{LOOPSERVE!r}, sys.argv[1]]\n"
            + f"runpy.run_path({LOOPSERVE!r}, run_name='__main__')\n")
    proc = _serve(code, port)
    try:
        t0 = time.monotonic()
        assert wait_until(lambda: answers(port), 10), "loopserve waited on getfqdn"
        assert time.monotonic() - t0 < 10
    finally:
        _stop(proc)


def test_stock_http_server_does_not_answer_while_reverse_dns_hangs():
    """Negative control: without it, the test above proves nothing."""
    port = free_port()
    code = (SLOW_DNS
            + "import http.server, sys\n"
            + "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])),"
            + " http.server.SimpleHTTPRequestHandler).serve_forever()\n")
    proc = _serve(code, port)
    try:
        # Alive and bound, but still inside getfqdn: nothing answers.
        assert not wait_until(lambda: answers(port), 3)
        assert proc.poll() is None
    finally:
        _stop(proc)
