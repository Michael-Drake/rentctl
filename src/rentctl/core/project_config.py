# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""``devctl.toml`` — the per-project profile source (ADR-0002 §3).

This is the **untrusted-input boundary**. Under ADR-0001 every command string was
authored by a coordinator and passed through a human; under self-service
enrollment a repo declares its own ``cmd`` and rentctl turns it into a process.
Two consequences shape this module:

* **Validation is adversarial, not just schema-shaped.** A project name becomes a
  lease *filename* and a ``cwd`` becomes a working directory, so traversal in
  either is rejected at the door rather than sanitised downstream.
* **There is no field in which to write a port.** ADR-0002 §6 says numbers are
  drawn, never picked, and therefore never written down. A schema that merely
  *lacked* a port field would let a repo write one and believe it took effect;
  this one **rejects** it, so the promise is structural.

The file is authored in the repo and is machine-independent: ``cwd`` is
repo-relative and resolved to an absolute path at claim time by :meth:`resolve`,
which is what keeps machine paths out of tracked files.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PROJECT_CONFIG_INVALID, UNKNOWN_PROFILE, DevctlError

# The file a project writes by hand to describe itself — the most user-facing
# name in the product after the command (ADR-0009, WI-0024). Renamed before the
# first public release deliberately: ship it as `devctl.toml` and every future
# rentctl user creates a file named after the old name forever, plus a migration
# rentctl owns permanently. Strictly cheaper now than after.
TOML_NAME = "rentctl.toml"

# Accepted when the current name is absent, never written. Same resolution shape
# as the state directory: enrolled projects keep working untouched, and nobody is
# asked to rename a file to stay enrolled. A project that renames it gets the new
# name honoured on the next run with no further step.
LEGACY_TOML_NAME = "devctl.toml"
BLOCK_SIZE = 10
DEFAULT_PROFILE = "default"
VALID_RUNNERS = ("process", "compose")

# Lowercase, starts alphanumeric, then alphanumerics/dot/dash/underscore. No
# separators, no "..", no whitespace, no shell metacharacters — the name is used
# as a filename and printed into human-facing output.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Keys that must never appear. Ports are drawn (ADR-0002 §6); "offset" is the
# retired ADR-0001 spelling and would silently read as an assignment rather than
# the preference ADR-0004 made it.
FORBIDDEN_PROJECT_KEYS = ("block", "port", "ports")
FORBIDDEN_PROFILE_KEYS = ("port", "ports", "block", "offset")

_PROJECT_KEYS = {"name", "runner"}
_PROFILE_KEYS = {"cmd", "cwd", "port_env", "preferred_offset"}


def _bad(msg: str) -> DevctlError:
    return DevctlError(PROJECT_CONFIG_INVALID, msg)


@dataclass(frozen=True)
class ProjectProfile:
    """One runnable variant, as authored. ``cwd`` is repo-relative."""

    cmd: str
    cwd: str
    port_env: str
    preferred_offset: int


@dataclass(frozen=True)
class ResolvedProfile:
    """A profile with ``cwd`` resolved against this machine's checkout.

    ``source_cwd`` keeps the repo-relative form so output can show what the repo
    actually declared, not only where it landed here.
    """

    cmd: str
    cwd: str          # absolute, this machine
    source_cwd: str   # repo-relative, as authored
    port_env: str
    preferred_offset: int


