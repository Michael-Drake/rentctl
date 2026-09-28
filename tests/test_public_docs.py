"""The public documents ship, and the links inside them resolve where they ship.

This file runs in two trees. In the private source tree `publish.py` exists, so
the published tree is *staged* into a tmp dir and checked there — the only
honest place to ask "does this link resolve for a stranger?", because the
private tree has files (the ADR log, the internal README) that never travel. In
the published tree there is no `publish.py`, and the tree under test is simply
the repo itself. Either way the assertion is about the published tree; neither
branch skips.

Why links: `plugin/README.md` once linked a private ADR. It rendered fine here
and was a dead link on GitHub, which is where every stranger reads it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DOCS = ("README.md", "SECURITY.md", "CONTRIBUTING.md", "plugin/README.md")
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def _load_publish():
    spec = importlib.util.spec_from_file_location("_rentctl_publish", REPO_ROOT / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def published_tree(tmp_path: Path) -> Path:
    if not (REPO_ROOT / "publish.py").is_file():
        return REPO_ROOT  # we ARE the published tree
    dest = tmp_path / "published"
    _load_publish().stage(dest)
    return dest


def test_security_and_contributing_are_on_the_allowlist():
    """The allowlist fails closed: a file not named in SHIP never ships. In the
    published tree the files' presence at the root is the same proof."""
    if (REPO_ROOT / "publish.py").is_file():
        ship = _load_publish().SHIP
        assert ("SECURITY.md", "SECURITY.md") in ship
        assert ("CONTRIBUTING.md", "CONTRIBUTING.md") in ship
    else:
        assert (REPO_ROOT / "SECURITY.md").is_file()
        assert (REPO_ROOT / "CONTRIBUTING.md").is_file()


def test_every_public_doc_is_in_the_published_tree(published_tree: Path):
    for rel in PUBLIC_DOCS:
        assert (published_tree / rel).is_file(), f"{rel} is not in the published tree"


def test_relative_links_in_public_docs_resolve_in_the_published_tree(published_tree: Path):
    broken: list[str] = []
    for rel in PUBLIC_DOCS:
        doc = published_tree / rel
        for target in _LINK.findall(doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if not (doc.parent / path).exists():
                broken.append(f"{rel} -> {target}")
    assert not broken, "links that are dead in the published tree: " + ", ".join(broken)


def test_security_policy_publishes_no_email_address():
    """The reporting route is GitHub private vulnerability reporting, deliberately.
    An address added here would be the owner's, published to strangers."""
    text = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "security/advisories/new" in text
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
