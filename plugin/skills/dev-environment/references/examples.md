# Examples

Values below are illustrative. Always use what your own calls return, especially `url`
and `port`.

## Start, use, stop (MCP)

**1. Check for an existing environment.**

`env_ls` with no arguments:

```json
{"ok": true, "environments": []}
```

Nothing is running for this checkout, so start one.

**2. Start it.**

`env_up` with no `project`. It resolves from the checkout's `rentctl.toml`:

```json
{
  "ok": true,
  "project": "shop",
  "profile": "default",
  "url": "http://localhost:41012",
  "port": 41012,
  "pid": 70311,
  "lease_expires": "2026-09-29T17:04:00-05:00",
  "already_running": false,
  "state": "running",
  "supervisor_pid": 70309,
  "claims": [
    {"session": "a1b2c3", "via": "mcp", "since": "2026-09-29T15:04:00-05:00",
     "expires": "2026-09-29T17:04:00-05:00"}
  ],
  "serving": "/home/dev/src/shop",
  "readiness": "answered"
}
```

Tell the user: "The dev server is at http://localhost:41012, leased until 17:04." Use
`41012`, not a port you expected.

**3. Use it.** Fetch `http://localhost:41012` if your client allows it, to confirm the app
responds and not just the socket. Calling `env_up` again later renews the lease and
returns `"already_running": true` and `"readiness": "not_probed"`.

**4. Stop it** when the user is done. Call `env_down` with no arguments:

```json
{
  "ok": true,
  "cwd": "/home/dev/src/shop",
  "downed": [
    {"project": "shop", "cwd": "/home/dev/src/shop", "port": 41012,
     "was_running": true, "stopped": true}
  ]
}
```

If another session in this checkout still holds it, the row says so instead. Tell the
user it keeps running for that session:

```json
{"project": "shop", "cwd": "/home/dev/src/shop", "port": 41012, "was_running": true,
 "stopped": false, "released": true, "held_by": ["d4e5f6"],
 "detail": "released this session's claim; the environment keeps running for d4e5f6"}
```

Only if the user asks to stop it for everyone, call `env_down` with `force=true` and
report `overrode_claims`.

## A failed start

```json
{
  "ok": false,
  "error": "START_TIMEOUT",
  "message": "'shop' did not reach running within 65s; its supervisor was asked to stop it",
  "port": 41012,
  "log_tail": ["Error: Cannot find module 'vite'", "..."]
}
```

Read `log_tail`: here the fix is installing dependencies. Fix it, then call `env_up` once
more. Do not retry without a change.

## The same flow for a human (CLI)

Run these in an ordinary terminal, not inside an agent's sandboxed shell:

```sh
rent ls              # what is running on this machine
rent up              # start or renew this checkout's environment; prints url and port
rent down shop       # stop this checkout's instance of "shop"
```

`rent down` typed by a person always stops, even if other sessions hold claims, and lists
them in `overrode_claims`.

## Verified cleanup

After a stop, confirm both:

1. `env_ls` (or `rent ls`) shows no row for the project in this checkout. A row with
   `"pending": true` or state `stopping` means it is still finishing, so look again. A row
   in `cleanup_incomplete` lists `survivors`: report those PIDs.
2. The port no longer answers. For example, `curl -sS -o /dev/null http://localhost:41012`
   fails to connect. (In a sandboxed shell localhost is blocked anyway, so rely on
   `env_ls`.)
