# Changelog — rentctl

User-visible changes to the **tool**.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: semver. Patch and minor are derived from the impact of what shipped;
a major is **declared** by a human rather than computed.

## [1.0.0] — unreleased

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
