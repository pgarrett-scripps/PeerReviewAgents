"""Evaluation harness: score the pipeline against a labeled OpenReview corpus.

Three stages, each a CLI subcommand (see :mod:`peerreviewagents.eval.__main__`):

  * ``fetch``   — pull a labeled corpus from OpenReview (PDFs + human scores
    + accept/reject decisions) into ``corpus.jsonl``.
  * ``run``     — run the review pipeline over the corpus, ``--repeats`` times
    per paper, into a resumable ``runs.jsonl`` with a provenance manifest on
    every record.
  * ``metrics`` — read corpus + runs and emit an agreement report (system vs
    human) and a consistency report (run-to-run stability).

The split lets you spend cheaply: run all papers once for agreement, then
re-run a small subset at higher ``--repeats`` for consistency, reusing the
first run (the runner is keyed by ``(paper_id, repeat)`` and skips work that
already exists).
"""

from __future__ import annotations

__all__ = ["schema", "corpus", "runner", "metrics"]
