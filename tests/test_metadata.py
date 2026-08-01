"""Release metadata that has to agree across files.

`pyproject.toml` and `CITATION.cff` both carry a version, and they drift the
moment someone bumps one and forgets the other — which surfaces as a Zenodo
record whose version disagrees with the package it archives. Cheap to assert,
annoying to discover later.
"""

from __future__ import annotations

from pathlib import Path

import yaml

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _citation() -> dict:
    return yaml.safe_load((ROOT / "CITATION.cff").read_text())


def test_version_matches_between_pyproject_and_citation():
    assert _citation()["version"] == _pyproject()["project"]["version"]


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
