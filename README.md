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
pip install rentctl
```

Requires Python 3.12+. **macOS and Linux.** There is no Windows support and no Windows
claim — process-group teardown is the safety-critical mechanism here, and it has no
tested Windows equivalent yet.

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
ports for the project, and wires both the MCP server and the cleanup hooks. Nobody else
has to edit anything.

**There is no port field.** Ports are drawn when a server starts, not written down in a
tracked file — so two checkouts of the same repo get different ports instead of fighting,
and a config file can't hand out a number the allocator never heard of. Each project owns
a contiguous block of 10 ports.

## Use

```
rent up myapp              # start (or renew) — prints the URL and the port
rent down --all --cwd .    # stop everything leased to this directory
rent ls                    # every environment on this machine
rent sweep                 # reconcile: stop what's expired or dead
rent events --summary      # what happened, and which cleanup layer did it
rent sync                  # re-approve a changed rentctl.toml
rent report-kill myapp --note "…"   # "you killed something I was using"
```

`report-kill` exists because `rentctl` cannot tell, on its own, whether a teardown
was unwanted — a kill you asked for and a kill you regret look identical from the
inside. The report lands in the same append-only log as everything else and is
matched to the teardown it disputes, so an unwanted kill becomes a fact on the
record rather than an anecdote.

Leases default to **120 minutes** and are capped at **480**. `rent up` on a live lease
renews it rather than starting a second server.

## How cleanup actually happens

Four independent layers, so no single failure leaves an orphan:

1. **You ask** — `rent down`.
2. **The session ends** — a `SessionEnd` hook, installed by `init`, tears down everything
   leased to that directory.
3. **The lease expires** — a detached watchdog per lease kills it at expiry, even if the
   session died without running its hook.
4. **The next sweep** — `rent sweep` reconciles anything the first three missed.

There is **no daemon.** State lives on disk and the OS process table is the source of
truth, so there is no background service to babysit, and nothing to resurrect after a
reboot.

### It won't kill things it doesn't own

Every lease records the process's PID **and its start time**. Before killing anything,
`rentctl` re-checks both. If the PID was recycled onto some unrelated process, the start
times disagree and it refuses — a stale lease can't get your database killed.

A listener inside a project's port block with no lease behind it is a **squatter**:
`rentctl` routes around it and reports it. It does not kill it.

And when it genuinely cannot tell whether a port is in use — no usable probe on the host —
it says so, rather than reporting the port as free. "No squatters found" means something
looked.

## Security: what you are trusting

Read this part. `rentctl` runs a command out of a config file in your repo.

**That is arbitrary code execution, and no tool can make it not be.** If you can run
`npm run dev`, you can run anything. What `rentctl` guarantees is narrower and more
useful: **it adds no *silent* path to it.**

- The command from `rentctl.toml` is shown to you and approved **once**, explicitly, at
  `rent init`.
- On approval it is copied into `rentctl`'s own registry along with a hash of the
  execution-determining fields.
- If the repo's `rentctl.toml` later changes that command, the next start **stops** and
  shows you a diff of approved-versus-current. It does not run the new command. You
  re-approve with `rent sync` or you don't.

The hash deliberately covers only the fields that determine execution, not the whole
file — hashing everything trains you to click through re-approvals for comment edits,
which defeats the point.

Consequences worth being explicit about:

- **`git pull` cannot change what `rentctl` runs.** It can change the file; it cannot
  change the approved command.
- **A repo cannot walk `rentctl` out of its own directory.** `cwd` is repo-relative and
  rejected if it is absolute, contains `..`, or symlinks outside the repo. Project names
  are filename-safe or refused.
- **Non-interactive enrollment is explicit.** CI passes `--trust-repo`, which is recorded
  as trust-on-first-use rather than being the quiet default.
- **`rentctl` only manages what you enrolled.** Test-framework servers (pytest fixtures,
  Playwright's `webServer`) own their own lifecycle and use ephemeral ports; `rentctl`
  never touches them.

### Development machines only

**Install this on machines where dev servers are meant to be disposable.** Not on a
host running anything you would mind losing.

A default install is safe: enforcement is **advisory** everywhere, and `rentctl` only
scans the port blocks of projects you enrolled. Nothing else on the machine is examined
and nothing unenrolled is ever signalled.

But `rentctl`'s whole job is killing processes that outlive a session, and strict
enforcement exists to make that unavoidable. A tool built to reap servers you forgot
about is the wrong tool to arm on a host serving traffic — there, the servers outliving
their session are *supposed* to. Keep it on development machines.

## Known gaps

Stated plainly, because a tool making safety claims should be honest about its edges:

- `runner = "compose"` is designed but not implemented. Asking for it fails with a clear
  error rather than doing nothing.
- The only environment beyond the port is `port_env`. Anything else a server needs has to
  ride inside `cmd` today.
- Windows: see Install. Not supported, not claimed.

## A note on the comments in the source

The source cites its own design decisions — `ADR-0008`, `WI-0040`, `spec §10`. Those
refer to this project's design log, which is kept privately; the reasoning around each
citation is written out where it is cited, so nothing is missing if you can't follow the
pointer.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Michael Drake.
