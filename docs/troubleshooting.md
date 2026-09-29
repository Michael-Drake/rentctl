# Troubleshooting rentctl in an agent client

Start with:

```
rent doctor
```

If `rent` itself is missing, run `python3 -m rentctl.doctor` or reinstall with
`uv tool install rentctl`.

## The agent has no rentctl tools

- **`rent`/`rent-mcp` not on `PATH`** — the plugin runs your installed package. Install it
  (`uv tool install rentctl`), make sure `~/.local/bin` is on the `PATH` your client
  starts with, and restart the session. Claude Code also warns about this at session start.
- **Codex:** `codex exec` is silent when an MCP server fails to start. The TUI shows a
  startup warning. `rent doctor` names a `[mcp_servers.rentctl]` in `config.toml` that
  shadows the plugin.
- **Claude Code:** a project `.mcp.json` entry that runs the same `rent-mcp` replaces the
  plugin's server (tool names then start `mcp__rentctl__` instead of
  `mcp__plugin_rentctl_rentctl__`). That is harmless; the skill uses either.

Never let the agent start the dev server by hand as a workaround: a server started outside
rentctl has no lease and nothing will ever stop it.

## `NOT_A_PROJECT` / `UNKNOWN_PROJECT`

The checkout has no `rentctl.toml`, or declares a project that is not enrolled on this
machine. Enrolling is yours to do: add the `rentctl.toml` and run `rent init` in the project
root. It shows the command and asks for your approval.

## `CMD_CHANGED`

`rentctl.toml` changed since you approved it. Nothing runs until you re-approve:

```
rent sync
```

An agent must never do this for you, and must never use `rent init --trust-repo`.

## `START_TIMEOUT` and other start failures

The result includes `log_tail` — the server's own output. Fix what it says, then retry
once. Full logs: `~/.local/state/devctl/logs/`. History: `rent events --project NAME --since 1h`.

`readiness: listening` means the server started but bound an address other than loopback,
so `http://localhost:<port>` will not reach it — fix the app's bind address.

## `UNSUPPORTED_ENVIRONMENT`

rentctl was run somewhere it cannot see processes — usually a sandboxed agent shell (Codex).
It refuses instead of acting blind. Use the MCP tools, which run outside the sandbox.

## The environment did not stop when the session ended

- **Other sessions still hold it.** Sessions in one checkout share a server; it stops when
  the last one releases. `rent ls` shows the remaining `claims`.
- **Codex hooks not trusted.** Run `/hooks` in Codex and trust rentctl's hooks. `rent doctor`
  shows `codex:hooks`.
- **The client was killed.** No client runs its session-end hook when it is killed; the
  server stops when the lease expires (default 120 minutes).
- **Stop still in progress.** `stopping` / `pending: true` in `rent ls` — look again in a few
  seconds. `cleanup_incomplete` with `survivors` names processes that would not die.

To stop it for everyone now: `rent down <project>` (a deliberate stop that names the
claims it overrode).

## Hooks are installed twice

If you enrolled a project with `rent init` before installing the plugin, its hooks are
also in the project's `.claude/settings.local.json`, and Claude Code runs both copies.
That is harmless — the second session-end release finds nothing left to release — but
`rent doctor` reports it, and you can delete the two rentctl entries (the ones running
`rent sweep` and `rent down … --reason session-end`) from that file. A `rent init` run
while the plugin is installed does not write them again.
