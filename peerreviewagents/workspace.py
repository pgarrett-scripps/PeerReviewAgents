"""The workspace helpers an application needs to write what the engine reads.

An app that keeps its own files beside a manuscript workspace — a revision, a
saved review, an imported scaffold, an export — writes files the engine reads
back and verifies. So it cannot have its own digest, its own JSON writer or its
own review normalisation: a second implementation of any of them is a file the
engine refuses, or worse, accepts as something it is not. These are the
engine's own objects, re-exported here under names that say what they are:

    from peerreviewagents.workspace import digest, read_json, write_json

:func:`digest` is the hash every manifest entry in a workspace is checked
against. :func:`read_json` and :func:`write_json` are the one reader and the one
writer of every JSON file in a run or a workspace; the write is atomic and
UTF-8, which is what makes a half-written file impossible rather than merely
unlikely. :func:`save_review` stores a review against a workspace, verifying it
first and refusing a review of a different manuscript version, and
:func:`normalize_review` is the anchoring step inside it, for an app that wants
the normalised findings without writing them. :func:`words` and :func:`text_of`
are the two text helpers the import uses, so a word count or an element's text
means the same thing on both sides.

The whole-workspace operations stay on the top level, where an app meets them
first: :func:`peerreviewagents.import_paper`,
:func:`peerreviewagents.verify_workspace` and
:func:`peerreviewagents.export_draft`. This module is the smaller tools those
are built from, kept in one place rather than as seven more top-level names.

Every name here is the same object the engine itself uses — the import is an
alias, not a copy — so there is no version of these with two behaviours.
"""

from __future__ import annotations

from .engine.annotations import normalize_review, save_review
from .engine.ingest import text_of, words
from .engine.storage import digest
from .engine.storage import read as read_json
from .engine.storage import write as write_json

__all__ = [
    "digest",
    "read_json",
    "write_json",
    "save_review",
    "normalize_review",
    "words",
    "text_of",
]
