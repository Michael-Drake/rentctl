# Integration matrix — rentctl 1.2.0

What was actually exercised in each agent client, on which versions, and what was not.
Every "tested" cell comes from a live run against the built 1.2.0 wheel and the staged
release tree (the same files this repository ships), on macOS, with isolated rentctl state
and a disposable project. Anything not run live is marked **unverified**, with the check
that remains.

## Capabilities

| | Claude Code | Codex |
|---|---|---|
| **Tested client version** | 2.1.284 (CLI: `claude -p`) | codex-cli 0.153.4 (CLI: `codex exec`, TUI) |
| **Install** | `claude plugin marketplace add` + `plugin install` — tested (local marketplace) | `codex plugin marketplace add` + `plugin add` — tested (local marketplace) |
| **MCP tools** (`env_up`, `env_down`, `env_ls`, `env_sweep`) | tested; server runs on your machine with your session's id | tested; server runs outside Codex's sandbox; session id taken per call from Codex's thread id |
| **Skill** `dev-environment` | tested: auto-activates on "run this app" / "show me my changes in a browser"; explicit `/rentctl:dev-environment` | tested: auto-activates on "run this app" / "see my changes in the browser"; explicit `$rentctl:dev-environment`. A bare "show me my changes" was read as a git diff |
| **Session-end cleanup** | tested: SessionEnd releases the session's claim; server stopped within seconds | tested (**after you trust the hooks** with `/hooks`): same; server stopped within a second |
| **Shared checkout** | tested: a second session shares the server; ending it leaves the first session's server running | tested: same |
| **Separate worktrees** | tested: separate ports, independent teardown | tested: same |
| **Client killed (no SessionEnd)** | tested: server kept until lease expiry, then stopped by its supervisor (56 s after the kill with a 1-minute lease) | tested: same (51 s after the kill with a 1-minute lease) |
| **Changed command** (`CMD_CHANGED`) | tested: agent tells you to run `rent sync`, runs nothing itself | tested: same |
| **Startup failure** | tested: agent reads the server log and does not loop | tested: same |
| **Agent's own shell** | may run `rent` (no sandbox by default) | sandboxed: `rent up` refuses with `UNSUPPORTED_ENVIRONMENT`, nothing starts — tested |
| **Missing `rent`** | tested: session-start warning; no tools; agent does not start the server by hand | tested: TUI shows the MCP failure and the hook warning; `codex exec` is silent about it; agent does not start the server by hand |
| **Diagnostics** (`rent doctor`) | session identity; installed plugin's hooks vs this rentctl | + plugin enabled, hooks trusted, config shadowing, installed plugin's hooks |
| **Upgrade 1.1.0 → 1.2.0** | tested from a local marketplace: unrelated settings kept, one MCP server, one hook of each kind | tested from a local marketplace: same; you must re-trust the hooks once (1.1.0's were invisible to Codex) |

## Surfaces

| surface | Claude Code | Codex |
|---|---|---|
| CLI | tested | tested (`codex exec`, TUI) |
| IDE extension | expected to work (runs the local CLI); **unverified** | not supported — the IDE extension loads no plugins |
| Desktop app | expected to work locally; **unverified** | expected to work locally; **unverified** |
| Cloud (claude.ai/code, Codex Cloud) | not supported — no access to your machine's processes | not supported — same |

## Still unverified, and the check that remains

- **Upgrading from the public GitHub marketplace** (the path most users take), in both
  clients. Local marketplaces were tested; the remote path needs the published release.
- **Codex:** SessionEnd on Ctrl-C or `SIGTERM` of the TUI; `codex resume`; versions newer
  than 0.153.4.
- **Claude Code:** the MCP server's session id after `/clear` or `--resume` (documented to
  keep its startup id; with shared claims the effect is a server kept until expiry, never
  one stopped early).
- The IDE and desktop surfaces listed above.

## Limits that are by design

- A killed client never runs its session-end hook, in either client. The lease expiry is
  the guarantee there (default 120 minutes).
- Codex runs no plugin hook until you trust it. Until then, cleanup is by expiry.
- The skill is guidance. Cleanup is enforced by rentctl's supervisor and hooks, not by the
  model remembering to stop anything.
