"""The problems a fix check is given: the ``--problems`` file and its schema.

``peerreview check-fixes paper-v2.pdf out/ --problems problems.json`` reads a
new draft of a paper against the problems a previous review raised. Those
problems come in through this file, and this module is the whole of its
contract: what a row has to hold, what is refused, and how one is written from a
run this package already finished.

A problem is deliberately small — an id, one paragraph of statement, a severity
and the quotations of the **old** draft it was raised against::

    {"schema_version": "review-problems-v1",
     "problems": [
       {"id": "i01",
        "statement": "The abstract reports a 42% improvement the results do not show.",
        "severity": "major",
        "evidence": [{"document": "", "quote": "a 42% improvement over the baseline"}],
        "author_reply": "Corrected to 24% throughout."}]}

A bare JSON list of those rows is read as the same document, because a file
written by hand usually is one.

It is not an issue, and not a finding. The check does not need the editor's
category, the ranks, the member findings or the anchors of the old manuscript:
it needs to know what to look for and where it used to be. That is also why this
file is its own schema rather than ``issues.json`` — an application that keeps
its own issue list can write five fields, and the check does not pull a whole
review in behind them.

``evidence`` is what makes a verdict checkable. The server tells the checker up
front whether each of these quotations still appears verbatim in the new draft,
and a problem fixed by deletion rests on exactly that: the old words are gone.
A problem with no evidence is refused rather than checked against the statement
alone, which is a reading of the paper nobody can verify.

This module imports nothing from the rest of the package except the two leaf
modules :mod:`peerreviewagents.names` and :mod:`peerreviewagents.issues`, so the
writer of a problems file, the reader of one and the check that runs on it
cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from .issues import ISSUE_SEVERITIES, members_of, rows_of
from .names import PROBLEMS_SCHEMA_VERSION

#: Every field a problem row holds. ``author_reply`` is the only optional one —
#: it is ``null`` where the authors sent no reply — and every other field is
#: required, because a row missing one is a problem the check cannot look for.
PROBLEM_FIELDS = ('id', 'statement', 'severity', 'evidence', 'author_reply')

#: Every field one evidence row holds: the document the quotation came from
#: (empty for the manuscript itself, the supplement's name otherwise) and the
#: quotation of the old draft, as the review quoted it.
EVIDENCE_FIELDS = ('document', 'quote')

#: The most problems one file may carry. A fix check reads a paper once and
#: answers a queue; a file an order of magnitude past the queue's own cap is a
#: ledger, not a check, and refusing it here says so before a session is
#: started. The cap the *check* has is
#: :data:`peerreviewagents.plugin.server.MAX_FIX_CHECKS`, and the problems past
#: it are reported as not checked rather than refused.
MAX_PROBLEMS = 200

#: The most evidence rows one problem may carry. A review's issue pools the
#: findings of several reviewers, so a handful of quotations is ordinary and
#: fifty is a transcription of the paper.
MAX_EVIDENCE = 12


def problem_id(value) -> str:
    """*value* as a problem id: text, stripped, or ``''`` where it is not one.

    Any non-empty text is an id. The ids of a file written from a run are the
    issue ids it already published (``i01``, ``i02``, …) and an application that
    keeps its own list uses its own, so the form is the caller's and only the
    uniqueness is this module's.
    """
    return str(value).strip() if isinstance(value, str) else ''


def _text(value) -> str:
    return str(value).strip() if isinstance(value, str) else ''


def rows_in(document) -> list:
    """The problem rows of a problems document: the objects in it, in order.

    One tolerant reader for every caller. An object with ``problems`` and a bare
    list are both read; anything else, and any row that is not an object, reads
    as no row — :func:`problems_in_document` is where a reader says so.
    """
    rows = document if isinstance(document, list) else None
    if rows is None and isinstance(document, dict):
        rows = document.get('problems')
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def evidence_in(problem) -> list:
    """The evidence rows of one problem: the objects in ``evidence``, in order."""
    rows = (problem or {}).get('evidence') if isinstance(problem, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def problems_in_document(document, label='the problems file') -> list:
    """Every reason this document cannot be checked, each one sentence.

    The whole validation, in one place and with nothing raised, so a caller can
    report all of it at once instead of one error per run. :func:`read` raises
    the first sentence of this list; the launcher prints the lot.
    """
    if not isinstance(document, (dict, list)):
        return [f'{label} holds a {type(document).__name__} where a list of problems belongs']
    if isinstance(document, dict) and 'problems' not in document:
        return [f'{label} is an object with no problems key, so it names no problem to check']
    rows = document if isinstance(document, list) else document.get('problems')
    if not isinstance(rows, list):
        return [f"{label}'s problems is a {type(rows).__name__} where a list belongs"]
    out = []
    odd = sum(1 for row in rows if not isinstance(row, dict))
    if odd:
        out.append(f'{label} holds {odd} problem row(s) that are not objects')
    good = rows_in(document)
    if not good:
        out.append(f'{label} names no problem to check')
    if len(good) > MAX_PROBLEMS:
        out.append(f'{label} holds {len(good)} problems and a fix check reads at most '
                   f'{MAX_PROBLEMS}: check the paper against the ones that matter, in '
                   'severity order')
    seen = set()
    for position, row in enumerate(good, 1):
        identity = problem_id(row.get('id'))
        where = f'problem {identity}' if identity else f'the problem in row {position}'
        if not identity:
            out.append(f'{where} has no id, and the check answers one problem per id')
        elif identity in seen:
            out.append(f'{where} appears twice, and one id is one problem')
        seen.add(identity)
        if not _text(row.get('statement')):
            out.append(f'{where} has no statement, so there is nothing to look for')
        severity = _text(row.get('severity'))
        if severity not in ISSUE_SEVERITIES:
            out.append(f'{where} is {severity or "no severity"!r} where one of '
                       + ', '.join(ISSUE_SEVERITIES) + ' belongs')
        evidence = evidence_in(row)
        if not evidence:
            out.append(f'{where} carries no evidence quotation of the old draft, and a '
                       'verdict on a problem nobody can point at is not checkable')
        if len(evidence) > MAX_EVIDENCE:
            out.append(f'{where} carries {len(evidence)} evidence quotations and a problem '
                       f'carries at most {MAX_EVIDENCE}')
        for index, quoted in enumerate(evidence, 1):
            if not _text(quoted.get('quote')):
                out.append(f"{where}'s evidence {index} has no quote")
            if quoted.get('document') is not None and not isinstance(quoted.get('document'), str):
                out.append(f"{where}'s evidence {index} names a document that is not text")
        reply = row.get('author_reply')
        if reply is not None and not isinstance(reply, str):
            out.append(f'{where} holds an author_reply that is not text')
    return out


def normalize(document) -> dict:
    """This document as the check reads it: every field present on every row.

    The rows keep the order they were given in — a file written from a run is in
    the review's own rank order, and the queue the checker reads is built from
    it — and every field of :data:`PROBLEM_FIELDS` is on every row, ``null``
    where the file carried nothing, so a consumer reads a field by name.

    Nothing is validated here. :func:`problems_in_document` is the validation,
    and :func:`read` is both.
    """
    rows = []
    for row in rows_in(document):
        rows.append({
            'id': problem_id(row.get('id')),
            'statement': _text(row.get('statement')),
            'severity': _text(row.get('severity')),
            'evidence': [{'document': _text(quoted.get('document')),
                          'quote': _text(quoted.get('quote'))}
                         for quoted in evidence_in(row)],
            'author_reply': (_text(row.get('author_reply'))
                             if row.get('author_reply') is not None else None),
        })
    stamp = (document.get('schema_version') if isinstance(document, dict)
             else None) or PROBLEMS_SCHEMA_VERSION
    return {'schema_version': str(stamp), 'problems': rows}


def read(path) -> dict:
    """The problems in *path*, validated, as :func:`normalize` returns them.

    A file that is not there, is not JSON, or holds a problem the check cannot
    look for is a :class:`ValueError` naming every reason at once. Nothing is
    guessed at and nothing is dropped quietly: a check run against half a
    problems file would report a paper as fixed because the problem it was
    raised against was never read.
    """
    source = Path(path)
    label = str(source)
    if not source.is_file():
        raise ValueError(f'There is no problems file at {label}. '
                         'Write one with peerreviewagents.problems_from_run(<a finished run>).')
    try:
        document = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{label} could not be read as JSON: {exc}') from exc
    complaints = problems_in_document(document, label)
    if complaints:
        raise ValueError('\n'.join([f'{label} cannot be checked:',
                                    *[f'  - {one}' for one in complaints]]))
    return normalize(document)


def from_issues(issues) -> dict:
    """A problems document built from the issue rows of a finished review.

    One problem per issue, in the file's own order: the issue's ``id``,
    ``statement`` and ``severity``, and one evidence row per member — the
    document it was found in and the quotation the reviewer filed. The member's
    line numbers are deliberately left behind: they are positions in the *old*
    manuscript, and the check reads a new draft whose lines are somewhere else.

    An issue whose members carry no quotation is left out. It is a problem with
    no passage to look for, which :func:`read` refuses in a file written by
    hand, and writing one here would make a file this module cannot read.
    """
    rows = []
    for issue in rows_of(issues) or (issues if isinstance(issues, list) else []):
        if not isinstance(issue, dict):
            continue
        evidence, seen = [], set()
        for member in members_of(issue):
            quote = _text(member.get('quote')) or _text(member.get('anchor_text'))
            document = _text(member.get('document'))
            if not quote or (document, quote) in seen:
                continue
            seen.add((document, quote))
            evidence.append({'document': document, 'quote': quote})
            if len(evidence) >= MAX_EVIDENCE:
                break
        identity = problem_id(issue.get('id'))
        severity = _text(issue.get('severity'))
        if not identity or not evidence or severity not in ISSUE_SEVERITIES:
            continue
        rows.append({'id': identity,
                     'statement': _text(issue.get('statement')) or _text(issue.get('name')),
                     'severity': severity,
                     'evidence': evidence,
                     'author_reply': None})
    return {'schema_version': PROBLEMS_SCHEMA_VERSION, 'problems': rows[:MAX_PROBLEMS]}


def problems_from_run(run, path=None) -> dict:
    """Write the ``--problems`` file for a finished run, and return what it holds.

    *run* is a run folder or the review folder inside it — whatever
    :func:`peerreviewagents.read_run` takes — and the problems are its published
    issues: the required changes and the suggestions, in the order the review
    ranked them, each with the quotations the reviewers filed.

    *path* is where to write it. The default is ``<run>/review/problems.json``,
    beside the review it was built from, so the file that says what to re-check
    lives with the review that asked for it. The document is returned either
    way, so an application that wants to edit the list — drop the suggestions,
    add the authors' replies — can write its own file from this one::

        document = problems_from_run('runs/v1')
        for problem in document['problems']:
            problem['author_reply'] = replies.get(problem['id'])

    A run that recorded no issues writes a file with an empty list, which
    ``check-fixes`` refuses: there is nothing to check, and that is better said
    by the command that was asked to check it than guessed at here.
    """
    from .runs import locate  # Local: `runs` reads this module's output, not the reverse.

    _, review = locate(run)
    document = review / 'issues.json'
    try:
        held = json.loads(document.read_text(encoding='utf-8')) if document.is_file() else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'{document} could not be read as JSON: {exc}') from exc
    built = from_issues(held)
    target = Path(path) if path else review / 'problems.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(built, indent=2) + '\n', encoding='utf-8')
    return built


__all__ = ['PROBLEM_FIELDS', 'EVIDENCE_FIELDS', 'MAX_PROBLEMS', 'MAX_EVIDENCE',
           'problem_id', 'rows_in', 'evidence_in', 'problems_in_document', 'normalize',
           'read', 'from_issues', 'problems_from_run']
