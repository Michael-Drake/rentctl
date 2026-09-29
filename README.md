# rentctl

<!-- mcp-name: io.github.Michael-Drake/rentctl -->

**A dev server should never outlive the work that needed it.**

`rentctl` *rents* dev environments to your AI coding sessions. Every environment is a
**lease**: it dies when the session ends, when the lease expires, or when you say so —
whichever comes first. Cleanup is owned by code, not by an agent remembering to clean up.

Ships as both a CLI (`rent`) and an MCP server, so an agent can start and stop
environments through tools instead of shelling out to a raw `npm run dev` it will forget
about.

## Why

An AI coding session starts a dev server. Then the session ends — crashes, is closed, or
just moves on — and the server keeps running. By Friday there are six of them, three are
on ports you've forgotten, and one is quietly serving stale code that makes a bug look
unreproducible.

Telling the agent to clean up doesn't fix this, because the failure mode *is* the agent
not doing what it was told. `rentctl` makes the cleanup structural: the environment has an
expiry, and something other than the agent enforces it.

## Install

```
uv tool install rentctl        # or: pipx install rentctl, or: pip install rentctl
```

Requires Python 3.12+. **macOS and Linux.** There is no Windows support and no Windows
claim — POSIX sessions are the safety-critical mechanism here, and they have no tested
Windows equivalent yet.

**Upgrading from 1.0.x? Drain first.** Run `rent down --all` in each project before you
upgrade, then restart your agent sessions so their MCP servers load the new version.
Skipping it is safe but degraded: a 1.0.x environment keeps working under 1.1 — `rent
up` renews it, and `rent down` and `rent sweep` stop it by its whole session — but a
1.0.x watchdog still running from before the upgrade enforces its expiry with the old
code, which can miss a child that outlived its shell.

## First run

A framework-free server, so nothing but Python is involved. In a scratch repo:

```
mkdir hello && cd hello && git init -q
cat > rentctl.toml <<'EOF'
[project]
name = "hello"
runner = "process"

[profiles.default]
cmd = 'python3 -m http.server --bind 127.0.0.1 "$PORT"'
cwd = "."
port_env = "PORT"
EOF
rent init
```

`init` shows you exactly what it will run and waits for an explicit yes:

```
Enroll project 'hello' (runner: process)
  source:     /home/you/hello
  port block: 6200-6209

rentctl will run these commands on your behalf:
  [default]
    command: python3 -m http.server --bind 127.0.0.1 "$PORT"
    in:      /home/you/hello
    port via $PORT

Approve and enroll? [y/N] y
```

Your block will differ — blocks are drawn from 5100 upward, lowest free first. Then:

```
$ rent up hello
{
  "ok": true,
  "project": "hello",
  …
  "url": "http://localhost:6200",
  "port": 6200,
  …
  "lease_expires": "2026-09-28T15:50:21.327166-05:00",
  "already_running": false,
  "state": "running",
  "supervisor_pid": 24202,
  …
  "readiness": "answered"
}

$ rent ls                      # one row: hello, port 6200, "state": "running", "healthy": true
$ curl -sI http://localhost:6200/
HTTP/1.0 200 OK

$ rent down --all --cwd . --reason session-end    # what the SessionEnd hook runs
{
  "ok": true,
  …
  "downed": [ { "project": "hello", …, "port": 6200, "was_running": true,
                "stopped": null, "pending": true, "state": "stopping",
                "detail": "supervisor 24202 is completing cleanup; rent ls shows the outcome" } ]
}

$ curl -sI http://localhost:6200/ || echo gone
gone
```

