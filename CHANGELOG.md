# Changelog — rentctl

User-visible changes to the **tool**.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: semver. Patch and minor are derived from the impact of what shipped;
a major is **declared** by a human rather than computed.

## [Unreleased] — 1.1.0 (not released)

**rentctl now tracks the workload, not the launch process.** Every environment gets its
own supervisor: a small rentctl process that starts the approved command inside a POSIX
session it owns, and stays until that whole session is verified empty. It replaces the
per-lease watchdog. One mechanism fixes the five findings below, which shared a root
cause: rentctl kept track of the process it launched, and a dev server is rarely only
that process.

### Fixed

- **S1 — Teardown could leave part of the server running.** A stop signalled the launch
  process's group and treated "the leader died" as "the server is gone", so a child that
  ignored `SIGTERM`, or outlived the shell that started it, survived teardown. Every stop
  — explicit, session end, expiry, sweep, failed start — now runs one algorithm over the
  whole session: `SIGTERM`, `SIGTERM` to anything forked during shutdown, per-process
  `SIGKILL` after the 10 s grace, and a final scan. Only an empty scan counts as
  stopped. Leases written by 1.0.x are stopped the same way.
- **S2 — A server could exist for a moment with no lease naming it.** `rent up` now
  writes the lease (`state: starting`, a fresh generation) before anything is spawned,
  and the supervisor registers itself in it before launching the command. A CLI killed
  mid-start no longer abandons a server: the supervisor carries on and the environment
  is owned, leased and expiring as usual.
- **S3 — Helpers were signalled by PID alone.** Every per-process signal re-checks the
  target's PID and start time immediately before sending (through a pidfd on Linux),
  and rentctl never sends `SIGTERM` to a supervisor. The wake signal (`SIGWINCH`) goes
  only to a supervisor whose identity was verified, and a 1.0.x watchdog is signalled
  only when its recorded start time proves it is still that watchdog.
- **S4 — Strict squatter reclaim could hit a starting server.** A starting server now has
  a lease before its first process, so its listener is never a squatter, and a listener
  in a live lease's session is that lease's wherever it bound. Strict reclaim re-checks
  under the project lock, re-verifies the listener's start time, and sends one `SIGTERM`
  without escalating. **Strict remains off by default.**
- **S5 — Session end could run out of time.** The `SessionEnd` hook ran every stop in
  turn inside its budget, which Claude Code sets at about 1.5 s for a plugin's hook. It
  now records a stop for every lease, wakes the supervisors and returns (`pending`); the
  supervisors finish cleanup after the session has gone. A lease with no living
  supervisor is handed to a detached recovery process instead of being stopped halfway.
- **Expiry lands on time.** The supervisor checks its lease at least once a second, so a
  lease is stopped within about a second of expiring, not up to a minute later.
- **A failed stop no longer counts as a teardown** in `rent events --summary`. It is its
  own event, `cleanup_incomplete`, and the lease is kept.

### Added

- **`rent down --wait SECONDS`** and MCP `env_down(wait_s=…)`: how long to wait for
  verified cleanup (default 15 s; MCP at most 60 s; `0` returns at once; the session-end
  hook uses `0`). Past the wait the answer is `"pending": true`, which is `"ok": true`.
- **Three `down` answers, each exactly what the lease says:** `"stopped": true`;
  `"stopped": false` with `"state": "cleanup_incomplete"` and `"survivors"` (the lease is
  kept and the supervisor keeps retrying); or `"stopped": null, "pending": true`.
- **New fields.** `rent up`: `state`, `supervisor_pid`. `rent ls`: `state` (`starting`,
  `running`, `stopping`, `unsupervised`, `cleanup_incomplete`), `supervisor` (`pid`,
  `registered`, `alive`), and when they apply `pending`, `stop_reason`, `survivors`,
  `identity_ambiguous` and `legacy`.
- **New error codes:** `STATE_WRITE_FAILED`, `SUPERVISOR_START_FAILED`,
  `STOP_IN_PROGRESS` (`up` on an environment still stopping after 15 s),
  `CLEANUP_INCOMPLETE` (`up` on an environment whose survivors are still named), and
  `UNSUPERVISABLE` (the command daemonized; its server left the supervised session and
  is not signalled).
- **New events:** `stop_requested` (a stop was asked for — evidence the hook fired, not
  a teardown), `cleanup_incomplete` (a stop left survivors), and `supervisor_lost` (a
  supervisor was found dead). `down` gains `generation`, `actor`, `cleanup: "verified"`,
  `escalated`, `attempts`, `supervisor_lost` and `escaped_listener`. `rent events
  --summary` counts the new kinds in a `supervision` block and counts each teardown once;
  layer counts for 1.0.x logs are unchanged.
