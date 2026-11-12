"""Read one run folder: :func:`read_run`, the supported way into a finished run.

An application that shows a review has to answer four questions about a folder
on disk — did it finish, why did it stop, what did it find, and what did it
cost — and until 0.9 the only way to answer them was to open
``editor-package.json``, ``findings.jsonl``, ``runtime.json``,
``progress.json``, ``protocol.json`` and ``manifest.json`` by hand and know
which of them a stopped run never wrote. :func:`read_run` is that reading, kept
beside the writer so it cannot drift from it.

It never raises for an unfinished run. Every kind of folder a review can leave
behind is a reading: ``complete``, ``complete-without-findings``, ``blocked``,
a run the starter killed on a token ceiling, a wall-clock limit, a rate limit
or a session error, and a run whose start-up probe refused to launch a session
at all and left nothing but ``startup.json`` and ``runtime.json``. What it does
refuse is a folder that is not a run, and a run file that is a symlink: the
reports and the ledger are written in place by the server, so a symlink in
their place points at a file some other process owns, and following it would
report that file's contents as this run's findings.
"""

from __future__ import annotations

import json
from pathlib import Path

from .engine.usage import agent_rows, observed_usage
from .names import COMPLETE_STATUSES, MANUSCRIPT_DIRECTORY, RUN_DIRECTORY

#: The files that make a folder a review folder. A run that was killed before
#: the server wrote anything still has ``startup.json`` from the probe.
RUN_MARKERS = ('protocol.json', 'progress.json', 'runtime.json', 'startup.json',
               'findings.jsonl', 'editor-package.json')


class NotARunFolder(ValueError):
    """The path is not a run folder and not the review folder inside one."""


def _no_symlink(path):
    """``path`` itself, or a refusal naming it. A run file is never a link."""
    if path.is_symlink():
        raise ValueError(f'Refusing to read {path.name}: a run file must not be a symlink '
                         f'({path})')
    return path


def _json(path):
    """One JSON file, or ``None`` when the run never wrote it."""
    path = _no_symlink(Path(path))
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        # A file truncated by a killed process is a fact about the run, not a
        # reason to refuse the whole reading.
        return None
    return value if isinstance(value, (dict, list)) else None


def _rows(path):
    """Every record in a JSON-lines ledger, skipping a half-written last line."""
    path = _no_symlink(Path(path))
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _is_review_folder(path):
    return any((path / name).is_file() for name in RUN_MARKERS)


def locate(path):
    """``(run root, review folder)`` for a run folder or the review folder in one.

    Either spelling is accepted because both are what callers have: the starter
    is given the run folder and writes ``<run>/review``, while
    ``peerreview report`` is often pointed straight at the review folder.
    """
    given = Path(path).resolve()
    if not given.is_dir():
        raise NotARunFolder(f'No run folder at {given}')
    if _is_review_folder(given / RUN_DIRECTORY):
        return given, given / RUN_DIRECTORY
    if _is_review_folder(given):
        root = given.parent if given.name == RUN_DIRECTORY else given
        return root, given
    raise NotARunFolder(f'{given} is not a run folder: none of ' + ', '.join(RUN_MARKERS)
                        + ' is in it, nor in a review/ folder inside it')


def dedupe(findings):
    """One row per ``finding_uid``: the latest submission of each, first-seen order.

    This is the ledger's own rule. ``findings.jsonl`` is append-only on purpose
    — a reviewer that restates a finding writes a second row, and nothing is
    overwritten — so a reader that counts rows counts resubmissions as findings.
    A row with no ``finding_uid`` keeps its own identity rather than collapsing
    with every other one.
    """
    order, latest = [], {}
    for index, finding in enumerate(findings):
        uid = finding.get('finding_uid') or f'#{index}'
        if uid not in latest:
            order.append(uid)
        latest[uid] = finding
    return [latest[uid] for uid in order]


def _identity(manifest, protocol, paper, pdf_from_startup=None):
    """Who the run reviewed, from the files that say so rather than from a name."""
    manifest, protocol, paper = manifest or {}, protocol or {}, paper or {}
    plan = manifest.get('plan') or protocol.get('plan') or {}
    return {
        'paper_id': manifest.get('paper_id') or protocol.get('paper_id') or paper.get('id'),
        'source_sha256': manifest.get('source_sha256') or paper.get('source_sha256'),
        'pdf': manifest.get('pdf') or protocol.get('pdf') or pdf_from_startup,
        'title': paper.get('title') or protocol.get('title'),
        'arm': protocol.get('arm') or protocol.get('mode') or plan.get('arm'),
        'reviewers': protocol.get('reviewers') if protocol.get('reviewers') is not None
        else plan.get('reviewers'),
        'specialists': plan.get('specialists'),
        'literature_cutoff': protocol.get('literature_cutoff'),
        'workflow_version': manifest.get('workflow_version') or protocol.get('version'),
        'finding_schema_version': (manifest.get('finding_schema_version')
                                   or protocol.get('finding_schema_version')),
        'claude_code_version': manifest.get('claude_code_version'),
    }


def _cause(runtime, progress, startup):
    """Why the run is not a written review, in one sentence, or ``None``.

    Three files can hold it and they are read in the order of how much the
    writer knew: the starter saw the session end, the server saw the stage that
    failed, and the probe saw a machine that could not start one at all.
    """
    for value in ((runtime or {}).get('blocked_reason'), (progress or {}).get('error')):
        if value:
            return str(value)
    problems = (startup or {}).get('problems') or []
    if problems:
        return 'the start-up probe failed: ' + '; '.join(str(problem) for problem in problems)
    return None