(Output trimmed where marked `…`.) That last step is the whole product: in an enrolled
Claude Code or Gemini CLI session you never type `rent down` — the session ending does.
The hook doesn't wait for the server to die; it asks, and the environment's supervisor
finishes the job (see [How cleanup actually happens](#how-cleanup-actually-happens)).
`rent down hello`, typed by hand, waits and answers `"stopped": true`.
`rent events --summary` afterwards shows the teardown and which cleanup layer did it.

If something doesn't behave, see [Troubleshooting](#troubleshooting).

## Enroll a project

A project describes itself in a `rentctl.toml` at its repo root (a `devctl.toml`
left over from before the rename is still read, so nothing needs renaming to keep
working):

```toml
[project]
name = "myapp"          # lowercase, no separators — it is used as a filename
runner = "process"

[profiles.default]
cmd = 'npm run dev -- --port "$PORT" --strictPort'
cwd = "frontend"        # repo-relative, never absolute
port_env = "PORT"       # rentctl sets this in the child's environment
```

Then, from the repo:

```
rent init
```

`init` shows you the exact command it will run, asks you to approve it, claims a block of
ports for the project, registers the MCP server, installs the cleanup hooks (unless the
Claude Code plugin already supplies them), and adds a short rule to `CLAUDE.md` /
`GEMINI.md` telling agents to start servers through rentctl rather than by hand. Nobody
else has to edit anything.

**There is no port field.** Ports are drawn when a server starts, not written down in a
tracked file — so two checkouts of the same repo get different ports instead of fighting,
and a config file can't hand out a number the allocator never heard of. Each project owns
a contiguous block of 10 ports.

## Use

```
rent up myapp              # start (or renew) — prints the URL and the port
rent down myapp            # stop it; waits up to 15 s (--wait SECONDS, --wait 0 = don't wait)
rent down --all --cwd .    # stop everything leased to this directory
rent ls                    # every environment on this machine, with its state
rent sweep                 # reconcile: stop what's expired or dead
rent events --summary      # what happened, and which cleanup layer did it
rent sync                  # re-approve a changed rentctl.toml
rent doctor                # are the shims, registry and hooks actually working?
rent report-kill myapp --note "…"   # "you killed something I was using"
```

`report-kill` exists because `rentctl` cannot tell, on its own, whether a teardown
was unwanted — a kill you asked for and a kill you regret look identical from the
inside. The report lands in the same append-only log as everything else and is
matched to the teardown it disputes, so an unwanted kill becomes a fact on the
record rather than an anecdote.

Leases default to **120 minutes** and are capped at **480**. `rent up` on a live lease
renews it rather than starting a second server.

## Which clients get automatic cleanup

| Client | How it is wired | Session-end cleanup |
|---|---|---|
| Claude Code | the plugin, or `rent init` (`.mcp.json` + `.claude/settings.local.json`) | **Yes** — SessionEnd runs `rent down --all --cwd "$CLAUDE_PROJECT_DIR" --reason session-end`; SessionStart runs `rent sweep` |
| Gemini CLI | `rent init` (`.gemini/settings.json`), when Gemini is used in the project or installed | **Yes** — same hooks, scoped by `$GEMINI_PROJECT_DIR` |
| Any other MCP client | register `rent-mcp` yourself | **No** — lease expiry and sweep only |
| A plain shell | `rent` | **No** — lease expiry and sweep only |

"Expiry and sweep only" still guarantees the server dies — just at the end of its lease
(120 minutes by default), not when you close the window.

**Plugin-only installs need the CLI too.** The Claude Code plugin calls the `rent` and
`rent-mcp` you installed with `uv tool install rentctl`; it does not bundle them, and it
deliberately does not fetch its own copy — a second, different rentctl acting on the same
leases is worse than none. Without `rent` on `PATH`, session-end cleanup is **off**: every
session then starts with a warning saying so and naming the install command. Install the
package first, then run `rent doctor`, which reports whether each project's hooks come
from the plugin, from `settings.local.json`, or from both.

**From the MCP Registry**, a client runs `uvx rentctl@<version>`: the `rentctl`
executable is the MCP server, the same as `rent-mcp` (typed in a terminal it waits for a
client on stdin). That gives you the tools with expiry-and-sweep cleanup only — the
hooks come from the plugin or `rent init`. `rent --version` says which version you are
running.

## How cleanup actually happens

Every environment has its own **supervisor**: a small rentctl process that starts your
command inside a POSIX session it owns and stays until that whole session — the command,
its children, and anything they spawned — has been checked and found empty. The lease is
written before the command starts, so there is no moment when something is running that
rentctl has no record of.

Every stop, whatever asked for it, is the same: `SIGTERM` to every process in the
session, `SIGTERM` to anything forked while it shuts down, `SIGKILL` to each process
still there after 10 s, then a final scan. A child that ignores `SIGTERM`, or that
outlived the shell that launched it, is stopped with the rest.

Four independent layers decide *when*, so no single failure leaves an orphan:

1. **You ask** — `rent down`.
2. **The session ends** — a `SessionEnd` hook asks for a stop of everything leased to
   that directory.
3. **The lease expires** — the supervisor starts the stop within about a second of the
   lease running out, even if the session died without running its hook.
4. **The next sweep** — `rent sweep` (which the hooks also run at session start) and
   `rent ls` clean up what the first three missed, including an environment whose
   supervisor was killed.

**What `rent down` tells you** is what the lease says, in one of three shapes:

- `"stopped": true` — the session was scanned empty and the lease removed.
- `"stopped": false, "state": "cleanup_incomplete", "survivors": [...]` — something could
  not be stopped (a child that switched user with `sudo`, say). The lease is **kept**, so
  the survivors stay named and the port stays out of the draw; the supervisor keeps
  retrying (5 s, 15 s, 60 s, then every minute), `rent down` retries at once, and `rent
  up` refuses with `CLEANUP_INCOMPLETE` until it clears.
- `"stopped": null, "pending": true` — the stop was accepted and is still running;
  `rent ls` shows how it ended. This is `"ok": true`: the supervisor finishes regardless.

`rent down` waits up to 15 s for the first two (`--wait SECONDS`; `--wait 0` returns
at once). The MCP tool `env_down` takes the same wait as `wait_s` — 15 s by default,
60 s at most.

**Session end returns fast; cleanup finishes in the background.** Claude Code gives a
plugin's `SessionEnd` hook about 1.5 s. So the hook's `rent down --all --reason
session-end` doesn't wait: it records a stop for every lease, wakes each supervisor, and
returns `pending`. The supervisors don't need the session to finish. A lease with no
living supervisor is handed to a detached recovery process rather than stopped halfway.

**A crashed session is cleaned up by layers 3 and 4, not 2.** If the session dies
without firing its hook, its server keeps running until the lease expires — up to the
full lease length. Shorter leases (`rent up myapp --lease-minutes 30`) shorten that
window. A sweep does not stop a live, unexpired lease; it cannot tell a crashed session
from one that is still working.

**If a supervisor is killed, recovery waits for the next command.** The server keeps
serving. The next `rent ls`, `sweep`, `up` or `down` notices, marks the lease
`unsupervised`, and records `supervisor_lost`; it is not killed early. At its expiry the
next of those commands stops it, and `rent down` stops it now. Nothing notices at the
moment the supervisor dies, because nothing else is running to notice.

There is **no daemon.** There is one supervisor per live environment, and it exits once
its environment is verified gone. State lives on disk and the OS process table is the
source of truth, so there is no background service to babysit, and nothing to resurrect
after a reboot.

### What supervision covers, per platform

**macOS.** Every process that stays in the environment's session is found and stopped:
orphans of a launch shell that exited, processes that ignore `SIGTERM`, processes that
move into their own process group, children forked during shutdown. A process that calls
`setsid()` — which is what daemonizing (double-fork plus `setsid`) does — leaves the
session and is **outside** supervision. rentctl never signals it; if it holds the lease's
port, the teardown reports it. When a command daemonizes during startup — its own
session empties while a process outside it answers the port — `rent up` fails with
`UNSUPERVISABLE` instead of reporting a start it cannot own. Run such servers in the
foreground.

**Linux.** Each supervisor also makes itself a *child subreaper*
(`prctl(PR_SET_CHILD_SUBREAPER)`, no privileges needed), so orphaned descendants are
reparented to it instead of to init. Every descendant of your command is captured,
including a daemon that double-forks and calls `setsid()`. Signals to single processes
go through a pidfd where the kernel has them. What still escapes is work that is not
your command's descendant — handed to another manager such as `systemd-run`, a
container runtime, an already-running `tmux` server, or `at`/`cron` — and the orphaned
children of a process placed in another PID namespace with `setns(2)` (which needs
`CAP_SYS_ADMIN` over that namespace): they go to that namespace's init. The capture also
ends if the supervisor itself is killed. Where the subreaper is refused (some
restricted containers), Linux gets the macOS guarantee.

`rent doctor` reports which one is in effect: `supervision: session` or
`supervision: session+subreaper`. **Neither is a sandbox** — a command that sets out to
escape supervision can.

### One active session per checkout

A lease belongs to a **(project, directory)** pair, not to a session. Two agent sessions
open in the *same* checkout share one environment — the second `rent up` renews the
first's server rather than starting its own — and the first session to end runs
`rent down --all --cwd` and tears it down under the other.

Separate git worktrees avoid this entirely: each worktree is its own directory, gets its
own lease and its own port, and its session end touches only its own server. Shared
sessions in one checkout are a known limitation, with no fix promised yet.

### It won't kill things it doesn't own

Every lease records its supervisor's PID **and start time**, and the supervisor's PID is
the environment's session id (a lease from 1.0.x uses its server's instead). A process is the environment's only if it is in that
session (or, on Linux, descends from the supervisor), and each one's identity is
re-checked immediately before it is signalled. If the session's PID has been recycled
onto some unrelated process, nothing under it can be proved ours: the lease is kept,
marked `identity_ambiguous`, and nothing is signalled — a stale lease can't get your
database killed.

A listener inside an enrolled project's port block with no lease behind it is a
**squatter**. What happens to it depends on the machine's enforcement mode:

- **advisory** (the default, and the only mode any command sets) — `rent up` routes
  around it, and `rent ls` / `rent sweep` report it. It is never signalled.
- **strict** — `rent sweep` sends each squatter `SIGTERM`, and reports what it killed.
  Since the session-start hook runs `rent sweep`, under strict that happens at every
  session start. Strict is set by the `enforcement` field in rentctl's machine-local
  registry; nothing turns it on for you. See
  [Development machines only](#development-machines-only) before you do.

When it genuinely cannot tell whether a port is in use — no usable probe on the host —
it says so, rather than reporting the port as free. "No squatters found" means something
looked.

**What it can see is limited to what you can see.** The port probe (`lsof`, or `psutil`
on Linux) runs as your user, and an unprivileged probe can miss sockets owned by *other*
users. So "free" means free as far as this user can see. A real collision still surfaces:
the server's own `bind()` fails, and `rent up` reports the failed start (with the log
tail) instead of writing a lease.

## Security: what you are trusting

Read this part. `rentctl` runs a command out of a config file in your repo.

**That is arbitrary code execution, and no tool can make it not be.** If you can run
`npm run dev`, you can run anything. What `rentctl` guarantees is narrower and more
useful: **it adds no *silent* path to it.**

- The command from `rentctl.toml` is shown to you and approved **once**, explicitly, at
  `rent init`.
- On approval it is copied into `rentctl`'s own registry along with a hash of the
  execution-determining fields: `runner`, `cmd`, `cwd` and `port_env`.
- If the repo's `rentctl.toml` later changes any of them, the next start **stops**
  (`CMD_CHANGED`) and shows you a diff of approved-versus-current. It does not run the
  new command. You re-approve with `rent sync` or you don't.

The hash deliberately covers only the fields that determine execution, not the whole
file — hashing everything trains you to click through re-approvals for comment edits,
which defeats the point.

**Approval pins the command, not the code it runs.** `npm run dev` is the same string
whatever `package.json` says `dev` means. An edit to a `package.json` script, a Makefile,
or the application code itself changes what actually executes *without* tripping
`CMD_CHANGED`. The pin stops a changed `rentctl.toml` from quietly running something new;
it is not a review of the repo. Treat pulling someone else's branch the way you already
would before running its dev server by hand.

Consequences worth being explicit about:

- **`git pull` cannot change the approved command.** It can change the file, and it can
  change the code that command runs; it cannot change which command `rentctl` runs.
- **A repo cannot walk `rentctl` out of its own directory.** `cwd` is repo-relative and
  rejected if it is absolute, contains `..`, or symlinks outside the repo. Project names
  are filename-safe or refused.
- **Non-interactive enrollment is explicit.** CI passes `--trust-repo`, which is recorded
  as trust-on-first-use rather than being the quiet default.
- **`rentctl` only manages what you enrolled.** Test-framework servers (pytest fixtures,
  Playwright's `webServer`) own their own lifecycle and use ephemeral ports; `rentctl`
  never touches them.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

### Development machines only

**Install this on machines where dev servers are meant to be disposable.** Not on a
host running anything you would mind losing.

A default install is safe: enforcement is **advisory**, and `rentctl` only scans the
port blocks of projects you enrolled. Nothing else on the machine is examined and nothing
unenrolled is ever signalled.

But `rentctl`'s whole job is killing processes that outlive a session, and strict
enforcement exists to make that unavoidable. A tool built to reap servers you forgot
about is the wrong tool to arm on a host serving traffic — there, the servers outliving
their session are *supposed* to. Keep it on development machines.

## Troubleshooting

- **`rent doctor`** checks that the `rent` shim runs, the registry loads, and each
  enrolled project's hooks are wired, and says which supervision level this host gets. If `rent` itself is missing, the same check runs
  as `python3 -m rentctl.doctor`.
- **Server logs** are in `~/.local/state/devctl/logs/<project>-<timestamp>.log` (under
  `$XDG_STATE_HOME` if set). A failed `rent up` includes the log's tail in its output.
- **What happened, and why**: `rent events` lists every start and teardown with the layer
  that did it; the raw log is `~/.local/state/devctl/events.jsonl`. A teardown's `down`
  event says whether the stop was verified and whether it needed `SIGKILL`
  (`"escalated": true`). `stop_requested` means a stop was asked for (for example by the
  session-end hook) and is not itself a teardown.
- **`rent ls` shows each lease's `state`** and whether its supervisor is alive:
  - `stopping` or `"pending": true` — a stop is under way; look again in a few seconds.
  - `unsupervised` — its supervisor died. The server is still yours and still serving;
    it is stopped at expiry by the next `ls`/`sweep`/`up`/`down`, or now by `rent down`.
    `rent up` will not renew it. (A lease started by 1.0.x also reads `unsupervised`,
    with `"legacy": true`; `rent up` does renew that one.)
  - `cleanup_incomplete` with `survivors` — those PIDs would not die. Find out why (a
    different user? stuck in the kernel?) before reusing the port; `rent down` retries.
- **`UNSUPERVISABLE` from `rent up`** — the command daemonized, so its server left the
  session rentctl supervises. Run it in the foreground. The stray server is left alone;
  stop it yourself.
- **The supervisor notes what it did in the server's log**, on lines prefixed
  `[rentctl supervisor <pid>]`.
- The registry of enrolled projects and approved commands is
  `~/.config/devctl/registry.json`. It is rentctl's own record — change it with
  `rent init` / `rent sync`, not by hand.

(The directories say `devctl`, the project's former name. That is deliberate — renaming
them would orphan every live lease.)

## Known gaps

Stated plainly, because a tool making safety claims should be honest about its edges:

- **The approval pin covers the command, not the code** — see Security.
- **One active session per checkout** — see above; use worktrees for parallel sessions.
- **Crash cleanup waits for expiry** — a session that dies without its hook leaves its
  server up until the lease runs out.
- **Recovery waits for the next command** — if a supervisor is killed, nothing acts
  until the next `rent ls`/`sweep`/`up`/`down`.
- **Daemonizing commands are not supervised on macOS**, and on Linux work handed to
  another process manager is not — see
  [What supervision covers](#what-supervision-covers-per-platform).
- **Only Claude Code and Gemini CLI get session-end hooks.** Other clients get expiry and
  sweep.
- `runner = "compose"` is designed but not implemented. Asking for it fails with a clear
  error rather than doing nothing.
- The only environment beyond the port is `port_env`. Anything else a server needs has to
  ride inside `cmd` today.
- Windows: see Install. Not supported, not claimed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## A note on the comments in the source

The source cites its own design decisions — `ADR-0008`, `WI-0040`, `spec §10`. Those
refer to this project's design log, which is kept privately; the reasoning around each
citation is written out where it is cited, so nothing is missing if you can't follow the
pointer.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Michael Drake.