- **Linux: whole-tree capture.** Each supervisor makes itself a child subreaper
  (`prctl(PR_SET_CHILD_SUBREAPER)`, unprivileged), so every descendant of the command is
  captured, including a double-forked `setsid()` daemon, and single-process signals go
  through a pidfd. Both are feature-detected; a host that refuses them gets the macOS
  guarantee.
- **`rent doctor` reports the supervision level**: `supervision: session` (macOS, or
  Linux without the subreaper) or `supervision: session+subreaper`, with the boundary
  of each.

### Changed

- **A dead supervisor's server keeps serving.** The next `rent ls`, `sweep`, `up` or
  `down` marks it `unsupervised` and records `supervisor_lost`; it is stopped at its
  expiry or by `rent down`, not killed early. Recovery is not instant: it happens on
  the next of those commands.
- **On macOS, a command that daemonizes is reported, not supervised.** A process that
  calls `setsid()` leaves the session; rentctl never signals it. If a command
  daemonizes during startup, `rent up` fails with `UNSUPERVISABLE` rather than
  reporting a start it cannot own.
- **`up` on an environment mid-transition** waits for a `starting` one and returns it,
  waits up to 15 s for a `stopping` one before starting fresh, and refuses one in
  `cleanup_incomplete`.

### Deprecated

- **`rent-watchdog` / `devctl-watchdog`.** Nothing starts a watchdog any more. The
  commands remain for watchdogs left running by 1.0.x: they babysit a 1.0.x lease with
  the fixed stop, exit at once (`exit-lease-supervised`) on a 1.1 lease, and print a
  one-line deprecation notice to stderr. They are removed in 2.0.

### Upgrading

- **Drain before upgrading:** run `rent down --all` in each project, then restart agent
  sessions so their MCP servers load 1.1. Not draining is safe but degraded: 1.0.x
  leases keep working (renewed in their old format, stopped by session membership), but
  a 1.0.x watchdog still running enforces expiry with the old stop. Processes left
  running from 1.0.x refuse 1.1 lease files rather than acting on them.

## [1.0.2] — release-ready, not yet published

**The guarantees, not the happy path.** Two independent reviews of 1.0.1 found the
product working as used — 73 real leases on one machine, every one ended by a cleanup
layer — and a set of places where what rentctl promised was stronger than what it did.
This release closes the ones that are small and independent. The larger one, owning a
server's whole process tree rather than its launch process, is a redesign and is not in
this release.

### Fixed

- **The MCP Registry install command could not start the server.** The registry entry
  put `--from rentctl rent-mcp` in `packageArguments`, which clients place *after* the
  package, so the command a client built — `uvx rentctl@1.0.1 --from rentctl rent-mcp` —
  failed with "An executable named `rentctl` is not provided by package `rentctl`". The
  wheel now ships a `rentctl` executable that starts the MCP server, and the entry needs
  no arguments at all: `uvx rentctl@<version>`. CI now builds that command from
  `server.json` the way a client does and runs it against the built wheel.
- **A worktree's symlinked directory could run the approved command outside the
  repository.** When a session in a git worktree started a server, rentctl carried the
  project's subdirectory over to that worktree without re-checking that it stayed inside
  it. A `frontend` symlinked elsewhere was followed. The start is now refused with
  `CWD_ESCAPES_ROOT`, using the same containment check enrollment uses.
- **Teardown could signal an unrelated process that inherited the watchdog's PID.**
  The watchdog was sent SIGTERM by PID alone. After a reboot, leases survive and PIDs
  do not. The watchdog's start time is now recorded and checked before any signal; a
  lease written by 1.0.1, which has no start time, is never signalled — removing the
  lease already makes a live watchdog exit on its next check. Skipped signals are
  logged as `watchdog_signal_skipped`.
- **`rent down app --all` also stopped `app--v2`.** A project's leases were found by a
  filename prefix, and project names may contain `--`. Leases are now matched by their
  parsed project name.
- **Servers listening only on IPv6 loopback read as unreachable and unhealthy.** Node 17+
  resolves `localhost` to `::1` first on macOS, so a Vite server on defaults was reported
  as "not on loopback, http://localhost will not reach it" — which was false. Readiness
  and health now try both `127.0.0.1` and `::1`.
- **`rent doctor` warned "no rentctl hooks" for projects the plugin wires.** It read only
  `settings.local.json`. It now reports where each project's hooks come from — plugin,
  settings, or both — and whether the plugin is actually enabled, with "cannot tell" as
  its own answer.
- **A plugin-only install silently did nothing.** Without `uv tool install rentctl`, the
  plugin's hooks exited 127 and nobody saw. Every session now starts with a warning that
  session-end cleanup is off and the command that fixes it; the session-end hook exits
  with the same message.
- **The MCP handshake reported the MCP SDK's version** (`1.30.0`) as the server's. It now
  reports rentctl's.

