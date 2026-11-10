"""Offline advisory checks backed by a local Paper Scaffold checkout.

The imported manuscript is plain extracted text.  This module therefore reuses
only scaffold functions that accept plain text.  It never runs the Typst source
cleaner, reads scaffold manuscript declarations, or applies journal limits.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.metadata
import platform
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .annotations import verify_workspace

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


SCHEMA_VERSION = 1
REVIEW_RULES = {
    "doubled-word",
    "misspelling",
    "long-sentence",
    "verbose-phrase",
    "double-hedge",
    "opener-run",
    "word-repetition",
}
_SCAFFOLD_MODULES = (
    "bibliography",
    "manuscript_sources",
    "prose_check",
    "prose_rules",
    "readability",
    "typst_prose",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dependency_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _git_revision(root: Path) -> str | None:
    """Read a checkout revision without invoking Git or changing the checkout."""
    dotgit = root / ".git"
    if not dotgit.exists():
        return None
    if dotgit.is_file():
        match = re.fullmatch(r"gitdir:\s*(.+)\s*", dotgit.read_text())
        if not match:
            return None
        gitdir = (root / match.group(1)).resolve()
    else:
        gitdir = dotgit
    head = (gitdir / "HEAD").read_text().strip()
    if not head.startswith("ref: "):
        return head or None
    ref = head[5:]
    loose = gitdir / ref
    if loose.is_file():
        return loose.read_text().strip() or None
    for packed in (gitdir / "packed-refs", gitdir.parent.parent / "packed-refs"):
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line and not line.startswith(("#", "^")):
                    value, name = line.split(" ", 1)
                    if name == ref:
                        return value
    return None


@contextlib.contextmanager
def _load_scaffold(scaffold_root: Path):
    """Load the sibling script modules from the explicitly supplied checkout."""
    tools = scaffold_root / "tools"
    required = [tools / "readability.py", tools / "prose_check.py"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("Paper Scaffold checkout is missing required tools: " + ", ".join(missing))

    saved = {name: sys.modules.get(name) for name in _SCAFFOLD_MODULES}
    for name in _SCAFFOLD_MODULES:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(tools))
    try:
        readability = importlib.import_module("readability")
        prose_check = importlib.import_module("prose_check")
        yield readability, prose_check
    finally:
        try:
            sys.path.remove(str(tools))
        except ValueError:
            pass
        for name in _SCAFFOLD_MODULES:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]


def _provenance(root: Path) -> dict:
    pyproject = root / "pyproject.toml"
    version = None
    if pyproject.is_file():
        version = tomllib.loads(pyproject.read_text()).get("project", {}).get("version")
    files = [root / "tools" / "readability.py", root / "tools" / "prose_check.py"]
    supporting = [root / "tools" / "prose_rules.py"]
    return {
        "scaffold_root": str(root),
        "scaffold_version": version,
        "scaffold_revision": _git_revision(root),
        "python": platform.python_version(),
        "dependencies": {
            "textstat": _dependency_version("textstat"),
            "codespell": _dependency_version("codespell"),
        },
        "reused_functions": [
            {
                "name": "readability.metrics",
                "path": str(files[0]),
                "sha256": _sha256(files[0]),
            },
            {
                "name": "prose_check.check",
                "path": str(files[1]),
                "sha256": _sha256(files[1]),
            },
        ],
        "supporting_files": [
            {"path": str(path), "sha256": _sha256(path)} for path in supporting
        ],
    }


def _citation_report(paper: dict) -> dict:
    references = paper.get("references", [])
    reference_ids = [row.get("id") for row in references if row.get("id")]
    duplicate_ids = sorted(key for key, count in Counter(reference_ids).items() if count > 1)
    cited_ids = sorted({
        citation
        for block in paper.get("blocks", [])
        for citation in block.get("citations", [])
        if citation
    })
    native = paper.get('source_format') == 'scaffold-typst'
    structured = bool(paper.get("xml_sha256")) or native
    known = set(reference_ids)
    result = {
        "scaffold_check": "unavailable",
        "scaffold_limitation": (
            "Paper Scaffold citation checks require BibTeX entries and citation keys. "
            "This workspace stores extracted reference records and source citation IDs, "
            "so those checks were not run."
        ),
        "workspace_linkage": "checked" if structured else "unavailable",
        "reference_count": len(references),
        "cited_reference_count": len(cited_ids) if structured else None,
        "duplicate_reference_ids": duplicate_ids,
        "unknown_citation_ids": sorted(set(cited_ids) - known) if structured else [],
        "uncited_reference_ids": sorted(known - set(cited_ids)) if structured else [],
    }
    if paper.get('port_provenance'):
        result['scaffold_limitation'] = 'Reference records and citation IDs were inherited from the verified source workspace. This port has no reconstructed BibTeX citation keys.'
        result['workspace_linkage'] = 'inherited_source_ids'
        result['workspace_limitation'] = 'Checks compare the original extracted citation IDs with the original reference records. This does not verify citation support or reconstruct native Scaffold bibliography semantics.'
    elif native:
        result['scaffold_limitation'] = 'The importer inventories literal BODY citation keys and BibTeX entry headers. Full bibliography validation occurs during native Word export.'
        result['workspace_linkage'] = 'partial_source_inventory'
        result['workspace_limitation'] = 'BODY keys are compared with a lexical BibTeX inventory. Front matter, dynamic citations, and separate bibliography prefixes are not fully resolved. Unused and unknown keys are inspection cues only. Citation support was not assessed.'
    elif not structured:
        result["workspace_limitation"] = (
            "The PDF-only import does not preserve reliable citation IDs. "
            "Uncited and unresolved reference checks were not inferred from rendered text."
        )
    else:
        result["workspace_limitation"] = (
            "Linkage compares only citation IDs retained in extracted blocks with extracted "
            "reference IDs. A missing link is an inspection cue, not proof that the paper "
            "failed to cite a reference."
        )
    return result


def _eligible_blocks(paper: dict) -> list[dict]:
    return [
        block for block in paper.get("blocks", [])
        if block.get("kind") not in {"heading", "reference", "table", "source"}
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]


def _rounded(metrics: dict) -> dict:
    return {
        "words": metrics["words"],
        "sentences": metrics["sents"],
        "words_per_sentence": round(metrics["wps"], 2),
        "flesch_kincaid_grade": round(metrics["fk"], 2),
        "flesch_reading_ease": round(metrics["ease"], 2),
        "gunning_fog": round(metrics["fog"], 2),
    }


def build_report(workspace, scaffold_root) -> dict:
    """Build an offline advisory report over verified immutable source blocks.

    ``workspace`` must be an imported paper workspace with a valid manifest.
    ``scaffold_root`` must name the exact Paper Scaffold checkout to reuse.
    No file in either location is changed.
    """
    workspace = Path(workspace).resolve()
    scaffold_root = Path(scaffold_root).resolve()
    paper = verify_workspace(workspace)
    blocks = _eligible_blocks(paper)

    section_text: dict[str, list[str]] = defaultdict(list)
    document_text: dict[str, list[str]] = defaultdict(list)
    findings = []
    block_metrics = []
    with _load_scaffold(scaffold_root) as (readability, prose_check):
        for block in blocks:
            text = block["text"]
            metrics = _rounded(readability.metrics(text))
            block_metrics.append({"block_id": block["id"], **metrics})
            section_text[str(block.get("section") or "Unspecified")].append(text)
            document_text[str(block.get("document") or "main")].append(text)
            for finding in prose_check.check(block["id"], text, text, text):
                if finding.rule not in REVIEW_RULES:
                    continue
                findings.append({
                    "block_id": block["id"],
                    "rule": finding.rule,
                    "message": finding.message,
                    "subject": finding.subject,
                    "context": finding.context,
                    "scaffold_severity": finding.severity,
                    "disposition": "advisory",
                })

        combined = "\n\n".join(block["text"] for block in blocks)
        overall = _rounded(readability.metrics(combined))
        by_section = {
            name: _rounded(readability.metrics("\n\n".join(texts)))
            for name, texts in sorted(section_text.items())
        }
        by_document = {
            name: _rounded(readability.metrics("\n\n".join(texts)))
            for name, texts in sorted(document_text.items())
        }

    manifest = workspace / "manifest.json"
    return {
        "schema_version": SCHEMA_VERSION,
        "paper_id": paper.get("id"),
        "source": {
            "verified": True,
            "source_sha256": paper.get("source_sha256"),
            "manifest_sha256": _sha256(manifest),
            "source_block_count": len(paper.get("blocks", [])),
            "checked_block_ids": [block["id"] for block in blocks],
        },
        "provenance": _provenance(scaffold_root),
        "scope": {
            "mode": "offline_advisory",
            "included_kinds": sorted({block.get("kind") for block in blocks}),
            "excluded_kinds": ["heading", "reference", "table", "source"],
            "note": (
                "Checks support editorial review only. Readability scores and style findings "
                "do not measure scientific validity, and scaffold authoring policy is not enforced."
            ),
        },
        "word_counts": {
            "method": "Paper Scaffold readability.metrics ASCII word pattern",
            "overall": overall["words"],
            "by_section": {name: row["words"] for name, row in by_section.items()},
            "by_document": {name: row["words"] for name, row in by_document.items()},
            "by_block": [
                {"block_id": row["block_id"], "words": row["words"]}
                for row in block_metrics
            ],
        },
        "readability": {
            "overall": overall,
            "by_section": by_section,
            "by_document": by_document,
            "by_block": block_metrics,
            "interpretation": (
                "Scores describe surface reading complexity. They are not evidence about "
                "scientific rigor, correctness, novelty, or reporting completeness."
            ),
        },
        "prose_findings": findings,
        "citations": _citation_report(paper),
    }


def compact_summary(report: dict) -> dict:
    """Return bounded reviewer context without dropping the report provenance."""
    findings = report.get("prose_findings", [])
    counts = Counter(row.get("rule") for row in findings)
    citation = report.get("citations", {})
    return {
        "paper_id": report.get("paper_id"),
        "source_verified": report.get("source", {}).get("verified", False),
        "checked_blocks": len(report.get("source", {}).get("checked_block_ids", [])),
        "words": report.get("word_counts", {}).get("overall", 0),
        "readability": report.get("readability", {}).get("overall", {}),
        "prose_advisories": len(findings),
        "prose_advisories_by_rule": dict(sorted(counts.items())),
        "prose_advisory_items": findings[:25],
        "prose_advisories_truncated": max(0, len(findings) - 25),
        "citation_linkage": citation.get("workspace_linkage"),
        "unknown_citation_ids": citation.get("unknown_citation_ids", []),
        "uncited_reference_ids": citation.get("uncited_reference_ids", []),
        "citation_limitation": citation.get("workspace_limitation"),
        "scaffold": {
            "version": report.get("provenance", {}).get("scaffold_version"),
            "revision": report.get("provenance", {}).get("scaffold_revision"),
        },
        "advisory_note": report.get("scope", {}).get("note"),
    }