@dataclass(frozen=True)
class ProjectConfig:
    """A parsed, validated ``devctl.toml``."""

    name: str
    runner: str
    profiles: dict[str, ProjectProfile]

    # --- loading ----------------------------------------------------------

    @staticmethod
    def path_in(repo_root: Path) -> Path:
        """The project's config file: the current name, or the legacy one if that
        is what is actually there.

        Resolution, not migration — nothing is moved or rewritten. A project
        enrolled before the rename keeps working with no action from anyone, and
        one that renames its file is honoured on the next run with no further
        step. Returns the **current** name when neither exists, so a caller
        creating a file creates the right one.
        """
        current = Path(repo_root) / TOML_NAME
        if current.is_file():
            return current
        legacy = Path(repo_root) / LEGACY_TOML_NAME
        if legacy.is_file():
            return legacy
        return current

    @staticmethod
    def new_path_in(repo_root: Path) -> Path:
        """Where a config rentctl *writes* goes — always the current name.

        Distinct from :meth:`path_in` on purpose. Adoption must not create a file
        under the legacy name just because :meth:`path_in` would resolve there;
        that would mint new instances of the name being retired.
        """
        return Path(repo_root) / TOML_NAME

    @classmethod
    def exists_in(cls, repo_root: Path) -> bool:
        return cls.path_in(repo_root).is_file()

    @classmethod
    def load(cls, repo_root: Path) -> "ProjectConfig":
        path = cls.path_in(repo_root)
        if not path.is_file():
            # Never "run `rent init`" — this is raised BY `rent init`, and
            # telling someone to run the command that just failed is a dead end
            # for the one user who most needs the answer: a stranger enrolling a
            # project for the first time. Say what to write instead.
            raise _bad(
                f"no {TOML_NAME} in {repo_root}\n\n"
                f"A project describes itself in a {TOML_NAME} at its repo root. "
                "Create one like:\n\n"
                "  [project]\n"
                "  name = \"myapp\"          # lowercase; it is used as a filename\n"
                "  runner = \"process\"\n\n"
                "  [profiles.default]\n"
                "  cmd = 'npm run dev -- --port \"$PORT\" --strictPort'\n"
                "  cwd = \".\"                # repo-relative, never absolute\n"
                "  port_env = \"PORT\"        # rentctl sets this in the child\n\n"
                "then run `rent init` again. If this project is already in "
                "rentctl's registry from an earlier setup, `rent init --adopt` "
                f"writes the {TOML_NAME} from that entry for you."
            )
        try:
            raw = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
            raise _bad(f"cannot parse {path}: {e}") from e
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> "ProjectConfig":
        if not isinstance(raw, dict):
            raise _bad(f"{TOML_NAME} root must be a table")

        stray = set(raw) - {"project", "profiles"}
        if stray:
            raise _bad(f"unexpected top-level key(s) in {TOML_NAME}: {', '.join(sorted(stray))}")

        project = raw.get("project")
        if not isinstance(project, dict):
            raise _bad(f"{TOML_NAME} needs a [project] table")
        _reject_forbidden(project, FORBIDDEN_PROJECT_KEYS, "[project]")
        unknown = set(project) - _PROJECT_KEYS
        if unknown:
            raise _bad(f"[project] has unknown key(s): {', '.join(sorted(unknown))}")

        name = project.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise _bad(
                f"[project] name {name!r} must match {NAME_RE.pattern} — it is used as a "
                "filename, so separators, '..', spaces, and uppercase are refused"
            )

        runner = project.get("runner", "process")
        if runner not in VALID_RUNNERS:
            raise _bad(f"[project] runner {runner!r} not in {VALID_RUNNERS}")

        profiles_raw = raw.get("profiles")
        if not isinstance(profiles_raw, dict) or not profiles_raw:
            raise _bad(f"{TOML_NAME} needs a non-empty [profiles.<name>] table")
        if DEFAULT_PROFILE not in profiles_raw:
            raise _bad(f"{TOML_NAME} must define a [profiles.{DEFAULT_PROFILE}] table")

        profiles: dict[str, ProjectProfile] = {}
        seen_offsets: dict[int, str] = {}
        for pname, praw in profiles_raw.items():
            profile = _parse_profile(pname, praw)
            prior = seen_offsets.get(profile.preferred_offset)
            if prior is not None:
                raise _bad(
                    f"profiles {prior!r} and {pname!r} both prefer offset "
                    f"{profile.preferred_offset}"
                )
            seen_offsets[profile.preferred_offset] = pname
            profiles[pname] = profile

        if profiles[DEFAULT_PROFILE].preferred_offset != 0:
            raise _bad(f"[profiles.{DEFAULT_PROFILE}] must have preferred_offset 0")

        return cls(name=name, runner=runner, profiles=profiles)

    # --- access -----------------------------------------------------------

    def profile(self, name: str = DEFAULT_PROFILE) -> ProjectProfile:
        try:
            return self.profiles[name]
        except KeyError:
            raise DevctlError(
                UNKNOWN_PROFILE,
                f"{TOML_NAME} has no profile {name!r} (have: {', '.join(sorted(self.profiles))})",
            ) from None

    def resolve(self, repo_root: Path) -> dict[str, ResolvedProfile]:
        """Resolve every profile's ``cwd`` against this machine's checkout.

        Rejects any path that escapes ``repo_root`` after resolution — a repo may
        declare where inside itself its server runs, never somewhere else on the
        machine.
        """
        root = Path(repo_root).resolve()
        out: dict[str, ResolvedProfile] = {}
        for pname, p in self.profiles.items():
            target = (root / p.cwd).resolve()
            if not target.is_relative_to(root):
                raise _bad(
                    f"profile {pname!r} cwd {p.cwd!r} resolves outside the project "
                    f"({target}) — cwd must stay within {root}"
                )
            out[pname] = ResolvedProfile(
                cmd=p.cmd,
                cwd=str(target),
                source_cwd=p.cwd,
                port_env=p.port_env,
                preferred_offset=p.preferred_offset,
            )
        return out


