# Contributing

## Running the suite

The suite runs under [uv](https://docs.astral.sh/uv/), exactly as CI runs it:

```
uv run --extra dev pytest -q -rs --cov --cov-fail-under=90
```

- **`-rs`** prints every skipped test with its reason. A skip looks like a pass in the
  default output; read the list.
- **`--cov --cov-fail-under=90`** is the release gate: coverage is measured over
  `rentctl.core` only (`[tool.coverage.run]` in `pyproject.toml`), and it must stay at
  or above 90%. The CLI, MCP server and runners are thin shells over `core/` and are
  covered by integration tests rather than by this number.
- CI runs the same command on Linux and macOS, Python 3.12 and 3.13. Some tests only run
  on one platform (the Linux port probe cannot run unprivileged on macOS); `-rs` shows
  which.

## Tests must use isolated state

rentctl's state is real: a registry of enrolled projects, live leases, and processes it
will kill. A test that touches `~/.config/devctl` or `~/.local/state/devctl` can tear down
a developer's running servers.

- Use the `devctl_home` fixture from `tests/conftest.py` (or `write_registry`, which
  builds on it). It points `RENTCTL_CONFIG_HOME` and `RENTCTL_STATE_HOME` at a temporary
  directory and clears the legacy and XDG overrides.
- Prefer `FakeRunner` and the injectable clock over spawning real servers. Where a test
  must spawn a real process, it must stop it, and must take its port from the OS (bind
  to port 0, as `tests/test_integration.py` does) — never a port in the 5100–5999 range
  rentctl allocates from, where a developer's real servers live.
- To try the CLI by hand without touching your own setup, set both variables:

  ```
  RENTCTL_STATE_HOME=/tmp/rs RENTCTL_CONFIG_HOME=/tmp/rc uv run rent ls
  ```

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`,
for example `fix(core): refuse an empty --cwd` or `docs: explain strict enforcement`.
Common types are `feat`, `fix`, `docs`, `test`, `refactor` and `chore`. The body says
*why* — the diff already says what.

## Generated files

`plugin/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` and `server.json`
are rendered from `src/rentctl/core/wiring.py`. Edit the generator, re-render, and let
`tests/test_wiring.py` confirm they agree — see [plugin/README.md](plugin/README.md).

## Security issues

Not here — see [SECURITY.md](SECURITY.md).
