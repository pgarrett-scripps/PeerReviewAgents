"""Manuscript workspaces, the investigation tools and the run reports.

What is left here after 0.9 is the part of the engine the plugin uses: a PDF
becomes a workspace of addressable source blocks (:mod:`ingest`,
:mod:`annotations`), an agent investigates that workspace through bounded tools
(:mod:`investigation`), and a finished run is rendered as a report
(:mod:`run_report`, :mod:`usage`). There is no orchestration here any more. The
review itself — reviewers, specialists, the editor, the ledger and the budgets
— belongs to the ``pra`` plugin and :mod:`peerreviewagents.plugin.server`.

Importing this package starts no model provider and reads no manuscript.
"""

# Tracks the distribution version in pyproject.toml.
__version__ = '0.9.0'
__all__ = ['__version__']
