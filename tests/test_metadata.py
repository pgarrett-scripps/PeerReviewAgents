"""Release metadata must agree before the release workflow publishes.

The tag must match pyproject.toml, and CITATION.cff must match that version.
Zenodo inherits the GitHub release tag without a static JSON override.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _citation() -> dict:
    return yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))


def test_version_matches_between_pyproject_and_citation():
    assert _citation()["version"] == _pyproject()["project"]["version"]


def test_zenodo_inherits_the_github_release_version():
    metadata = json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))
    assert "version" not in metadata, (
        "Remove version from .zenodo.json so Zenodo uses the GitHub release tag. "
        "A static override can archive a release under an outdated version."
    )


def test_citation_has_the_fields_github_needs():
    """A malformed CITATION.cff silently drops the 'Cite this repository' button."""
    cff = _citation()
    for key in ("cff-version", "message", "title", "authors"):
        assert key in cff, f"CITATION.cff is missing {key!r}"
    assert cff["authors"], "CITATION.cff lists no authors"


def test_author_names_are_well_formed():
    for author in _citation()["authors"]:
        assert author.get("family-names"), f"author missing family-names: {author}"
        assert author.get("given-names"), f"author missing given-names: {author}"


def test_orcids_are_valid_identifiers():
    """A malformed ORCID is worse than a missing one — it credits nobody."""
    import re

    pattern = re.compile(r"^https://orcid\.org/\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
    for author in _citation()["authors"]:
        orcid = author.get("orcid")
        if orcid is None:
            continue  # absent is fine; wrong is not
        assert pattern.match(orcid), f"malformed ORCID for {author['family-names']}: {orcid}"


def test_license_agrees_with_pyproject():
    cff_license = _citation()["license"]
    declared = _pyproject()["project"]["license"]
    text = declared if isinstance(declared, str) else declared.get("text", "")
    assert cff_license == text, f"CITATION.cff says {cff_license}, pyproject says {text}"


def test_the_lock_file_pins_the_version_this_tree_is():
    """``uv.lock`` sat at 0.9.1 while pyproject.toml said 0.10.0.

    The lock is what a reproduction installs. Pinned to the release before,
    ``uv sync`` built a tree whose own package was a version behind the code
    beside it, and the release workflow published from the lock.
    """
    import re

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    entry = re.search(r'name = "peerreviewagents"\nversion = "([^"]+)"', lock)
    assert entry, "uv.lock no longer holds an entry for this package"
    assert entry.group(1) == _pyproject()["project"]["version"], (
        f"uv.lock pins {entry.group(1)} and pyproject says "
        f"{_pyproject()['project']['version']}; run `uv lock`")


def test_every_version_the_prose_points_back_to_is_one_that_was_released():
    """``a run written before 0.9.2`` named a release that never happened.

    A sentence about what an earlier release did is a promise a reader can
    check against the changelog. Pointing at a version that is not in it — a
    number picked while the release was still being planned — tells them to
    look for behaviour no tag ever had.
    """
    import re

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = set(re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M))
    assert released, "CHANGELOG.md lists no released version"
    sources = [*(ROOT / "peerreviewagents").rglob("*.py"),
               *(ROOT / "peerreviewagents").rglob("*.md"),
               *(ROOT / "docs").rglob("*.md")]
    for path in sorted(sources):
        text = path.read_text(encoding="utf-8")
        for version in re.findall(r"[Bb]efore (\d+\.\d+\.\d+)", text):
            assert version in released, (
                f"{path.relative_to(ROOT)} points back to {version}, which is not a release in "
                f"CHANGELOG.md: {sorted(released)}")


def test_the_integrations_doc_counts_the_tools_the_server_has():
    """A number in prose drifts the moment a tool is added, and silently.

    ``docs/INTEGRATIONS.md`` tells an integrator how many tools the server
    serves and how many the agents are allowed, in two different sentences.
    They were 23 and 27 against a server of 28 — the doc had been right about
    one release and then two tools arrived. The count is the server's to state,
    so the doc is checked against it rather than against itself.
    """
    import re

    from peerreviewagents.plugin.server import TOOLS

    text = (ROOT / "docs" / "INTEGRATIONS.md").read_text(encoding="utf-8")
    numbers = re.findall(r"(\d+) (?:MCP tools|`peerreview` tools)", text)
    assert numbers, "the doc no longer states a tool count anywhere"
    assert set(numbers) == {str(len(TOOLS))}, (
        f"the server serves {len(TOOLS)} tools and the doc says {sorted(set(numbers))}")
