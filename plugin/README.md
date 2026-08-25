# rentctl — Claude Code plugin

Installs both halves of rentctl into a Claude Code session:

- the **MCP server**, so an agent has `env_up` / `env_down` / `env_ls` / `env_sweep`; and
- the **session hooks**, so cleanup happens whether or not the agent cooperates.

The second half is why this channel exists. An MCP server alone is half the product —
it can start environments and cannot guarantee they die. A PyPI-only install hands a
stranger an MCP server they must hand-wire hooks around, which restates the problem
[ADR-0002](../adr/0002-devctl-is-standalone-self-service-enrollment.md) exists to remove.

## `.claude-plugin/plugin.json` is generated — do not edit it

It is rendered from [`core/wiring.py`](../src/devctl/core/wiring.py), the same module
`rent init` writes its hooks from. Two renders of one source are identical by
construction; two hand-kept copies drift, and a drifted plugin would ship a *different
cleanup contract* under the same name as the CLI.

Regenerate:

```python
from pathlib import Path
from devctl.core import wiring
Path("plugin/.claude-plugin/plugin.json").write_text(wiring.render_plugin_manifest())
```

`tests/test_wiring.py` fails if the checked-in file and the render disagree, so drift is
caught by the suite rather than noticed in the field.

## Prerequisite

`rent-mcp` and `rent` must be on `PATH` — the manifest invokes them by name rather than by
a plugin-relative path, because they are installed with the Python distribution, not
vendored here:

```
uv tool install rentctl        # or: pipx install rentctl
```

A plugin whose commands do not resolve fails silently in exactly the way this tool exists
to prevent, so install the distribution first.

## Status: not yet verified against a live install

The manifest is generated, schema-conformant as documented, and covered by tests. **It has
not been installed into a running Claude Code and observed to load** — that is the
remaining step for [WI-0008](../work-items/), and until it is done this directory should be
treated as built-but-unproven rather than shipped. Marking it otherwise would be the same
"reported success it could not deliver" failure the tool spent a session removing.
