"""Journal profiles: load venue-specific context for the review agents.

Each journal lives in one TOML file under ``journals_dir`` — by default the
profiles bundled inside this package (see ``README.md`` in this directory
for the schema). Override the directory via the ``journals_dir`` config key
or ``PEERREVIEW_JOURNALS_DIR`` to point at your own profiles.
A profile is parsed into a :class:`JournalProfile` and rendered to a
prompt block via :meth:`JournalProfile.to_prompt_block`, which is folded
into the shared manuscript context block so every agent that reviews
against a target venue sees the same standards, scope, and limits.

The loader is deliberately forgiving: every field except ``name`` is
optional, unknown keys are ignored, and a selected-but-missing slug is a
clear error the CLI/web layer can surface before a run starts. When no
journal is selected the agents receive an empty block and behave exactly
as before.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


def _default_journals_dir() -> Path:
    """The bundled profiles directory — this package's own directory, which
    holds the shipped ``*.toml`` profiles alongside this module. Resolved
    relative to this file so it works whether the package is run from a
    source checkout, an installed wheel, the CLI, the web server, or
    pytest."""
    return Path(__file__).resolve().parent


def journals_dir(config: dict | None = None) -> Path:
    """Resolve the directory holding journal ``.toml`` profiles.

    Precedence: explicit ``journals_dir`` in config, else the bundled
    profiles shipped inside this package. A relative config value is
    resolved against the current working directory (matching ``output_dir``
    semantics).
    """
    raw = (config or {}).get("journals_dir")
    if raw:
        return Path(os.path.expanduser(str(raw)))
    return _default_journals_dir()


class ArticleTypeLimits(BaseModel):
    """Per-venue overrides for one universal article type.

    The article-type *taxonomy* (what a Letter or Review is, and how to judge
    it) lives in :mod:`peerreviewagents.article_types`; a journal only supplies
    the specifics that actually differ between venues. Every field is optional —
    a type listed with no overrides still gets the shared general framing.
    """

    max_words: int = 0
    abstract_max_words: int = 0
    notes: str = ""


class JournalProfile(BaseModel):
    """One journal's submission context. Mirrors the TOML schema in
    ``journals/_template.toml``; only ``name`` is required."""

    slug: str = Field(..., description="Filename stem used to select this journal.")
    name: str
    aliases: list[str] = Field(default_factory=list)
    publisher: str = ""
    field: str = ""

    impact_factor: float = 0.0
    impact_factor_year: int = 0
    acceptance_rate: str = ""

    audience: str = ""
    description: str = ""
    scope: str = ""

    max_words: int = 0
    abstract_max_words: int = 0
    max_figures: int = 0
    max_references: int = 0

    # Optional per-type cap/notes overrides, keyed by the universal article-type
    # slug (see peerreviewagents.article_types). Venues that don't differentiate
    # by manuscript type leave this empty.
    article_types: dict[str, ArticleTypeLimits] = Field(default_factory=dict)

    guidelines: str = ""
    last_updated: str = ""

    def article_type_limits(self, key: str) -> ArticleTypeLimits | None:
        """This venue's cap/notes overrides for article-type ``key``, if any."""
        return self.article_types.get(key)

    def _limits_line(self) -> str:
        """One line summarizing whatever hard limits the venue declares."""
        parts: list[str] = []
        if self.max_words:
            parts.append(f"main text ≤ {self.max_words} words")
        if self.abstract_max_words:
            parts.append(f"abstract ≤ {self.abstract_max_words} words")
        if self.max_figures:
            parts.append(f"≤ {self.max_figures} display items (figures + tables)")
        if self.max_references:
            parts.append(f"≤ {self.max_references} references")
        return "; ".join(parts)

    def to_prompt_block(self) -> str:
        """Render the profile as a prompt block for the review agents.

        Empty fields are omitted so the block stays compact. The caller
        wraps/positions this; here we only produce the inner content.
        """
        lines: list[str] = [
            "=== TARGET JOURNAL ===",
            f"Name: {self.name}",
        ]
        if self.field:
            lines.append(f"Field: {self.field}")
        if self.impact_factor:
            year = f" ({self.impact_factor_year})" if self.impact_factor_year else ""
            lines.append(f"Approx. impact factor: {self.impact_factor}{year}")
        if self.acceptance_rate:
            # The caveat is not padding. Rendered bare, this number gets used
            # as a post-review threshold: on a Nature submission the
            # meta-reviewer read "~8%", wrote that the panel's 3.52/5 "would
            # nominally suggest a major revision verdict, but for a target
            # venue with Nature's selectivity", and returned reject — over a
            # panel where four reviewers said minor, four said major, and none
            # said reject. The editor then followed it.
            #
            # The figure is a base rate over ALL submissions, most of which
            # never reach a referee. A manuscript being reviewed has already
            # passed the desk, so applying the headline rate again charges it
            # twice for a selection it already survived.
            lines.append(
                f"Approx. acceptance rate: {self.acceptance_rate} of all "
                "submissions — a base rate dominated by desk rejections, NOT a "
                "quota for manuscripts under review. This one has already "
                "cleared the desk. Judge it on the panel's findings and this "
                "venue's stated standards; do not lower a verdict to match "
                "this number."
            )
        if self.audience:
            lines.append(f"Audience: {self.audience}")
        if self.description:
            lines.append(f"About: {self.description.strip()}")
        if self.scope:
            lines.append(f"Scope: {self.scope.strip()}")
        limits = self._limits_line()
        if limits:
            lines.append(f"Submission limits: {limits}")
        if self.guidelines:
            lines.append("Author/reviewer guidelines:")
            lines.append(self.guidelines.strip())
        lines.append("=== END TARGET JOURNAL ===")
        return "\n".join(lines)


def _read_profile(path: Path) -> JournalProfile:
    with path.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    # Keep only keys the model knows about; ignore extras silently.
    known = {k: v for k, v in raw.items() if k in JournalProfile.model_fields}
    known["slug"] = path.stem
    return JournalProfile(**known)


def list_journals(config: dict | None = None) -> list[JournalProfile]:
    """All journal profiles in ``journals_dir``, sorted by name.

    Files whose stem starts with ``_`` (e.g. ``_template.toml``) are
    skipped. Unparseable files are skipped rather than aborting the list.
    """
    directory = journals_dir(config)
    if not directory.is_dir():
        return []
    out: list[JournalProfile] = []
    for path in sorted(directory.glob("*.toml")):
        if path.stem.startswith("_"):
            continue
        try:
            out.append(_read_profile(path))
        except Exception:  # noqa: BLE001 — a malformed file shouldn't break selection
            continue
    return sorted(out, key=lambda p: p.name.lower())


def load_journal(slug: str | None, config: dict | None = None) -> JournalProfile | None:
    """Load a single profile by slug, or ``None`` if ``slug`` is falsy.

    Raises ``FileNotFoundError`` (with the available slugs) when a
    non-empty slug doesn't resolve — callers validate at selection time so
    a typo fails fast instead of silently reviewing against no venue.
    """
    if not slug:
        return None
    path = journals_dir(config) / f"{slug}.toml"
    if not path.is_file():
        available = ", ".join(p.slug for p in list_journals(config)) or "(none found)"
        raise FileNotFoundError(
            f"unknown target journal {slug!r}; available: {available}"
        )
    return _read_profile(path)
