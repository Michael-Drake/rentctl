"""macOS CI diagnostic: why does a test http.server never answer on the runner?"""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

PY = sys.executable


def answers(port, t=0.3):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=t).close()
        return True
    except OSError as e:
        return repr(e)


def wait(port, secs):
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        r = answers(port)
        if r is True:
            return f"answered after {time.monotonic() - t0:.2f}s"
        time.sleep(0.25)
    return f"NOT answering after {secs}s, last: {answers(port)}"


def sh(cmd):
    print(f"$ {cmd}", flush=True)
    print(subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout[-3000:], flush=True)


def run(label, port, **kw):
    tmp = Path(tempfile.mkdtemp())
    log = open(tmp / "w.log", "w")
    env = dict(os.environ, PORT=str(port))
    cmd = f"'{PY}' -m http.server \"$PORT\" --bind 127.0.0.1"
    t0 = time.monotonic()
    p = subprocess.Popen(["/bin/sh", "-c", cmd], stdout=log, stderr=subprocess.STDOUT, env=env, **kw)
    res = wait(port, 40)
    print(f"== {label}: pid {p.pid} spawn->{res}; poll={p.poll()}", flush=True)
    sh(f"ps -axo pid,ppid,pgid,sess,stat,etime,command | grep -E 'http.server|PID' | grep -v grep")
    sh(f"lsof -nP -iTCP:{port} -sTCP:LISTEN")
    log.flush()
    print("log:", (tmp / "w.log").read_text()[-2000:], flush=True)
    p.kill()
    subprocess.run(["pkill", "-f", f"http.server {port}"])
    p.wait()
    return res


print("python", sys.version, flush=True)
sh("sw_vers; uname -a; ulimit -a | head -5")
run("plain popen", 21001)
run("start_new_session", 21002, start_new_session=True)

from rentctl.core.registry import RegistryProfile  # noqa: E402
from rentctl.core.runners import ProcessRunner  # noqa: E402

tmp = Path(tempfile.mkdtemp())
r = ProcessRunner()
prof = RegistryProfile(cmd=f"'{PY}' -m http.server \"$PORT\" --bind 127.0.0.1", cwd=str(tmp), port_env="PORT", preferred_offset=0)
h = r.start(prof, 21003, tmp / "r.log")
print("== ProcessRunner:", wait(21003, 40), flush=True)
sh("ps -axo pid,ppid,pgid,sess,stat,etime,command | grep -E 'http.server|PID' | grep -v grep")
sh("lsof -nP -iTCP:21003 -sTCP:LISTEN")
print("runner log:", (tmp / "r.log").read_text()[-3000:], flush=True)
print("stop:", r.stop(h), flush=True)