def _toml_string(value: str) -> str:
    """Render a string as TOML, preferring the literal form for shell commands.

    Dev commands are full of ``$``, quotes and backslashes — a typical one is
    ``npm run dev -- --port "$PORT" --strictPort``. A TOML *literal* string
    (single quotes) passes all of that through untouched, so it is the default;
    a basic string is used only when the value itself contains a single quote,
    which a literal string cannot represent at all.
    """
    if "'" not in value:
        return f"'{value}'"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def render_toml(config: "ProjectConfig", *, header: str | None = None) -> str:
    """Render a :class:`ProjectConfig` back to ``devctl.toml`` text.

    Used by the adopt path (ADR-0012) to write a file for a project whose profile
    already lives in the registry. The output is re-parsed and compared before it
    is written, so this never silently emits something ``load()`` would reject.
    """
    lines: list[str] = []
    if header:
        lines += [f"# {line}" if line else "#" for line in header.splitlines()]
        lines.append("")
    lines += [
        "[project]",
        f"name = {_toml_string(config.name)}",
        f"runner = {_toml_string(config.runner)}",
    ]
    for pname in sorted(config.profiles):
        p = config.profiles[pname]
        lines += [
            "",
            f"[profiles.{pname}]",
            f"cmd = {_toml_string(p.cmd)}",
            f"cwd = {_toml_string(p.cwd)}",
            f"port_env = {_toml_string(p.port_env)}",
        ]
        # Only emit a non-default offset: `default` is required to be 0, so
        # writing it every time adds a line that can only ever be wrong later.
        if p.preferred_offset:
            lines.append(f"preferred_offset = {p.preferred_offset}")
    return "\n".join(lines) + "\n"


def _parse_profile(pname: str, raw: Any) -> ProjectProfile:
    if not isinstance(raw, dict):
        raise _bad(f"[profiles.{pname}] must be a table")
    _reject_forbidden(raw, FORBIDDEN_PROFILE_KEYS, f"[profiles.{pname}]")
    unknown = set(raw) - _PROFILE_KEYS
    if unknown:
        raise _bad(f"[profiles.{pname}] has unknown key(s): {', '.join(sorted(unknown))}")

    for field in ("cmd", "cwd", "port_env"):
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            raise _bad(f"[profiles.{pname}] needs a non-empty string {field!r}")

    cwd = raw["cwd"]
    if Path(cwd).is_absolute():
        raise _bad(
            f"[profiles.{pname}] cwd {cwd!r} must be repo-relative — absolute paths are "
            "machine-specific and must not enter a tracked file"
        )
    if ".." in Path(cwd).parts:
        # Syntactic rejection, independent of any root. `resolve()` also checks
        # containment after resolution, which is what catches a symlinked subdir
        # pointing outside the repo — neither check subsumes the other.
        raise _bad(
            f"[profiles.{pname}] cwd {cwd!r} may not contain '..' — a project declares "
            "where inside itself its server runs, never somewhere else on the machine"
        )

    offset = raw.get("preferred_offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or not (0 <= offset < BLOCK_SIZE):
        raise _bad(
            f"[profiles.{pname}] preferred_offset must be an integer in 0..{BLOCK_SIZE - 1}"
        )

    return ProjectProfile(
        cmd=raw["cmd"], cwd=cwd, port_env=raw["port_env"], preferred_offset=offset
    )


def _reject_forbidden(table: dict, forbidden: tuple[str, ...], where: str) -> None:
    """Refuse keys that must not exist, loudly.

    Silently ignoring a ``port`` key is worse than failing: the repo believes it
    pinned a port and rentctl draws a different one, so the two disagree with no
    signal anywhere (ADR-0002 §6).
    """
    present = [k for k in forbidden if k in table]
    if present:
        raise _bad(
            f"{where} may not set {', '.join(repr(k) for k in present)} — ports are drawn by "
            "rentctl at start time and are never written down (ADR-0002 §6); use port_env"
        )


def profile_fingerprint(runner: str, cmd: str, cwd_abs: str, port_env: str) -> str:
    """SHA-256 over the fields that determine what executes (ADR-0003 §2).

    Deliberately **not** a hash of the whole file: comments, formatting, and
    unrelated profiles would all trip ``CMD_CHANGED``, which trains users to
    re-approve reflexively and makes the alarm worthless. Only what actually
    changes execution is covered — including ``cwd``, since running the same
    command in a different directory is a different thing to run.
    """
    # DO NOT add `ensure_ascii=False` here. Everywhere else in this codebase that
    # is the correct fix, because the JSON is written to a file or shown to a
    # human. This payload is neither — it is hashed and discarded, so the
    # escaping is invisible in the output and load-bearing in the input.
    #
    # Changing it changes the digest for any project whose `cmd` or `cwd`
    # contains a non-ASCII character. The `cmd_sha256` already stored in the
    # registry would then stop matching, and `env_up` would refuse with
    # `CMD_CHANGED` on a config nobody edited — precisely the false alarm this
    # function's docstring explains it was narrowed to avoid. If it ever must
    # change, it needs a pin migration, not a one-word edit.
    payload = json.dumps(
        {"runner": runner, "cmd": cmd, "cwd": cwd_abs, "port_env": port_env},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
