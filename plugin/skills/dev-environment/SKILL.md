---
name: dev-environment
description: "Start, reuse, check, diagnose and stop a checkout's local dev server through rentctl's leased environments (env_up, env_down, env_ls, env_sweep, or the rent CLI). Use when the user wants to run this app, start the dev server for this worktree, preview or see their changes in a browser, asks why their dev environment won't start, wants to stop the environment they are using, or mentions rentctl or rent up/down. Not for production services or deploys, not for short-lived servers a test framework starts itself (pytest fixtures, jest, a Playwright webServer), and not for ordinary code edits."
---

# Dev environment (rentctl)

rentctl leases a dev server to the checkout that asked for it: one port, one process tree,
an expiring lease, verified cleanup. Every start goes through it so nothing outlives the
work. Follow these steps in order.

## 1. Identify the project

- Call `env_up` with no `project`: it resolves the project from the `rentctl.toml` at or
  above the checkout. Name a project only when the user names one.
- `NOT_A_PROJECT` (no `rentctl.toml`) or `UNKNOWN_PROJECT` with `enrolled: false` means the
  checkout is not enrolled. Enrolling is the user's decision. Tell them to run `rent init`
  in the project root themselves; it shows the exact command and asks them to approve it.
- Never run `rent init --trust-repo`, never create or edit `rentctl.toml` to make a start
  succeed, and never start the server by hand (`npm run dev`, `python manage.py
  runserver`, ...) as a fallback. Stop and report instead.

## 2. Check what is already running

- Call `env_ls` first. If this checkout already has an environment, reuse it: calling
  `env_up` again renews it and returns the same URL. Do not start a duplicate.
- One directory holds one environment. `PROFILE_MISMATCH` means a different profile is
  running here; `held_by` names the sessions using it. Ask the user before stopping it,
  or use another worktree for the other profile.
- Several sessions in one checkout share one server. `claims` lists every session
  entitled to it and `shared_with` names the others.

## 3. Start or renew

- Prefer the MCP tool `env_up` (`project?`, `lease_minutes`, `profile`). Tool names here are
  bare; your client may show them with a prefix (for example `mcp__..._env_up`).
- Use the CLI `rent up` only when the MCP tools are unavailable **and** your shell is not
  sandboxed. In Codex, never run `rent` from the shell: the sandbox hides the process
  table, so it refuses with `UNSUPPORTED_ENVIRONMENT`. Use the MCP tools, which run
  outside the sandbox.

## 4. Report what you got

- Give the user the returned `url` and `port` exactly as returned. Never assume a
  conventional port (3000, 5173, 8000, ...).
- Check `readiness` before calling it working:
  - `answered`: something accepted a connection on loopback. An open socket is not a
    healthy app; fetch the URL to confirm when your client allows it.
  - `listening`: it started but bound a non-loopback address, so the `url` will not reach
    it. Report `readiness_detail`; the fix is in the app's bind address.
  - `unknown`: the probe could not run. The environment is tracked; say it is unconfirmed.
  - `not_probed`: a renewal of an environment that was already running.
- Mention `serving` when it is not the directory the user expects.

## 5. Tell failures apart

Each has a different owner. Details and fixes: [references/troubleshooting.md](references/troubleshooting.md).

- **Not enrolled** (`NOT_A_PROJECT`, `UNKNOWN_PROJECT`): the user runs `rent init`.
- **Command changed** (`CMD_CHANGED`): `rentctl.toml` no longer matches the command the
  user approved. They re-approve with `rent sync`. Never approve on their behalf.
- **Tool unavailable** (no `env_*` tools in your session): have the user run
  `rent doctor`; if `rent` is missing, `uv tool install rentctl`, then restart the session.
- **Startup failure** (`START_TIMEOUT` or another start error with `log_tail`): read
  `log_tail` first, fix the app's error, then retry once.
- **Port problems** (`PORT_SQUATTED` squatters, `BLOCK_EXHAUSTED`): a process rentctl did not
  start holds ports in the project's block. rentctl never kills it; report the holder.
- **Stop still running** (`STOP_IN_PROGRESS`, `CLEANUP_INCOMPLETE`): wait and check
  `env_ls`; with `survivors`, report the PIDs, do not retry blindly.
- **Sandboxed shell** (`UNSUPPORTED_ENVIRONMENT`): switch to the MCP tools.

## 6. Diagnose before retrying

Never loop on `env_up`. Before a second attempt, look at the evidence:

- `log_tail` in the failed result (the server's own output).
- `rent doctor`: are the shims, registry and hooks live, and which session id was found.
- `rent events --project NAME --since 1h`: every start, stop and failure with its reason.

If you cannot run these (sandboxed shell), ask the user to run them in a terminal.

## 7. Stop

- `env_down` with no `force` releases **only this session's hold**. If other sessions
  still hold it, the result is `stopped: false` with `held_by`; tell the user it keeps
  running for them.
- `pending: true` is success: the stop was accepted and is finishing; `env_ls` shows the
  outcome.
- Use `force=true` only when the user explicitly asks to stop it for everyone; report
  `overrode_claims`.
- Never run `rent down --all` or `rent down --all-instances` for a one-session request:
  those reach other sessions and other worktrees.
- Session hooks release this session's hold when the session ends, so stopping at the
  end is courtesy, not a requirement.

## 8. Leave test servers to the test framework

If a test runner starts its own server (a pytest fixture, jest global setup, Playwright
`webServer`), let it. Do not wrap it in rentctl and do not start a leased environment
for it.

## 9. Renew for long work

Calling `env_up` again renews your lease. Leases expire (default 120 minutes) and the
server is stopped at expiry. For a long task, pass a larger `lease_minutes` (max 480) or
renew before it lapses, and tell the user the expiry (`lease_expires`).

`env_sweep` reconciles every lease on the machine; call it only when the user asks for a
cleanup, not as a routine step.

Worked start, use and stop sequence with example results, and a verified-cleanup check:
[references/examples.md](references/examples.md).
