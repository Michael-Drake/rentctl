# rentctl for Claude Code

Five minutes from nothing to a leased dev server that dies with your session.

## What you install, and where

| piece | supplies | where it must live |
|---|---|---|
| the Python package (`uv tool install rentctl`) | `rent` and `rent-mcp`: the engine that starts, supervises, expires and stops servers | the machine that has your repo and runs the dev server |
| the Claude Code plugin (`rentctl@rentctl`) | the MCP registration, the SessionStart/SessionEnd hooks, and the `dev-environment` skill | your Claude Code install on that same machine |
| enrollment (`rent init`, once per project) | your approval of the project's dev command, and its port block | the same machine |

The plugin does not download rentctl for you. It runs the `rent` you installed, and it
tells you at session start when that is missing.

Supported surfaces: the Claude Code CLI, IDE extensions and desktop app on the machine
that holds the repo (they run plugin hooks and MCP servers locally). **Not supported:**
cloud sessions on claude.ai/code — they do not install your plugins and cannot reach your
machine's processes.

## Install

```
uv tool install rentctl
claude plugin marketplace add Michael-Drake/rentctl
claude plugin install rentctl@rentctl
```

Restart Claude Code, then check:

```
rent doctor
claude plugin details rentctl     # Hooks: SessionStart, SessionEnd · Skills: dev-environment
claude mcp list                    # plugin:rentctl:rentctl … ✔ Connected
```

## Enroll your project (once)

Your repo describes its dev server in a `rentctl.toml`:

```toml
[project]
name = "myapp"
runner = "process"

[profiles.default]
cmd = 'npm run dev -- --port "$PORT" --strictPort'
cwd = "."
port_env = "PORT"
```

Then, in the project root:

```
rent init
```

It prints the exact command and asks you to approve it. Only the approved command ever
runs. If the plugin is installed, `init` does not write a second copy of the hooks.

## First run

Open Claude Code in the project and ask:

> Run this app.

The `dev-environment` skill starts the environment through rentctl and reports the URL
rentctl drew for it — not a guessed port. Other things it handles:

- "Show me my changes" — starts or reuses the environment and gives you the URL.
- "Why won't my dev server start?" — reads the server log and rentctl's diagnosis first.
- "Stop the environment I'm using" — releases *your* session's hold; if another session in
  the same checkout still uses it, it keeps running for them.

You can also invoke it directly: `/rentctl:dev-environment`.

## A copyable example with verified cleanup

In a terminal (a person, not the agent):

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
rent init                 # approve the command
claude -p "Run this app and tell me its URL."
```

When `claude -p` exits, its SessionEnd hook releases the session's claim and the server
is stopped. Verify:

```
rent ls                   # no environment for "hello"
rent events --project hello --since 10m   # an `up`, then a `down` with reason session-end
curl -sS "http://localhost:<port>/" || echo "stopped"
```

## What happens when

| event | what rentctl does |
|---|---|
| session ends normally | SessionEnd releases this session's claim; the server stops when no other session holds one |
| two sessions in one checkout | they share one server; each holds its own claim |
| separate worktrees | separate servers and ports; ending one never touches another |
| Claude Code is killed (no SessionEnd) | the claim expires with the lease (120 min default); the supervisor stops the server then |
| you run `rent down <project>` in a terminal | a deliberate stop for everyone, naming any claims it overrode |

## Troubleshooting

See [troubleshooting.md](troubleshooting.md). The first command is always `rent doctor`.