def read_run(path):
    """Everything a caller needs about one run folder, from the run's own files.

    Returns a dict. The keys that matter most:

    ``status``
        One of :data:`peerreviewagents.names.RUN_STATUSES`, or ``unknown`` for
        a folder whose status file was never written. ``complete`` is the only
        status under which the findings are the whole review.
    ``complete``, ``blocked``
        The two questions asked of a status, already answered.
    ``cause``
        Why a run that is not complete stopped — a rate limit, a breached token
        ceiling, the wall-clock limit, a stage that never reported, a start-up
        probe that refused to launch. ``None`` for a complete run.
    ``stage``
        ``startup`` when no session ever ran and no subscription usage was
        spent, ``session`` once one did.
    ``findings``, ``body``, ``appendix``
        The findings, de-duplicated to the latest row per ``finding_uid``, from
        ``editor-package.json`` when the editor wrote one and from the ledger
        (``findings.jsonl``) when it did not. ``findings_source`` says which,
        because a ledger reading has no editor placement: everything the server
        kept is in ``body`` and ``appendix`` is empty.
    ``escalations``, ``escalation_rulings``
        The specialist findings that asked for a ruling, and the rulings given.
    ``identity``
        What was reviewed: paper id, source hash, PDF path, arm, versions.
    ``agents``
        One row per agent slot: role, track, model, server and session tool
        calls, tokens, state. Empty for a run that never claimed a slot.
    ``reports``
        Absolute paths to what exists on disk — ``REVIEW.md``, ``REVIEW.txt``,
        each ``audit_<track>.md``, ``editor-package.json`` — and nothing else.

    Raises :class:`NotARunFolder` for a folder that is not a run, and
    ``ValueError`` for a run file that is a symlink.
    """
    root, review = locate(path)
    protocol = _json(review / 'protocol.json')
    manifest = _json(review / 'manifest.json')
    progress = _json(review / 'progress.json') or {}
    runtime = _json(review / 'runtime.json') or {}
    startup = _json(review / 'startup.json') or {}
    workspace = root / MANUSCRIPT_DIRECTORY
    paper = _json(workspace / 'paper.json') if (workspace / 'paper.json').is_file() else None

    status = str(runtime.get('status') or progress.get('status') or '') or None
    if status is None and startup.get('problems'):
        status = 'blocked'
    cause = _cause(runtime, progress, startup)
    if status is None:
        status = 'blocked' if cause else 'unknown'
    complete = status in COMPLETE_STATUSES

    package = _json(review / 'editor-package.json') or {}
    if package.get('findings') is not None or package.get('appendix') is not None:
        body = dedupe([f for f in package.get('findings') or [] if isinstance(f, dict)])
        appendix = dedupe([f for f in package.get('appendix') or [] if isinstance(f, dict)])
        source = 'editor-package'
    else:
        body = dedupe(_rows(review / 'findings.jsonl'))
        appendix = []
        source = 'ledger' if body else 'none'
    findings = body + appendix
    rulings = [r for r in package.get('escalation_rulings') or [] if isinstance(r, dict)]

    audits = {path.name.split('audit_', 1)[1].removesuffix('.md'): str(path)
              for path in sorted(review.glob('audit_*.md')) if not path.is_symlink()}
    reports = {name: str(review / filename) for name, filename in
               (('markdown', 'REVIEW.md'), ('text', 'REVIEW.txt'),
                ('editor_package', 'editor-package.json'))
               if (review / filename).is_file() and not (review / filename).is_symlink()}
    reports['audits'] = audits

    return {
        'run': str(root),
        'review': str(review),
        'status': status,
        'complete': complete,
        'blocked': status == 'blocked',
        'cause': None if complete else cause,
        'stage': 'startup' if (startup.get('problems') and not runtime.get('result')
                               and not progress.get('stages')) else 'session',
        'findings': findings,
        'body': body,
        'appendix': appendix,
        'findings_source': source,
        'counts': {'findings': len(findings), 'body': len(body), 'appendix': len(appendix),
                   'quarantined': len(_rows(review / 'quarantined.jsonl')),
                   'findings_delivered': progress.get('findings_delivered'),
                   'findings_submitted': progress.get('findings_submitted')},
        'quarantined': _rows(review / 'quarantined.jsonl'),
        'escalations': [f for f in findings if f.get('escalate')],
        'escalation_rulings': rulings,
        'summary': package.get('summary'),
        'verdict': package.get('verdict'),
        'limitations': package.get('limitations') or [],
        'identity': _identity(manifest, protocol, paper, (startup.get('probe') or {}).get('pdf')),
        'stages': progress.get('stages') or (runtime.get('stages') if runtime else None),
        'runtime': {'wall_seconds': runtime.get('wall_seconds'),
                    'tokens_total': runtime.get('tokens_total'),
                    'rate_limits': runtime.get('rate_limits') or [],
                    'per_agent_token_ceiling': runtime.get('per_agent_token_ceiling'),
                    'per_run_token_ceiling': runtime.get('per_run_token_ceiling'),
                    'claude_code_version': runtime.get('claude_code_version'),
                    'recorded': runtime.get('recorded')},
        'agents': agent_rows(review, protocol),
        'usage': observed_usage(review),
        'reports': reports,
        'startup_problems': list(startup.get('problems') or []),
        'files': {name: str(review / name) for name in RUN_MARKERS
                  if (review / name).is_file()},
    }


__all__ = ['read_run', 'locate', 'dedupe', 'NotARunFolder', 'RUN_MARKERS']
