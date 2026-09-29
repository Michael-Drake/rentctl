# Troubleshooting rentctl environments

Every tool returns `{"ok": true, ...}` or `{"ok": false, "error": "CODE", "message": "...", ...}`.
Read `message` and any extra fields before acting. They usually name the fix.

## Error codes

| Code | What it means | What to do |
|---|---|---|
| `NOT_A_PROJECT` | No `rentctl.toml` at or above this directory. | Tell the user the checkout is not set up for rentctl. Adding a `rentctl.toml` and running `rent init` is their decision. Do not write the file yourself or start the server by hand. |
| `UNKNOWN_PROJECT` | The named project, or the one `rentctl.toml` declares (`enrolled: false`), is not enrolled on this machine. | Ask the user to run `rent init` in the project root. It shows the exact command and asks for approval. Never use `--trust-repo`. |
| `CMD_CHANGED` | `rentctl.toml` no longer matches the command the user approved. Nothing new was run. | The user reviews the change and re-approves with `rent sync`. Never run it for them or edit the file back to match. |
| `NOT_APPROVED` | The user declined the command at `rent init` or `rent sync`. | Respect it. Nothing to retry. |
| `PROFILE_MISMATCH` | This directory already runs a different profile (`running_profile`). `held_by` names the sessions using it. | Ask before stopping it. Or run the other profile from another worktree. |
| `UNKNOWN_PROFILE` | The project has no profile by that name. | Use `default` or a profile listed in `rentctl.toml`. |
| `START_TIMEOUT` | The server did not become ready in time and its start was cancelled. | Read `log_tail`. It is usually the app's own error (missing dependency, bad env var, port bind failure). Fix it, then retry once. |
| `UNSUPERVISABLE` | The command daemonized, so its server left the process session rentctl supervises. | The command must run in the foreground. The user fixes it in `rentctl.toml`, then runs `rent sync`. The stray server is not rentctl's to stop. |
| `PORT_SQUATTED` | A process rentctl did not start holds a port in the project's block. `env_ls` and `env_sweep` list it as `"status": "squatter"` with its pid and name. | rentctl routes around it and never kills it. Report the holder. The user decides whether to stop it. |
| `BLOCK_EXHAUSTED` | Every port in the project's block is taken. `holders` names each one. | Stop an environment the user no longer needs (see `env_ls`), or have the user free a squatted port. |
| `STOP_IN_PROGRESS` | A stop in this directory has not finished yet. | Wait a few seconds, check `env_ls`, then call `env_up` again. |
| `CLEANUP_INCOMPLETE` | An earlier stop left survivors (`survivors` lists PIDs), or ownership could not be proved (`identity_ambiguous`). The lease is kept so they stay attributable. | Do not retry blindly. Report the PIDs. The user investigates before the port is reused. `rent down PROJECT` retries the stop. |
| `UNSUPPORTED_ENVIRONMENT` | rentctl cannot read the process table here. This happens in a sandboxed agent shell such as Codex's. Nothing was started. | Use the MCP tools (`env_up` and the others), which run outside the sandbox. Do not ask for wider sandbox permissions. |
| `REGISTRY_INVALID` | rentctl's registry of enrolled projects is missing or corrupt. | Have the user run `rent doctor`. Do not edit the registry by hand. |
| `INVALID_CWD` | `--cwd` was passed empty, usually an unexpanded shell variable. | Pass a real directory, or omit `--cwd`. |
| `SUPERVISOR_START_FAILED`, `STATE_WRITE_FAILED`, `INTERNAL` | rentctl itself failed. | Run `rent doctor` and `rent events --since 1h`, and report both to the user. |

## Tools are missing

If your session has no `env_up` / `env_down` / `env_ls` / `env_sweep` tools:

1. Ask the user to run `rent doctor` in a terminal. It checks the `rent` shim, the registry,
   the wiring of each enrolled project, and which session id it can see.
2. If `rent` is not found, the package is not installed: `uv tool install rentctl` (or
   `pipx install rentctl`).
3. After installing or re-wiring, the agent session must be restarted to load the MCP server.

## Readiness is not health

`readiness: "answered"` means a TCP connect to loopback succeeded, nothing more. The app
can still return errors on every request. Fetch the URL when your client allows it, or
tell the user it is started but unverified. `listening` means the app bound a specific
non-loopback address, so `url` will not reach it. The app's bind address needs
changing. It is not a rentctl fault.

## Evidence to gather before a retry

- `log_tail` in the failed `env_up` result: the server's own last lines of output.
- `rent doctor`: install and wiring health.
- `rent events --project NAME --since 1h`: every start, stop, renewal and release, with
  its reason. `claim_released` means a session let go while others still hold it. It is
  not a teardown.
- `rent ls` (or `env_ls`): each environment's `state`, `claims`, `healthy`, and whether
  its supervisor is alive.

## States seen in `env_ls`

- `running`: supervised and serving.
- `stopping` or `"pending": true`: a stop is under way. Look again shortly.
- `unsupervised`: its supervisor died. The server is still tracked and is stopped at
  expiry. `env_up` will not renew it, so stop it and start again.
- `cleanup_incomplete`: see `CLEANUP_INCOMPLETE` above.
