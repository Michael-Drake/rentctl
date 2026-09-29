#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake
"""ADR-0016 evidence probe: session/group mechanics for a per-lease supervisor.

Throwaway experiment, NOT product code. Run with any Python >= 3.12 that has
psutil installed:

    python adr/0016-experiments/probe.py            # all experiments
    python adr/0016-experiments/probe.py readability

It binds no ports, touches no rentctl state, and signals only processes it
spawned itself (every signal is preceded by a pid + create_time check). Every
helper process it creates has a hard lifetime cap (LIFETIME_S) so a crashed run
cannot leak processes for long, and the harness kills and verifies all of them
before exiting. Output is plain `key: value` lines for pasting into the ADR.

The same script is the Linux CI leg's checklist: run it on ubuntu and compare.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

LIFETIME_S = 45
PY = sys.executable
ME = os.path.abspath(__file__)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def ctime(pid: int) -> float | None:
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return None
        return p.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def sid_of(pid: int):
    try:
        return os.getsid(pid)
    except OSError as e:
        return f"{type(e).__name__}({e.errno})"


def pgid_of(pid: int):
    try:
        return os.getpgid(pid)
    except OSError as e:
        return f"{type(e).__name__}({e.errno})"


def session_members(sid: int, born_at_or_after: float, exclude: set[int]) -> list[int]:
    """Every live, non-zombie process whose getsid() == sid and that was born no
    earlier than the session owner (1 s tolerance, as rentctl's guard uses)."""
    out = []
    for pid in psutil.pids():
        if pid in exclude or pid == 0:  # getsid(0) is the CALLER, a classic trap
            continue
        try:
            if os.getsid(pid) != sid:
                continue
        except OSError:
            continue
        ct = ctime(pid)
        if ct is None or ct + 1.0 < born_at_or_after:
            continue
        out.append(pid)
    return sorted(out)


def verified_kill(pid: int, expected_ctime: float, sig: int, sid: int | None = None) -> str:
    ct = ctime(pid)
    if ct is None or abs(ct - expected_ctime) > 1.0:
        return "skipped-identity-mismatch"
    if sid is not None and sid_of(pid) != sid:
        return "skipped-left-session"
    try:
        os.kill(pid, sig)
        return "signalled"
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "eperm"


def read_pidfile(path: Path, timeout: float = 5.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            txt = path.read_text().strip()
            if txt:
                return int(txt)
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    return None


def out(key: str, value) -> None:
    print(f"{key}: {value}", flush=True)


# --------------------------------------------------------------------------
# helper roles (run as separate processes)
# --------------------------------------------------------------------------

def role_ign(pidfile: str) -> None:
    """Ignores SIGTERM (SIG_IGN), writes its pid, sleeps."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(pidfile).write_text(str(os.getpid()))
    time.sleep(LIFETIME_S)


def role_pgesc(pidfile: str) -> None:
    """Leaves its process group (setpgid(0,0)) but not its session; ignores TERM."""
    os.setpgid(0, 0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(pidfile).write_text(str(os.getpid()))
    time.sleep(LIFETIME_S)


def role_sidesc(pidfile: str) -> None:
    """Leaves its session entirely (setsid) — the documented escape boundary."""
    os.setsid()
    Path(pidfile).write_text(str(os.getpid()))
    time.sleep(LIFETIME_S)


def role_forker(pidfile: str, spawned_pidfile: str) -> None:
    """On SIGTERM: spawn a fresh TERM-ignoring child, then exit. Models a
    process that starts another process during shutdown."""
    def on_term(signum, frame):
        subprocess.Popen([PY, ME, "ign", spawned_pidfile])
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    Path(pidfile).write_text(str(os.getpid()))
    time.sleep(LIFETIME_S)


def role_waker(resultfile: str) -> None:
    """Wake-latency check: Event.wait interrupted by a SIGWINCH handler."""
    ev = threading.Event()
    got = {}

    def on_winch(signum, frame):
        got["t"] = time.monotonic()
        ev.set()
    signal.signal(signal.SIGWINCH, on_winch)
    Path(resultfile + ".ready").write_text(str(os.getpid()))
    t0 = time.monotonic()
    woke = ev.wait(30)
    t1 = time.monotonic()
    Path(resultfile).write_text(json.dumps({"woke": woke, "waited_s": round(t1 - t0, 4),
                                            "handler_to_return_ms": round((t1 - got.get("t", t1)) * 1000, 3)}))


def role_sup(workdir: str, scenario: str) -> None:
    """Supervisor-like process. Spawned by the harness with start_new_session=True,
    so it is session leader AND group leader (sid == pgid == pid). The workload
    is launched inside that session AND group (arrangement A in ADR-0016)."""
    wd = Path(workdir)
    me = os.getpid()
    my_ct = ctime(me)
    res: dict = {"sup_pid": me, "sup_sid": os.getsid(0), "sup_pgid": os.getpgid(0)}
    term_hits = {"n": 0}

    def on_term(signum, frame):  # a Python handler, NOT SIG_IGN: exec() resets it
        term_hits["n"] += 1
    signal.signal(signal.SIGTERM, on_term)

    if scenario == "normal":
        # an ordinary workload: shell waiting on two children that die on TERM
        shell = subprocess.Popen(f"sleep {LIFETIME_S} & sleep {LIFETIME_S} & wait", shell=True)
        time.sleep(0.5)
        res["members_before"] = session_members(me, my_ct, {me})
        t0 = time.monotonic()
        os.killpg(me, signal.SIGTERM)
        while session_members(me, my_ct, {me}) and time.monotonic() - t0 < 5:
            shell.poll()  # reap our direct child so it does not linger as a zombie
            time.sleep(0.01)
        shell.wait()
        res["ms_to_empty_after_TERM"] = round((time.monotonic() - t0) * 1000, 1)
        res["members_after"] = session_members(me, my_ct, {me})
        res["sup_term_handler_hits"] = term_hits["n"]
        (wd / "sup.json").write_text(json.dumps(res))
        return
    if scenario == "full":
        cmd = (
            f"'{PY}' '{ME}' ign '{wd}/ign.pid' & "
            f"'{PY}' '{ME}' pgesc '{wd}/pgesc.pid' & "
            f"'{PY}' '{ME}' sidesc '{wd}/sidesc.pid' & "
            f"'{PY}' '{ME}' forker '{wd}/forker.pid' '{wd}/forked.pid' & "
            "sleep 1; exit 0"
        )
    else:  # supkill: one TERM-ignoring child, shell exits
        cmd = f"'{PY}' '{ME}' ign '{wd}/ign.pid' & sleep 1; exit 0"
    shell = subprocess.Popen(cmd, shell=True)  # same session, same group as sup
    res["shell_pid"] = shell.pid
    res["shell_pgid"] = pgid_of(shell.pid)
    shell.wait()  # reap: the launch parent has exited
    res["shell_exit"] = shell.returncode
    names = ["ign"] if scenario != "full" else ["ign", "pgesc", "sidesc", "forker"]
    kids = {n: read_pidfile(wd / f"{n}.pid") for n in names}
    time.sleep(0.2)
    res["kids"] = {
        n: {"pid": p, "ppid": psutil.Process(p).ppid(), "sid": sid_of(p), "pgid": pgid_of(p)}
        for n, p in kids.items() if p
    }
    try:
        res["children_recursive"] = sorted(c.pid for c in psutil.Process(me).children(recursive=True))
    except psutil.Error as e:
        res["children_recursive"] = repr(e)
    t = time.perf_counter()
    res["session_members"] = session_members(me, my_ct, {me})
    res["enum_ms"] = round((time.perf_counter() - t) * 1000, 2)

    if scenario == "supkill":
        (wd / "sup.json").write_text(json.dumps(res))
        time.sleep(LIFETIME_S)  # harness SIGKILLs us here
        return

    # --- stop: graceful phase ---
    timeline = []
    t0 = time.monotonic()
    os.killpg(me, signal.SIGTERM)  # our own group: cannot be a reused id while we live
    grp = set(session_members(me, my_ct, {me}))
    for pid in grp:
        if pgid_of(pid) != me:  # setpgid escapers: per-pid, verified
            verified_kill(pid, ctime(pid) or 0.0, signal.SIGTERM, sid=me)
    grace = 1.5
    while time.monotonic() - t0 < grace:
        m = session_members(me, my_ct, {me})
        timeline.append((round(time.monotonic() - t0, 3), m))
        time.sleep(0.1)
    res["sup_term_handler_hits"] = term_hits["n"]
    res["sup_alive_after_own_killpg_term"] = True
    forked = read_pidfile(wd / "forked.pid", timeout=0.1)
    res["forked_during_shutdown"] = {"pid": forked, "sid": sid_of(forked) if forked else None,
                                     "ppid": (psutil.Process(forked).ppid() if forked and ctime(forked) else None)}
    res["survivors_after_grace"] = session_members(me, my_ct, {me})

    # --- escalation: per-pid SIGKILL to verified survivors, never killpg(self) ---
    t1 = time.monotonic()
    rounds = 0
    while True:
        surv = session_members(me, my_ct, {me})
        if not surv or time.monotonic() - t1 > 2.0:
            break
        rounds += 1
        for pid in surv:
            verified_kill(pid, ctime(pid) or 0.0, signal.SIGKILL, sid=me)
        time.sleep(0.05)
    res["kill_rounds"] = rounds
    res["survivors_after_kill"] = session_members(me, my_ct, {me})
    res["escalation_ms"] = round((time.monotonic() - t1) * 1000, 1)
    res["timeline_first_last"] = [timeline[0], timeline[-1]] if timeline else []
    res["sup_still_alive_to_verify"] = True
    (wd / "sup.json").write_text(json.dumps(res))


# --------------------------------------------------------------------------
# harness experiments
# --------------------------------------------------------------------------

def exp_readability() -> None:
    out("platform", f"{sys.platform} python {sys.version.split()[0]} psutil {psutil.__version__}")
    out("uid", os.getuid())
    out("psutil_has_sid_api", any("sid" in a for a in dir(psutil.Process)))
    # a same-user process in a DIFFERENT session
    p = subprocess.Popen([PY, "-c", "import time; time.sleep(5)"], start_new_session=True)
    time.sleep(0.2)
    out("getsid(own child, other session)", f"{sid_of(p.pid)} (child pid {p.pid}; my sid {os.getsid(0)})")
    out("getpgid(own child, other session)", pgid_of(p.pid))
    p.kill(); p.wait()
    # pid 1 and a process of another user
    out("getsid(1)", sid_of(1))
    out("getpgid(1)", pgid_of(1))
    foreign = None
    for proc in psutil.process_iter(["uids", "name"]):
        try:
            if proc.info["uids"] and proc.info["uids"].real not in (os.getuid(),) and proc.pid > 1:
                foreign = proc
                break
        except psutil.Error:
            continue
    if foreign:
        out("foreign-user process", f"pid {foreign.pid} uid {foreign.info['uids'].real} name {foreign.info['name']}")
        out("getsid(foreign-user)", sid_of(foreign.pid))
        out("create_time(foreign-user)", ctime(foreign.pid) is not None)
        try:
            foreign.cmdline()
            out("cmdline(foreign-user)", "readable")
        except psutil.AccessDenied:
            out("cmdline(foreign-user)", "AccessDenied")
    q = subprocess.Popen([PY, "-c", "import time; time.sleep(5)", "marker-xyz"])
    time.sleep(0.2)
    out("cmdline(own process)", psutil.Process(q.pid).cmdline()[-1])
    q.kill(); q.wait()
    # how much of the process table can an unprivileged user getsid()?
    ok = err = 0
    errs: dict[str, int] = {}
    for pid in psutil.pids():
        if pid == 0:
            continue  # getsid(0) means "the caller", not pid 0
        try:
            os.getsid(pid)
            ok += 1
        except OSError as e:
            err += 1
            errs[type(e).__name__] = errs.get(type(e).__name__, 0) + 1
    out("getsid over whole table", f"{ok} readable, {err} errors {errs}")
    # enumeration cost
    n = len(psutil.pids())
    t = time.perf_counter()
    for _ in range(10):
        session_members(os.getsid(0), 0.0, set())
    out("enumerate-by-sid cost", f"{round((time.perf_counter() - t) * 100, 2)} ms/scan over {n} pids")


def _cleanup(pids: dict[str, int | None], cts: dict[str, float | None]) -> dict:
    report = {}
    for n, pid in pids.items():
        if not pid:
            continue
        ct = cts.get(n)
        if ct is not None and ctime(pid) is not None:
            report[n] = verified_kill(pid, ct, signal.SIGKILL)
        else:
            report[n] = "already-gone"
    time.sleep(0.2)
    report["all_dead"] = all(not p or ctime(p) is None or abs((ctime(p) or 0) - (cts.get(n) or -9)) > 1
                             for n, p in pids.items())
    return report


def exp_full() -> None:
    wd = Path(tempfile.mkdtemp(prefix="adr0016-full-"))
    sup = subprocess.Popen([PY, ME, "sup", str(wd), "full"], start_new_session=True)
    rc = sup.wait(timeout=30)
    res = json.loads((wd / "sup.json").read_text())
    for k, v in res.items():
        out(f"full.{k}", v)
    out("full.sup_exit", rc)
    # the setsid escaper is expected to survive; harness kills it (it is ours)
    names = ["ign", "pgesc", "sidesc", "forker", "forked"]
    pids = {n: read_pidfile(wd / f"{n}.pid", timeout=0.1) for n in names}
    alive = {n: (p if p and ctime(p) else None) for n, p in pids.items()}
    out("full.alive_after_sup_exit", alive)
    if alive.get("sidesc"):
        out("full.sidesc_now", {"ppid": psutil.Process(alive["sidesc"]).ppid(), "sid": sid_of(alive["sidesc"])})
    cts = {n: ctime(p) for n, p in pids.items() if p}
    out("full.harness_cleanup", _cleanup(pids, cts))


def exp_normal() -> None:
    wd = Path(tempfile.mkdtemp(prefix="adr0016-normal-"))
    subprocess.Popen([PY, ME, "sup", str(wd), "normal"], start_new_session=True).wait(timeout=30)
    for k, v in json.loads((wd / "sup.json").read_text()).items():
        out(f"normal.{k}", v)


def exp_supkill() -> None:
    wd = Path(tempfile.mkdtemp(prefix="adr0016-supkill-"))
    sup = subprocess.Popen([PY, ME, "sup", str(wd), "supkill"], start_new_session=True)
    deadline = time.monotonic() + 10
    while not (wd / "sup.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    res = json.loads((wd / "sup.json").read_text())
    sup_pid = res["sup_pid"]
    sup_ct = ctime(sup_pid)
    ign = res["kids"]["ign"]["pid"]
    out("supkill.before", {"sup": sup_pid, "ign": res["kids"]["ign"], "members": res["session_members"],
                           "children_recursive": res["children_recursive"]})
    os.kill(sup_pid, signal.SIGKILL)
    sup.wait()
    time.sleep(0.2)
    out("supkill.sup_alive", ctime(sup_pid) is not None)
    out("supkill.ign_after", {"alive": ctime(ign) is not None, "sid": sid_of(ign), "pgid": pgid_of(ign),
                              "ppid": psutil.Process(ign).ppid()})
    # recovery from a DIFFERENT session: enumerate by the dead supervisor's sid
    members = session_members(sup_pid, sup_ct or 0.0, set())
    out("supkill.found_by_sid_from_outside", members)
    try:
        os.killpg(sup_pid, 0)
        out("supkill.killpg(dead_sup_pgid,0)", "group still exists (members remain)")
    except ProcessLookupError:
        out("supkill.killpg(dead_sup_pgid,0)", "ESRCH")
    ign_ct = ctime(ign)
    t0 = time.monotonic()
    out("supkill.recovery_TERM", verified_kill(ign, ign_ct or 0.0, signal.SIGTERM, sid=sup_pid))
    time.sleep(0.5)
    out("supkill.ign_alive_after_TERM", ctime(ign) is not None)
    out("supkill.recovery_KILL", verified_kill(ign, ign_ct or 0.0, signal.SIGKILL, sid=sup_pid))
    time.sleep(0.2)
    out("supkill.ign_alive_after_KILL", ctime(ign) is not None)
    out("supkill.recovery_ms", round((time.monotonic() - t0) * 1000))
    out("supkill.members_after", session_members(sup_pid, sup_ct or 0.0, set()))


def exp_legacy() -> None:
    """1.0.x arrangement: shell spawned with start_new_session=True is itself the
    session leader. Reproduce S1 and show the sid-enumeration fix applies."""
    wd = Path(tempfile.mkdtemp(prefix="adr0016-legacy-"))
    shell = subprocess.Popen(f"'{PY}' '{ME}' ign '{wd}/ign.pid' & wait", shell=True,
                             start_new_session=True)
    ign = read_pidfile(wd / "ign.pid")
    time.sleep(0.2)
    leader, leader_ct = shell.pid, ctime(shell.pid)
    out("legacy.ids", {"leader": leader, "leader_sid": sid_of(leader), "ign": ign, "ign_sid": sid_of(ign),
                       "ign_pgid": pgid_of(ign)})
    os.killpg(leader, signal.SIGTERM)  # what ProcessRunner.stop() does
    shell.wait(timeout=5)
    time.sleep(0.3)
    out("legacy.leader_dead_after_TERM", ctime(leader) is None)
    out("legacy.child_alive_after_TERM (S1)", ctime(ign) is not None)
    members = session_members(leader, leader_ct or 0.0, set())
    out("legacy.found_by_leader_sid_after_leader_death", members)
    ign_ct = ctime(ign)
    out("legacy.escalate", verified_kill(ign, ign_ct or 0.0, signal.SIGKILL, sid=leader))
    time.sleep(0.2)
    out("legacy.child_alive_after_escalation", ctime(ign) is not None)


def exp_wake() -> None:
    wd = Path(tempfile.mkdtemp(prefix="adr0016-wake-"))
    rf = str(wd / "wake.json")
    w = subprocess.Popen([PY, ME, "waker", rf], start_new_session=True)
    pid = read_pidfile(Path(rf + ".ready"))
    time.sleep(0.5)
    ct = ctime(pid) if pid else None
    out("wake.signal", verified_kill(pid, ct or 0.0, signal.SIGWINCH) if pid else "no-pid")
    w.wait(timeout=10)
    out("wake.result", Path(rf).read_text())
    # default disposition check: SIGWINCH to a process with NO handler is ignored
    d = subprocess.Popen([PY, "-c", "import time; time.sleep(3)"], start_new_session=True)
    time.sleep(0.3)
    os.kill(d.pid, signal.SIGWINCH)
    time.sleep(0.3)
    out("wake.SIGWINCH_default_is_harmless", d.poll() is None)
    d.kill(); d.wait()


def role_subreaper(workdir: str) -> None:
    """Linux only: become a child subreaper, launch a double-fork+setsid
    daemonizer, and report whether the escaped grandchild reparents to us."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_CHILD_SUBREAPER = 36
    rc = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    wd = Path(workdir)
    res = {"prctl_rc": rc, "errno": ctypes.get_errno(), "sup": os.getpid()}
    # sh -> python sidesc (setsid) in background; sh exits => sidesc orphaned
    subprocess.Popen(f"'{PY}' '{ME}' sidesc '{wd}/sidesc.pid' & sleep 0.5; exit 0", shell=True).wait()
    esc = read_pidfile(wd / "sidesc.pid")
    time.sleep(0.3)
    res["escaper"] = {"pid": esc, "ppid": psutil.Process(esc).ppid() if esc else None,
                      "sid": sid_of(esc) if esc else None}
    res["in_descendants"] = esc in [c.pid for c in psutil.Process().children(recursive=True)]
    res["pidfd_available"] = hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
    if esc and res["pidfd_available"]:
        fd = os.pidfd_open(esc)
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        os.close(fd)
        os.waitpid(esc, 0)  # it is our child now: reap it, the zombie held the pid
        res["escaper_killed_via_pidfd"] = ctime(esc) is None
    elif esc:
        os.kill(esc, signal.SIGKILL)
    (wd / "sub.json").write_text(json.dumps(res))


def exp_linux() -> None:
    if not sys.platform.startswith("linux"):
        out("linux", "skipped (not Linux) — the CI Linux leg must run this")
        return
    try:
        with open(f"/proc/{os.getpid()}/stat") as f:
            field6 = int(f.read().rsplit(")", 1)[1].split()[3])
        out("linux./proc/stat session field == getsid", field6 == os.getsid(0))
    except OSError as e:
        out("linux./proc/stat", repr(e))
    wd = Path(tempfile.mkdtemp(prefix="adr0016-linux-"))
    subprocess.Popen([PY, ME, "subreaper", str(wd)], start_new_session=True).wait(timeout=30)
    for k, v in json.loads((wd / "sub.json").read_text()).items():
        out(f"linux.subreaper.{k}", v)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "subreaper":
        role_subreaper(sys.argv[2]); return 0
    if len(sys.argv) > 1 and sys.argv[1] in ("ign", "pgesc", "sidesc", "waker"):
        {"ign": role_ign, "pgesc": role_pgesc, "sidesc": role_sidesc, "waker": role_waker}[sys.argv[1]](sys.argv[2])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "forker":
        role_forker(sys.argv[2], sys.argv[3]); return 0
    if len(sys.argv) > 1 and sys.argv[1] == "sup":
        role_sup(sys.argv[2], sys.argv[3]); return 0
    which = sys.argv[1:] or ["readability", "legacy", "normal", "full", "supkill", "wake", "linux"]
    for name in which:
        print(f"--- {name} ---", flush=True)
        globals()[f"exp_{name}"]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
