# rentctl for Codex

The same rentctl, the same plugin directory, the same skill — installed into Codex.

## What you install, and where

| piece | supplies | where it must live |
|---|---|---|
| the Python package (`uv tool install rentctl`) | `rent` and `rent-mcp` | the machine that has your repo and runs the dev server |
| the Codex plugin (`rentctl@rentctl`) | the MCP registration, the SessionStart/SessionEnd hooks, and the `dev-environment` skill | Codex on that same machine |
| enrollment (`rent init`, once per project) | your approval of the project's dev command, and its port block | the same machine |

**Where it works:** the Codex CLI (`codex`, `codex exec`) on the machine that holds the repo
— it runs plugin MCP servers and hooks locally. The Codex desktop app runs locally too and
should behave the same, but it has not been tested with rentctl. **Not
supported:** Codex Cloud (tasks run in OpenAI's containers, not on your machine), and the
IDE extension, which does not load plugins.

**Why the sandbox is fine.** Codex runs the commands the model types in a sandbox that
cannot see other processes or open local ports. rentctl's MCP server and hooks run outside
that sandbox, as you. So the agent uses rentctl's MCP tools; a `rent up` typed into the
sandboxed shell refuses with `UNSUPPORTED_ENVIRONMENT` instead of acting blind.

## Install

```
uv tool install rentctl
codex plugin marketplace add Michael-Drake/rentctl
codex plugin add rentctl@rentctl
```

## Trust the hooks (once)

Codex does not run a plugin's hooks until you trust them. Start `codex`, type `/hooks`, and
trust rentctl's `SessionStart` and `SessionEnd` hooks. Until you do, rentctl still works,
but a session's environment is not released when the session ends — it is stopped when
its lease expires (120 minutes by default).

Check everything with:

```
rent doctor
```

Under `codex:plugin`, `codex:hooks` and `codex:mcp-shadow` it reports whether the plugin is
enabled, whether its hooks are trusted, and whether a hand-written `[mcp_servers.rentctl]`
in `~/.codex/config.toml` is shadowing the plugin's server (Codex silently prefers the
config entry — remove it if you added one by hand).

## Enroll your project (once)

Add a `rentctl.toml` to the repo and run `rent init` in a terminal, exactly as in the
[Claude Code guide](claude-code.md#enroll-your-project-once). Enrolling is your decision:
the skill never runs `rent init` for you.

## First run

In the project:

```
codex "Run this app."
```

or explicitly: `$rentctl:dev-environment start the dev server`. The skill calls rentctl's
`env_up` tool, and reports the URL and readiness rentctl returned.

`codex exec` works too: rentctl's tools declare that they are not destructive (except
`env_sweep`), so Codex calls them without an approval prompt.

## A copyable example with verified cleanup

Use the `hello` project from the [Claude Code guide](claude-code.md#a-copyable-example-with-verified-cleanup), then:

```
codex exec "Run this app and tell me its URL."
rent ls                                   # nothing left for "hello" (hooks trusted)
rent events --project hello --since 10m   # an `up`, then a `down` with reason session-end
```

## What happens when

| event | what rentctl does |
|---|---|
| session ends normally (`/exit`, `codex exec` finishing) | SessionEnd releases this session's claim — **if the hooks are trusted** |
| two sessions in one checkout | they share one server; each holds its own claim (keyed by Codex's thread id) |
| Codex is killed (no SessionEnd) | the claim expires with the lease; the supervisor stops the server then |
| hooks not trusted | nothing fires at session end; expiry stops the server |

## Troubleshooting

See [troubleshooting.md](troubleshooting.md). Codex-specific: `codex exec` does not report
an MCP server that failed to start — if the agent says it has no rentctl tools, run
`rent doctor` and check that `rent-mcp` is on your `PATH`.