### Added

- **`rent --version`.** A tool that stops processes should be able to say which version
  of it did.
- **`SECURITY.md`** (private vulnerability reporting) and **`CONTRIBUTING.md`** (how to run
  the suite, and why tests must use isolated state).

### Changed

- **The README now states the limits plainly:** approval pins the command, not the code
  it runs; crash cleanup relies on expiry and sweep on a ~60 s watchdog interval; two
  sessions in one checkout share one environment and the first to end stops it;
  squatters are reported under advisory enforcement and **are** stopped under strict;
  "verified free" means free as far as this user can see. The plugin README now opens
  with a user quickstart.

## [1.0.1] — 2026-09-03

**The release 1.0.0 needed.** Two of these are defects in artifacts 1.0.0 already
published — a plugin that installed no cleanup, and no route to install it by any
command — and one is the precondition for the MCP registry listing, which cannot be
published against 1.0.0 because the registry proves package ownership by grepping the
*published* README for a marker that release does not carry.

### Fixed

- **The Claude Code plugin installed no hooks at all.** Its manifest wrapped the hook
  events in the *settings-file* envelope (`{"hooks": {"SessionEnd": …}}`) instead of
  emitting the event map directly. Claude Code read the outer key as an event name,
  matched nothing, and ignored every entry at runtime — so the plugin delivered the MCP
  server and no cleanup, while listing as installed. Since installing the cleanup hooks
  is the entire reason the plugin channel exists, 1.0.0's plugin was the half of the
  product it was meant to complete. Found by running `claude plugin validate`, which
  names it exactly.
- **The plugin could not be installed by anyone.** The published repo carried
  `plugin/.claude-plugin/plugin.json` but no marketplace manifest at its root, and
  `claude plugin marketplace add` reads the root. There was no route to the plugin by
  any command.
- **`plugin_installed()` always answered "absent."** It checked
  `~/.claude/plugins/<name>`, which Claude Code never creates — installs are recorded in
  `installed_plugins.json` and unpacked under `cache/<marketplace>/<plugin>/<version>`.
  It now reads that register, and it respects install scope: a project-scoped install no
  longer counts as coverage for a *different* project, which would have made `rent init`
  skip writing hooks that nothing else was going to supply.

### Added

- **A marketplace manifest** at `.claude-plugin/marketplace.json`, rendered from the same
  module as the plugin manifest, so the listing and the plugin it lists cannot describe
  different things.
- **`server.json`** — the MCP registry entry, generated so its version can never drift
  from the version actually on PyPI.
- **Trusted Publishing** (`.github/workflows/release.yml`): tagged releases upload to
  PyPI over OIDC, with no API token held anywhere.
- **"Development machines only"** in the README's security section. A default install is
  advisory and only scans enrolled port blocks; strict enforcement is not for a host
  serving traffic.

### Changed

- The package summary now leads with the guarantee rather than the mechanism, matching
  the repository description.

## [1.0.0] — 2026-09-02

**The first public release.** Version declared by the project owner rather than
computed: `1.0.0` is what ships when `rentctl` becomes public, on the reasoning that a
tool asking strangers to trust it with process-killing on their machine should not
present itself as provisional.

The promise this version is being held to: *rentctl becomes a tool a stranger can
install in one command and trust with their machine — public, self-service, and
runtime-agnostic.*

### Added

- **Leased dev environments with four cleanup layers** — explicit `down`, a session-end
  hook, a watchdog on lease expiry, and a sweep that reconciles against the OS. No
  daemon: state lives on disk and the OS process table is the source of truth.
- **Ports drawn per lease** from a per-project block, so concurrent sessions and
  worktree lanes never collide, and no port is written down in a tracked file.
- **An append-only event log** recording every start and teardown with its reason, the
  cleanup layer, whether the reason was declared or inferred, and whether a process was
  actually signalled.
- **MCP server** (`env_up`, `env_down`, `env_ls`, `env_sweep`) and a **Claude Code
  plugin manifest** generated from the same source the CLI writes from, so the two
  channels cannot ship different cleanup contracts under one name.
- **`rent init`** — a project enrolls itself from a `rentctl.toml` at its repo root,
  with the command shown and approved once, then hash-pinned. A changed command stops
  and shows a diff rather than running. No second party, no central registry to be
  added to (ADR-0002).
- **`rent init --adopt`** — for a project already in the registry with no config file:
  writes one from the existing entry, keeping its port block (ADR-0012).
- **`rent sync`** — re-approve a changed command and re-pin it.
- **`rent events`** — read the append-only lease event log; `--summary` folds it into
  the shape the pilot gate is scored from (ADR-0006).
