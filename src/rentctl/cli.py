# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""``rent init|sync|up|down|ls|sweep|events`` — the CLI shell (spec §3.2, ADR-0002).

A thin argument-parser over :class:`rentctl.core.service.Service` and
:mod:`rentctl.core.enroll`. Hooks call this (hooks can't call MCP tools): the
SessionEnd hook runs
``rent down --all --cwd "$CLAUDE_PROJECT_DIR" --reason session-end`` and the
SessionStart hook runs ``rent sweep`` (§7). Every command prints the
operation's JSON result and exits non-zero when ``ok`` is false, so a hook or
human sees structured output.

``events`` is CLI-only by design. It reads the lease event log for pilot-gate
scoring and post-hoc diagnosis — a reader for the operator, not a capability
for a session — and the spec's tool set is closed at four (§4.5, §12 O2).

``init`` is the exception to the JSON-only rule: it prints the resolved command
in plain text and waits for an explicit approval before writing anything
(ADR-0003 §1). Approval that isn't legible isn't approval.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import enroll as enroll_mod
from .core import runtimes as runtimes_mod
from .core import wiring
from .core.errors import DevctlError
from .core.events import EXPLICIT, SESSION_END, parse_since, summarize
from .core.paths import DevctlPaths
from .core.service import DEFAULT_LEASE_MINUTES, Service, _now_local


def _build_parser() -> argparse.ArgumentParser:
    # No explicit prog: argparse falls back to basename(sys.argv[0]), so `--help`
    # names whichever alias the user actually typed. Hardcoding either `rent` or
    # `devctl` would print the wrong one for half of callers while both ship
    # (ADR-0009 §4), and this needs no second edit when the aliases are dropped.
    parser = argparse.ArgumentParser(
        description="leased dev environments — a dev server never outlives the work that needed it"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="start (or renew) a project's environment")
    up.add_argument("project", help="registry project key")
    up.add_argument("--lease-minutes", type=int, default=DEFAULT_LEASE_MINUTES)
    up.add_argument("--profile", default="default")
    up.add_argument("--cwd", default=None, help="caller project dir recorded on the lease")

    down = sub.add_parser("down", help="stop a project, or all leased to --cwd")
    down.add_argument("project", nargs="?", default=None)
    down.add_argument("--all", action="store_true", help="down everything leased to --cwd")
    down.add_argument("--cwd", default=None)
    # Naming a project downs *this* cwd's instance. Reaching every worktree's copy
    # is opt-in, so an ordinary teardown can't kill a sibling lane's server.
    down.add_argument(
        "--all-instances",
        action="store_true",
        help="down every worktree's instance of the named project, not just this cwd's",
    )
    # Only the two caller-owned reasons are accepted. Expiry and sweep reasons are
    # produced by the watchdog and the reconciler, so no CLI caller can forge
    # evidence that cleanup layer 3 or 4 fired.
    down.add_argument(
        "--reason",
        choices=[EXPLICIT, SESSION_END],
        default=None,
        help="declare which cleanup layer this is (the SessionEnd hook passes session-end)",
    )

    sub.add_parser("ls", help="list every broker-owned environment on the machine")
    sub.add_parser("sweep", help="reconcile: stop expired/dead, report squatters")
    sub.add_parser(
        "doctor",
        help="liveness check: are the shims, registry and wired hooks actually working?",
    )

    # G3's only instrument (spec §11.1). devctl cannot observe whether a kill was
    # unwanted — the teardown looks identical from the inside — so the criterion
    # has to have an inbound channel or it cannot be scored at all (WI-0016).
    report = sub.add_parser(
        "report-kill", help="report that rentctl killed something you were using"
    )
    report.add_argument("project", help="the project whose environment was torn down")
    report.add_argument("--note", required=True, help="what was lost, in your own words")
    report.add_argument("--port", type=int, default=None, help="narrow the match to one port")

    events = sub.add_parser("events", help="read the append-only lease event log")
    events.add_argument("--project", default=None, help="only this project's events")
    events.add_argument("--since", default=None, help="7d / 24h / 90m, or an ISO timestamp")
    events.add_argument("--limit", type=int, default=None, help="keep only the newest N")
    events.add_argument(
        "--summary",
        action="store_true",
        help="fold into totals per cleanup layer instead of listing events",
    )

    init = sub.add_parser("init", help="enroll this project (reads ./rentctl.toml)")
    init.add_argument("--path", default=".", help="project root (default: cwd)")
    init.add_argument(
        "--trust-repo",
        action="store_true",
        help="skip the interactive approval — for CI/scripted use. Recorded in the registry.",
    )
    init.add_argument(
        "--no-hooks", action="store_true", help="register the MCP server but write no session hooks"
    )
    init.add_argument(
        "--print-hooks",
        action="store_true",
        help="print the hook fragment and exit, writing nothing (for nonstandard setups)",
    )
    init.add_argument(
        "--adopt",
        nargs="?",
        const=True,
        default=None,
        metavar="NAME",
        help="this project is already in rentctl's registry but has no rentctl.toml: "
             "write one from the existing entry (ADR-0012). Bare --adopt finds the "
             "entry by location; pass a NAME when more than one matches. The derived "
             "file is shown for approval and written only if you accept.",
    )
    init.add_argument(
        "--runtime",
        action="append",
        metavar="ID",
        help="enroll only this agent runtime (repeatable). Default: every runtime "
             "used in the project or installed on the machine. Known ids: "
             + ", ".join(sorted(runtimes_mod.BY_ID)),
    )

    sync = sub.add_parser("sync", help="re-approve a changed rentctl.toml and re-pin it")
    sync.add_argument("--path", default=".", help="project root (default: cwd)")
    return parser


def _confirm(plan: "enroll_mod.EnrollPlan") -> bool:
    """Show the exact resolved command and require an explicit yes (ADR-0003 §1).

    Prints to stderr so the command's JSON result on stdout stays machine-readable.
    Not a default-yes prompt: anything other than an explicit ``y``/``yes`` is a
    decline, including EOF, so a non-interactive run without ``--trust-repo``
    refuses rather than silently approving.
    """
    for line in plan.summary_lines():
        print(line, file=sys.stderr)
    print(file=sys.stderr)
    try:
        answer = input("Approve and enroll? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def main(argv: list[str] | None = None, service: Service | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd in ("init", "sync"):
        return _enrollment_command(args)

    # Before `Service()`, deliberately. `doctor` is the check you run when the
    # machine is broken, so it must not require a working Service to report that
    # — a diagnostic that dies on the fault it diagnoses is the nine-day outage
    # again (WI-0051).
    if args.cmd == "doctor":
        return _doctor_command()

    svc = service or Service()

    if args.cmd == "up":
        result = svc.env_up(args.project, args.lease_minutes, args.profile, cwd=args.cwd)
    elif args.cmd == "down":
        if args.project is not None and not args.all:
            result = svc.env_down(
                project=args.project,
                cwd=args.cwd,
                reason=args.reason,
                all_instances=args.all_instances,
            )
        else:
            result = svc.env_down(cwd=args.cwd, reason=args.reason)
    elif args.cmd == "ls":
        result = svc.env_ls()
    elif args.cmd == "events":
        result = _events(svc, args)
    elif args.cmd == "report-kill":
        result = svc.report_false_kill(args.project, note=args.note, port=args.port)
    else:  # sweep
        result = svc.env_sweep()

    # A human reads this. `rent sweep` on a bad registry was printing
    # "registry invalid — squatter detection skipped" to a terminal.
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def _doctor_command() -> int:
    """``rent doctor`` — is rentctl alive, and are its capabilities real?

    Note the other spelling: ``python3 -m rentctl.doctor`` runs the same check
    without the console shim. That path is not a convenience — a self-check
    reached *through* the shim cannot report that the shim is missing, which is
    exactly how the fleet's hooks failed silently for nine days.
    """
    from .core.doctor import diagnose

    report = diagnose()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return report.exit_code


def _events(svc: Service, args: argparse.Namespace) -> dict:
    try:
        since = parse_since(args.since, _now_local()) if args.since else None
    except ValueError as e:
        return {"ok": False, "error": "BAD_SINCE", "message": str(e)}
    found = svc.events.read(project=args.project, since=since, limit=args.limit)
    if args.summary:
        return {"ok": True, "summary": summarize(found)}
    return {"ok": True, "count": len(found), "events": found}


def _enrollment_command(args: argparse.Namespace) -> int:
    """``init`` / ``sync`` — the enrollment half (ADR-0002, ADR-0003).

    Errors render as the same ``{"ok": false, ...}`` envelope every other command
    uses, so a ``CMD_CHANGED`` refusal is as machine-readable as it is legible.
    """
    if args.cmd == "init" and args.print_hooks:
        # Exactly one runtime's fragment, because the point of this flag is that
        # a consumer can route the bytes straight into a settings file. The
        # shapes genuinely differ (each runtime's SessionEnd names a different
        # project-dir variable), so `--runtime` selects; the default stays the
        # reference runtime, which is what this flag has always printed.
        binding = runtimes_mod.BY_ID.get(
            (args.runtime or [runtimes_mod.CLAUDE_CODE.id])[0]
        )
        if binding is None:
            print(
                f"unknown runtime {args.runtime[0]!r} — known: "
                f"{', '.join(sorted(runtimes_mod.BY_ID))}",
                file=sys.stderr,
            )
            return 2
        # Destination to stderr so stdout stays pure JSON.
        print(f"# {binding.label} → {'/'.join(binding.hooks_file)}", file=sys.stderr)
        print(wiring.render_hooks_text(binding))
        return 0

    paths = DevctlPaths.default()
    root = Path(args.path).resolve()
    try:
        if args.cmd == "init":
            result = enroll_mod.enroll(
                root,
                paths,
                approve=_confirm,
                unattended=args.trust_repo,
                write_hooks=not args.no_hooks,
                runtimes=tuple(args.runtime) if args.runtime else None,
                adopt=args.adopt if args.adopt is not None else False,
            )
        else:
            result = enroll_mod.sync(root, paths, approve=_confirm)
    except DevctlError as e:
        # The refusal path, and the one moment a user is certainly reading:
        # every enrollment error message is authored with an em-dash, so the
        # sentence explaining *why* was the part being mangled.
        print(json.dumps(e.to_envelope(), indent=2, ensure_ascii=False))
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
