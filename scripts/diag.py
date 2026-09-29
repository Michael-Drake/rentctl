"""macOS CI diagnostic, round 2: is the ~35s before http.server listens the
reverse-DNS lookup in HTTPServer.server_bind (socket.getfqdn), or something else?"""
import os
import socket
import subprocess
import sys
import time

PY = sys.executable


def timed(label, fn):
    t0 = time.monotonic()
    try:
        r = fn()
    except Exception as e:  # noqa: BLE001 - diagnostic, report anything
        r = repr(e)
    print(f"== {label}: {time.monotonic() - t0:.2f}s -> {r!r}", flush=True)


def answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


def spawn_until_answer(label, code, port):
    env = dict(os.environ, PORT=str(port))
    t0 = time.monotonic()
    p = subprocess.Popen([PY, "-c", code], env=env)
    while time.monotonic() - t0 < 60 and not answers(port):
        time.sleep(0.1)
    print(f"== {label}: answered={answers(port)} after {time.monotonic() - t0:.2f}s", flush=True)
    p.kill()
    p.wait()


def sh(cmd):
    print(f"$ {cmd}", flush=True)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print((r.stdout + r.stderr)[-2500:], flush=True)


print("python", sys.version, flush=True)
sh("hostname; scutil --get LocalHostName; scutil --get ComputerName")
sh("cat /etc/hosts")
sh("scutil --dns | head -40")

timed("python -c pass (interpreter startup)", lambda: subprocess.run([PY, "-c", "pass"]).returncode)
timed("python -c 'import http.server'", lambda: subprocess.run([PY, "-c", "import http.server"]).returncode)
timed("socket.gethostname()", socket.gethostname)
timed("socket.getfqdn('127.0.0.1') #1", lambda: socket.getfqdn("127.0.0.1"))
timed("socket.getfqdn('127.0.0.1') #2", lambda: socket.getfqdn("127.0.0.1"))
timed("socket.gethostbyaddr('127.0.0.1')", lambda: socket.gethostbyaddr("127.0.0.1"))
timed("socket.getfqdn('')", lambda: socket.getfqdn(""))

STOCK = (
    "import http.server, os\n"
    "http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])),"
    " http.server.SimpleHTTPRequestHandler).serve_forever()\n"
)
NO_FQDN = (
    "import http.server, os, socketserver\n"
    "class S(http.server.ThreadingHTTPServer):\n"
    "    def server_bind(self):\n"
    "        socketserver.TCPServer.server_bind(self)\n"
    "        self.server_name, self.server_port = self.server_address[:2]\n"
    "S(('127.0.0.1', int(os.environ['PORT'])), http.server.SimpleHTTPRequestHandler).serve_forever()\n"
)
spawn_until_answer("stock ThreadingHTTPServer", STOCK, 21011)
spawn_until_answer("server_bind without getfqdn", NO_FQDN, 21012)
