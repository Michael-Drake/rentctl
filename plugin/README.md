# rentctl — plugin for Claude Code and Codex

One plugin directory, installable in both clients from the same marketplace. It
installs both halves of rentctl:

- the **MCP server**, so an agent has `env_up` / `env_down` / `env_ls` / `env_sweep`; and
- the **session hooks**, so cleanup happens whether or not the agent cooperates —
  `rent sweep` at session start, `rent down --all --cwd "${CLAUDE_PROJECT_DIR:-$(pwd -P)}"
  --reason session-end` at session end. The session-end hook asks each environment's
  supervisor to stop and returns at once, inside the client's short hook budget; the
  supervisors finish the cleanup after the session has gone.

In Codex, hooks do not run until you trust them once (`/hooks`); until then cleanup
falls back to lease expiry. `rent doctor` tells you which state you are in.

## Quickstart

```
claude plugin marketplace add Michael-Drake/rentctl
claude plugin install rentctl@rentctl
uv tool install rentctl        # or: pipx install rentctl — the plugin needs this
cd your-project && rent init   # approve the command; claims the project's ports
rent doctor                    # confirm the shim, registry and wiring are live
```

**Step 3 is not optional.** The plugin invokes `rent` and `rent-mcp` by name from your
`PATH`; they come from the Python package, not from this plugin, and the plugin does not
download its own. Without them session-end cleanup is off — and you are told: every
session starts with a warning that says so and names `uv tool install rentctl`, and the
session-end hook exits with the same message instead of failing silently.

`rent init` still matters with the plugin installed: it approves the project's command
and claims its port block. It detects the plugin and skips writing a second copy of the
hooks. See the [main README](../README.md) for `rentctl.toml`, the cleanup layers, and
the known limitations.

## Why a plugin at all

An MCP server alone is half the product: it can start environments and cannot guarantee
they die. A PyPI-only install hands you an MCP server you must hand-wire hooks around.
The plugin is the one channel that installs the hooks too.

## For maintainers

### Layout

```
.claude-plugin/marketplace.json      # generated — the one marketplace; both clients add the repo root
plugin/
  .claude-plugin/plugin.json         # generated — Claude Code manifest (MCP inline, NO hooks key)
  .codex-plugin/plugin.json          # generated — Codex manifest (MCP inline, skills, interface)
  hooks/hooks.json                   # generated — the ONE hook file, wrapped {"hooks": {...}}
  README.md
```

The hooks live **only** in `hooks/hooks.json`. Claude Code merges inline manifest hooks
with that file (so both would fire every hook twice); Codex ignores inline hooks
entirely (1.1.0 installed into Codex with no cleanup). Neither manifest may carry a
`hooks` key, and the suite fails if one does.

### The four generated files — do not edit them

All four are rendered from [`core/wiring.py`](../src/rentctl/core/wiring.py), the same
module `rent init` writes its hooks from. Two renders of one source are identical by
construction; two hand-kept copies drift, and a drifted plugin would ship a *different
cleanup contract* under the same name as the CLI. Regenerate them all:

```python
from pathlib import Path
from rentctl.core import wiring
wiring.write_generated_files(Path("."))   # run from the repo root
```

`tests/test_wiring.py` fails if any file in `wiring.GENERATED_FILES` and its render
disagree, so drift is caught by the suite rather than noticed in the field. **Both
manifests carry the package version**, so a release bump makes them stale and the suite
red until they are re-rendered — that is deliberate, and the command above is the fix.

### Validate before any release that touches this directory

These are the only checks that speak for the consumers. Always `--strict`: without it
the 1.0.0 defect was only a warning.

```
claude plugin validate --strict plugin
claude plugin validate --strict .
uv run --no-project --with pyyaml python ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugin
```

The third is the validator bundled with Codex (there is no `codex plugin validate`); it
reads `.codex-plugin/plugin.json` only, requires strict semver and the `interface`
block, and rejects a `hooks` key. Skills are checked by the bundled
`~/.codex/skills/.system/skill-creator/scripts/quick_validate.py <skill-dir>`.

### History: verified against a live install, 2026-09-02

Installed into a running Claude Code and observed. `claude plugin list` reports it
enabled at version 1.0.0; `claude mcp list` reports
`plugin:rentctl:rentctl: rent-mcp — ✔ Connected`; `claude plugin details rentctl` reports
`Hooks (2) SessionEnd, SessionStart`.

**The install is what found the defect.** Until it was run, the manifest nested its hooks
one level too deep — the settings-file envelope, `{"hooks": {"SessionEnd": …}}`, instead
of the event map itself. `claude plugin validate` names it exactly:
`hooks.hooks: unknown hook event; entry ignored at runtime`. Version 1.0.0 shipped that
way, so the plugin installed **no cleanup at all** while listing as installed. The two
shapes differ by one level of nesting; tests, review and a documented schema all passed
over it, because every one of them was checking that the file said what we meant rather
than that Claude Code agreed.

`claude plugin validate .` (marketplace) and `claude plugin validate ./plugin` both pass
clean. Run them before any release that touches this directory — they are cheap and they
are the only check that speaks for the consumer.