- **`rent report-kill <project> --note "…"`** — report that a teardown killed something
  you were using. The one thing rentctl cannot observe about itself: a wanted kill and
  an unwanted one look identical from the inside. Recorded in the same append-only event
  log as everything else and matched to the teardown it disputes.
- **Multi-runtime enrollment.** Claude Code and Gemini CLI, each with its own config
  shape, and a runtime rentctl has never heard of can still be wired through neutral
  environment variables. Every enrolled project also gets a policy rule in its own
  context file, so an agent that cannot see the tools still knows not to start a server
  by hand (ADR-0011).
- **Apache 2.0 licence** with a `NOTICE` file, and an `authors` field naming a person
  rather than a role.

### Security

- A registered port with a listener but no lease is **reported, never killed**.
- Every kill verifies the process start time first, so a recycled PID is refused.
- "Cannot determine" is a distinct port-probe answer from "nobody is listening" — an
  unrunnable check never reads as a clean one.

### Changed

- **The distribution is `rentctl` and the command is `rent`** (ADR-0009). `devctl`,
  `devctl-watchdog` and `devctl-mcp` still work and will until the enrolled projects
  migrate and the pilot gate passes — cutting them mid-pilot would break the cleanup hooks
  the pilot exists to validate.
- **The project config file is `rentctl.toml`.** A `devctl.toml` is still read when no
  `rentctl.toml` is present, so nothing needs renaming to keep working.
- **The MCP server registers as `rentctl`**, and enrollment renames a legacy `devctl` entry
  rather than adding a second one — two entries would advertise the same four tools twice
  in one session.
- **`rent init` repairs wiring it already owns** instead of treating any existing entry as
  proof of correctness. A hook that has drifted is rewritten in place; a hook the project
  wrote itself is never touched (ADR-0012).
- **Working directories stay `~/.config/devctl` and `~/.local/state/devctl`** despite the
  rename. A deliberate mismatch: nobody types those paths, and renaming them would migrate
  every live lease and the event log for a cosmetic match.

### Fixed

- **A server that binds a specific address is no longer killed for starting correctly.**
  Readiness polled `127.0.0.1` only, so a dev server bound to a chosen interface — a
  tailnet address, a particular LAN address — answered there and never on loopback. The
  probe timed out and `rent up` stopped the process, returning "did not answer" with the
  server's own *"listening on …"* line inside the failure. Readiness now also asks which
  process is listening on the port at **any** address, and matches it by process group so
  a child (`npm` → `node`) still counts as yours and a stranger's listener does not.
- **`rent up` no longer reports success over a process that already exited.** Readiness
  proved that *something* answered the port, never that it was yours: if a foreign
  listener held the port and your command died on `EADDRINUSE`, the result was `ok` with a
  lease naming a pid that was already gone — which every later reconcile then read as a
  crashed server of yours.
- **"Started, but not where I looked" is now a distinct answer from "did not start."**
  Those call for opposite responses — leave it alone versus read the log — and were the
  same message. `rent up` now reports a `readiness` field on every start, and says so
  plainly when the server is listening somewhere the printed URL will not reach, or when
  the probe could not run at all. A probe that could not answer never kills: the lease is
  written instead, so the process stays tracked and is swept rather than orphaned.
- **A dev server now starts in the caller's worktree**, not the directory recorded in the
  registry — so a lane serves its own code rather than another lane's (ADR-0010).
- **"Cannot determine" is no longer reported as "nobody is listening."** The port probe
  raises when it cannot run instead of returning the same answer as an empty port, which
  had made squatter detection silently pass on any host without `lsof` (ADR-0008). This
  also covers the case where `lsof` *is* installed but the call fails: it exits non-zero
  with no output both for "nothing is listening" and for a usage error, and the second
  was being reported as a verified-free port on the backend that is primary on macOS.
- **An empty `--cwd` is refused rather than treated as absent.** Session-end hooks
  interpolate a shell variable; when it is unset the shell passes `""`, which used to fall
  back to the calling process's directory and tear down whatever was leased there.
- **A drifted approved command is reported by `rent sweep`**, not only by `rent up`. Sweep
  runs at session start, so the drift surfaces when there is time to deal with it rather
  than at the moment someone wanted to begin work (ADR-0003).
- **Enrollment says how it chose a runtime** — `configured`, `detected`, `requested` or
  `defaulted` — so a guess from what is installed on the machine is no longer
  indistinguishable from a fact about the project.
- **Writing into a machine-generated settings file is reported as provisional**, naming the
  destination that survives regeneration. Such a write silently reverts at the next render,
  and reporting plain success for it cost one enrolled project three days of pilot evidence.
- **Files rentctl edits keep their non-ASCII text verbatim.** JSON writes no longer
  re-encode a project's own prose into `\uXXXX` escapes.

[1.0.0]: https://github.com/Michael-Drake/rentctl
