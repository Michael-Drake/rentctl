"""Install smoke test — run against the BUILT WHEEL, never the source tree.

Usage:  python wheel_smoke.py <dist-dir>

Every check here exercises an artifact the way a stranger meets it. The suite proves
the source; this proves the thing on PyPI will start. Three past defects lived exactly in
that gap — a plugin whose hooks were never read, a marketplace manifest that was never
published, and an MCP registry command that named an executable the wheel did not
contain — and each passed a green suite, because no check ran the artifact as its
consumer would.

Everything runs in throwaway directories: its own uv tool dir, its own bin dir, its own
rentctl state and config homes. Nothing here reads or writes a real lease.

The registry command is built HERE, from server.json, by the rules a registry client
follows — deliberately not by calling rentctl's own renderer. A test that asks the code
under test how to invoke itself shares its premise and cannot fail on the thing it is
meant to catch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "wheel-smoke", "version": "0"},
    },
}


def fail(msg: str) -> None:
    print(f"SMOKE FAIL: {msg}", flush=True)
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"ok  {msg}", flush=True)


def run(cmd: list[str], env: dict[str, str], stdin: str | None = None, timeout: int = 300):
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, env=env, timeout=timeout
    )


def args_of(entries: list[dict]) -> list[str]:
    """Flatten registry Argument objects in order: positional -> value; named -> name [value]."""
    out: list[str] = []
    for a in entries or []:
        if a.get("type") == "named":
            out.append(a["name"])
            if a.get("value") is not None:
                out.append(str(a["value"]))
        else:
            out.append(str(a.get("value", a.get("valueHint", ""))))
    return out


def client_command(server: dict, pin: str) -> list[str]:
    """The command a registry client assembles for a PyPI package run with uvx:
    runtimeHint, runtimeArguments, identifier<pin>version, packageArguments.

    Clients differ on the pin: VS Code writes `identifier@version`, a hand-built
    command usually `==`. uvx accepts both, and both are run below."""
    pkgs = [p for p in server.get("packages", []) if p.get("registryType") == "pypi"]
    if len(pkgs) != 1:
        fail(f"expected exactly one pypi package in server.json, found {len(pkgs)}")
    p = pkgs[0]
    hint = p.get("runtimeHint", "uvx")
    return (
        [hint]
        + args_of(p.get("runtimeArguments", []))
        + [f"{p['identifier']}{pin}{p['version']}"]
        + args_of(p.get("packageArguments", []))
    )


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: wheel_smoke.py <dist-dir>")
    dist = Path(sys.argv[1]).resolve()
    wheels = sorted(dist.glob("rentctl-*.whl"))
    if len(wheels) != 1:
        fail(f"expected one rentctl wheel in {dist}, found {[w.name for w in wheels]}")
    wheel = wheels[0]
    version = wheel.name.split("-")[1]
    ok(f"wheel {wheel.name} (version {version})")

    with zipfile.ZipFile(wheel) as z:
        ep_name = next(n for n in z.namelist() if n.endswith("entry_points.txt"))
        entry_points = z.read(ep_name).decode()
    scripts = {
        line.split("=")[0].strip()
        for line in entry_points.split("[console_scripts]", 1)[1].split("[", 1)[0].splitlines()
        if "=" in line
    }
    ok(f"console scripts in the wheel: {sorted(scripts)}")

    tmp = Path(tempfile.mkdtemp(prefix="rentctl-smoke-"))
    env = dict(os.environ)
    env.update(
        UV_TOOL_DIR=str(tmp / "tools"),
        UV_TOOL_BIN_DIR=str(tmp / "bin"),
        RENTCTL_STATE_HOME=str(tmp / "state"),
        RENTCTL_CONFIG_HOME=str(tmp / "config"),
        PATH=f"{tmp / 'bin'}{os.pathsep}{env.get('PATH', '')}",
    )
    for k in ("DEVCTL_STATE_HOME", "DEVCTL_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        env.pop(k, None)

    try:
        r = run(["uv", "tool", "install", str(wheel)], env)
        if r.returncode != 0:
            fail(f"uv tool install failed:\n{r.stderr}")
        ok("uv tool install from the wheel")

        rent = str(tmp / "bin" / "rent")
        r = run([rent, "--version"], env)
        if r.returncode != 0 or r.stdout.strip() != f"rentctl {version}":
            fail(f"rent --version: exit {r.returncode}, stdout {r.stdout!r}, stderr {r.stderr!r}")
        ok(f"rent --version -> {r.stdout.strip()}")

        r = run([rent, "doctor"], env)
        try:
            report = json.loads(r.stdout)
        except json.JSONDecodeError:
            fail(f"rent doctor did not print JSON: exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        install = next((c for c in report.get("checks", []) if c.get("check") == "install"), None)
        if install is None or install.get("status") != "ok":
            fail(f"doctor install check not ok: {install}")
        ok(f"rent doctor answers; install check: {install['detail']}")

        r = run([str(tmp / "bin" / "rent-mcp")], env, stdin=json.dumps(INIT) + "\n", timeout=120)
        if '"serverInfo"' not in r.stdout:
            fail(f"rent-mcp did not answer initialize: exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        ok("rent-mcp answers MCP initialize")

        server = json.loads((ROOT / "server.json").read_text())
        # Resolve rentctl from THIS dist dir. The version under test is not on PyPI
        # yet, so the only place it can come from is the wheel we just built.
        uv_env = dict(env, UV_FIND_LINKS=str(dist), UV_CACHE_DIR=str(tmp / "uvcache"))
        for pin in ("@", "=="):
            cmd = client_command(server, pin)
            if cmd[0] != "uvx":
                fail(f"runtimeHint is {cmd[0]!r}; this smoke only knows how to run uvx")
            if f"rentctl{pin}{version}" not in cmd:
                fail(f"server.json pins {cmd} but the wheel is {version}")
            r = run(cmd, uv_env, stdin=json.dumps(INIT) + "\n", timeout=300)
            if f'"version":"{version}"' not in r.stdout.replace(" ", ""):
                fail(
                    f"the registry client command did not answer initialize as "
                    f"rentctl {version}: {cmd}\n"
                    f"exit {r.returncode}\nstdout {r.stdout}\nstderr {r.stderr}"
                )
            ok(f"registry client command answers MCP initialize: {' '.join(cmd)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SMOKE PASS", flush=True)


if __name__ == "__main__":
    main()
