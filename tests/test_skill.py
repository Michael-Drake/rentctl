# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Drake

"""The shared workflow skill names only interfaces that exist.

The skill is prose an agent acts on, so a tool, error code or subcommand it
mentions that the code does not have is an instruction to call something that
is not there. These checks tie every such name back to its source of truth.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rentctl import cli, mcp_server
from rentctl.core import errors

SKILL_DIR = Path(__file__).resolve().parent.parent / "plugin" / "skills" / "dev-environment"
SKILL_FILES = sorted(SKILL_DIR.rglob("*.md"))


def _frontmatter() -> dict:
    text = (SKILL_DIR / "SKILL.md").read_text()
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert m, "SKILL.md has no frontmatter"
    # No YAML dependency in the dev extra: the frontmatter is deliberately flat
    # (one `key: value` per line), and a double-quoted scalar parses as JSON.
    out: dict = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        assert sep and re.fullmatch(r"[a-z][a-z-]*", key), f"not a flat key: {line!r}"
        value = value.strip()
        out[key] = json.loads(value) if value.startswith('"') else value
    return out


def _all_text() -> str:
    return "\n".join(p.read_text() for p in SKILL_FILES)


def test_skill_files_present():
    names = {p.relative_to(SKILL_DIR).as_posix() for p in SKILL_FILES}
    assert {"SKILL.md", "references/troubleshooting.md", "references/examples.md"} <= names


def test_frontmatter_is_portable():
    fm = _frontmatter()
    assert set(fm) <= {"name", "description", "license", "metadata"}
    assert fm["name"] == SKILL_DIR.name
    assert len(fm["name"]) <= 64
    assert isinstance(fm["description"], str)
    assert len(fm["description"]) <= 1024
    assert "<" not in fm["description"] and ">" not in fm["description"]


def test_relative_links_resolve():
    text = (SKILL_DIR / "SKILL.md").read_text()
    for target in re.findall(r"\]\(([^)#]+)\)", text):
        assert (SKILL_DIR / target).is_file(), target


def test_mcp_tools_exist():
    registered = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    mentioned = set(re.findall(r"\benv_[a-z]+\b", _all_text()))
    assert mentioned, "the skill should name the MCP tools"
    assert mentioned <= registered, mentioned - registered


def test_error_codes_exist():
    codes = {v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str)}
    mentioned = set(re.findall(r"\b[A-Z]+(?:_[A-Z]+)+\b", _all_text())) | (
        {"INTERNAL"} & set(re.findall(r"\b[A-Z]+\b", _all_text()))
    )
    assert mentioned, "the skill should name error codes"
    assert mentioned <= codes, mentioned - codes


def _subcommands() -> set[str]:
    parser = cli._build_parser()
    for action in parser._actions:
        if action.choices and isinstance(action.choices, dict):
            return set(action.choices)
    raise AssertionError("no subparsers found")


def test_rent_subcommands_exist():
    subs = _subcommands()
    mentioned = set(re.findall(r"`rent ([a-z][a-z-]*)", _all_text()))
    mentioned |= set(re.findall(r"^rent ([a-z][a-z-]*)", _all_text(), re.MULTILINE))
    assert mentioned, "the skill should name rent subcommands"
    assert mentioned <= subs, mentioned - subs


@pytest.mark.parametrize("flag", ["--all", "--all-instances", "--lease-minutes", "--since", "--project"])
def test_rent_flags_exist(flag):
    # Only flags the skill actually mentions are checked; each must be real.
    if flag not in _all_text():
        pytest.skip(f"{flag} not mentioned")
    help_text = " ".join(
        opt for a in cli._build_parser()._actions if a.choices and isinstance(a.choices, dict)
        for sp in a.choices.values() for act in sp._actions for opt in act.option_strings
    )
    assert flag in help_text.split()


def test_trust_repo_only_forbidden():
    for path in SKILL_FILES:
        for line in path.read_text().splitlines():
            if "--trust-repo" in line:
                assert re.search(r"\b(never|not|don't|do not)\b", line, re.I), (path.name, line)


def test_no_private_paths():
    text = _all_text()
    assert "/Users/" not in text
    assert not re.search(r"\bADR-?\d", text)
    assert "adr/" not in text
