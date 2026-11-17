"""The names a run folder is made of: statuses, schema version, folder names.

This module imports nothing. An application that reads run folders needs these
constants to find the files and to decide what a status means, and until 0.9
they existed only as literals inside ``peerreviewagents.plugin.server`` — which
a reader had to import, and which is the writer of the files rather than their
description. They are re-exported at the top level, so
``from peerreviewagents import RUN_DIRECTORY`` is the supported spelling and
``plugin.server`` imports them from here rather than keeping a second copy.
"""

from __future__ import annotations

#: The folder inside a run that holds the review: the ledger, the run files,
#: the reports. ``<run>/review``.
RUN_DIRECTORY = 'review'

#: The folder inside a run that holds the imported manuscript, including
#: ``paper.md``, ``map.json``, ``manifest.json`` and the page images.
#: ``<run>/manuscript``.
MANUSCRIPT_DIRECTORY = 'manuscript'

#: The folder inside a run that holds the snapshot of the code the paper was
#: reviewed against, when a review was given any: ``<run>/code``, with the
#: copied text source under ``files/`` and the record of it in
#: ``manifest.json``. Absent from a run that reviewed no code.
CODE_DIRECTORY = 'code'

#: The schema every stored finding is stamped with, in its ``schema_version``.
FINDING_SCHEMA_VERSION = 'review-finding-v4'

#: What ``map.json`` and ``manifest.json`` of a manuscript workspace are. The
#: base is written once and never edited, so a reader checks this before
#: trusting the rows. ``paper-base-v2`` is the first shape whose rows can carry
#: a repair: a piece a model read off the page image keeps the converter's
#: words in ``original``, and the manifest says in ``ai_read`` whether the
#: manuscript was read at all. ``paper-base-v1`` is refused by name — it has no
#: field that says which pieces are the converter's own, so a consumer reading
#: one cannot tell a transcription from a reconstruction.
BASE_SCHEMA_VERSION = 'paper-base-v2'

#: What ``draft.json`` is: the converted pieces before the base is written.
#: A draft is this run's working copy, not a manuscript — it has no line
#: numbers, nothing cites it, and it is replaced by the base at the seal.
DRAFT_SCHEMA_VERSION = 'paper-draft-v1'

#: The name of the main document of a manuscript workspace. Supplements are
#: ``supplement-<name>.md`` beside it, and every citation names its document.
MAIN_DOCUMENT = 'paper.md'

#: What state a run's manuscript workspace is in, which is not the same
#: question as what state the run is in: ``draft`` is a converted paper the
#: Read stage has not sealed, so it has no line numbers and nothing can cite
#: it; ``sealed-by-reader`` is a base a model read the pages of;
#: ``sealed-without-read`` is a base written with no page read at all — an
#: import with the Read stage off, a reader that failed and sealed as
#: converted, or an application's own source through
#: :func:`peerreviewagents.write_manuscript`; and ``absent`` is a run
#: that stopped before the manuscript was imported. Without this an
#: application had to look for ``draft.json`` by name — a private file of this
#: package — to tell a run whose read stopped from one that never started.
MANUSCRIPT_DRAFT = 'draft'
MANUSCRIPT_SEALED_BY_READER = 'sealed-by-reader'
MANUSCRIPT_SEALED_WITHOUT_READ = 'sealed-without-read'
MANUSCRIPT_ABSENT = 'absent'

#: Every state a manuscript workspace can be read as, in the order one passes
#: through them.
MANUSCRIPT_STATES = (MANUSCRIPT_ABSENT, MANUSCRIPT_DRAFT, MANUSCRIPT_SEALED_BY_READER,
                     MANUSCRIPT_SEALED_WITHOUT_READ)

#: ``running`` while the session is alive, ``blocked`` for any outcome that is
#: not a written review, and the two ``complete`` statuses for one that is.
RUNNING = 'running'
BLOCKED = 'blocked'
COMPLETE = 'complete'
COMPLETE_WITHOUT_FINDINGS = 'complete-without-findings'

#: Every status a run folder can record, in the order a run passes through them.
RUN_STATUSES = (RUNNING, BLOCKED, COMPLETE, COMPLETE_WITHOUT_FINDINGS)

#: The statuses that mean the review was written. A run that completed without
#: findings is finished: it is not an error and not an empty folder.
COMPLETE_STATUSES = (COMPLETE, COMPLETE_WITHOUT_FINDINGS)

#: Every command ``peerreview`` has, in the order its help lists them. A wrapper
#: that offers its own commands checks them against this rather than against a
#: list of its own: ``review`` runs a review and ``report`` re-renders one, and
#: there is no third. The parsers behind them are
#: :func:`peerreviewagents.review_parser` and
#: :func:`peerreviewagents.report_parser`.
CLI_COMMANDS = ('review', 'report')

__all__ = ['RUN_DIRECTORY', 'MANUSCRIPT_DIRECTORY', 'CODE_DIRECTORY', 'FINDING_SCHEMA_VERSION',
           'BASE_SCHEMA_VERSION', 'DRAFT_SCHEMA_VERSION',
           'MAIN_DOCUMENT', 'RUN_STATUSES', 'COMPLETE_STATUSES', 'MANUSCRIPT_STATES',
           'MANUSCRIPT_DRAFT', 'MANUSCRIPT_SEALED_BY_READER', 'MANUSCRIPT_SEALED_WITHOUT_READ',
           'MANUSCRIPT_ABSENT',
           'RUNNING', 'BLOCKED', 'COMPLETE', 'COMPLETE_WITHOUT_FINDINGS', 'CLI_COMMANDS']
