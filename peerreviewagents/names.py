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
#: ``paper.json`` and the page images. ``<run>/manuscript``.
MANUSCRIPT_DIRECTORY = 'manuscript'

#: The schema every stored finding is stamped with, in its ``schema_version``.
FINDING_SCHEMA_VERSION = 'review-finding-v3'

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

__all__ = ['RUN_DIRECTORY', 'MANUSCRIPT_DIRECTORY', 'FINDING_SCHEMA_VERSION', 'RUN_STATUSES',
           'COMPLETE_STATUSES', 'RUNNING', 'BLOCKED', 'COMPLETE', 'COMPLETE_WITHOUT_FINDINGS',
           'CLI_COMMANDS']
