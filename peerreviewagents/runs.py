"""Read one run folder: :func:`read_run`, the supported way into a finished run.

An application that shows a review has to answer four questions about a folder
on disk — did it finish, why did it stop, what did it find, and what did it
cost — and until 0.9 the only way to answer them was to open
``issues.json``, ``findings.jsonl``, ``runtime.json``,
``progress.json``, ``protocol.json`` and ``manifest.json`` by hand and know
which of them a stopped run never wrote. :func:`read_run` is that reading, kept
beside the writer so it cannot drift from it.

It never raises for a run that stopped, and it never raises for a damaged file
either. A process killed mid-write is the ordinary case this function exists
for: a truncated ``runtime.json``, a half-written last line in a usage receipt
or a ledger with bytes that are not UTF-8 all read as far as they can be read,
and every file that could not be read in full is named in the ``problems`` list
of the result. The two things it does refuse are a folder that is not a run,
and any file or directory under the review folder that is a symlink: the
reports and the ledger are written in place by the server, so a symlink in
their place points at something another process owns, and following it would
report that file's contents as this run's findings.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import issues as issue_model
from .engine import citation_ledger
from .engine.usage import agent_rows, observed_usage
from .names import (
    BLOCKED,
    CODE_CHECK_VERDICTS,
    COMPLETE,
    COMPLETE_STATUSES,
    COMPLETE_WITHOUT_FINDINGS,
    FIX_CHECK_FILE,
    FIX_CHECK_SCHEMA_VERSION,
    FIX_VERDICTS,
    MANUSCRIPT_ABSENT,
    MANUSCRIPT_DIRECTORY,
    MANUSCRIPT_DRAFT,
    MANUSCRIPT_SEALED_BY_READER,
    MANUSCRIPT_SEALED_WITHOUT_READ,
    RUN_DIRECTORY,
    RUN_MARKERS,
    RUN_STATUSES,
)

# The one module that decides which model a slot runs also decides what counts
# as a different model, which is what this reader has to compare.
from .plugin import models as model_plan

# ``RUN_MARKERS`` — the files that make a folder a review folder — is imported
# above and stays in this module's `__all__`, which is the name consumers
# already read it by. It is defined in `peerreviewagents.names` because the
# code snapshot recognises a run folder by the same files, and that walk is in
# the engine, which cannot import this module.

#: The status reported for a folder whose status file was never written, or
#: whose status is a word this version does not know.
UNKNOWN = 'unknown'


class NotARunFolder(ValueError):
    """The path is not a run folder and not the review folder inside one."""


def _relative(path, base):
    """``path`` written from ``base``, or its bare name when it is not under one.

    Problem lines are printed verbatim by consumers, next to a run folder the
    reader already has, so a path in one says where in that folder to look
    rather than where this machine keeps it. ``os.path.relpath`` is the whole
    of it, with the bare name for the cases it cannot answer — no ``base``, or
    a different drive on Windows.
    """
    path = Path(path)
    if base is None:
        return path.name
    try:
        written = Path(os.path.relpath(path, Path(base))).as_posix()
    except ValueError:  # a different drive on Windows, which has no relative form
        return path.name
    # ``.`` is ``base`` itself and ``../`` is somewhere outside it, and neither
    # is a name a reader can look up in the folder they are holding.
    return path.name if written == '.' or written.startswith('..') else written


def _why(exc):
    """Why a file could not be read, in words, with no absolute path in them.

    ``str`` of an :class:`OSError` is ``[Errno 13] Permission denied:
    '/home/someone/runs/2026-03-04/review/citations/ledger.jsonl'`` — the
    reason and then the whole path of the machine that read it. The reason is
    the half a reader needs, and the path is already in the line the reason is
    put into, written from the run folder.
    """
    if isinstance(exc, UnicodeDecodeError):
        return 'it is not valid UTF-8'
    if isinstance(exc, OSError):
        return exc.strerror or exc.__class__.__name__
    if isinstance(exc, ValueError):
        return str(exc)
    # Anything else reaching here is a shape the reader did not foresee, and
    # its class is the half of the sentence that says what kind of damage it
    # was. ``TypeError`` alone is more use to the person holding the folder
    # than an empty parenthesis.
    return f'{exc.__class__.__name__}: {exc}'.strip(': ')


def _refusal(path, base=None):
    """The refusal for one symlinked run file, naming it with its folder.

    ``base`` is the folder the name is written from, so the file that is
    refused is ``citations/ledger.jsonl`` rather than ``ledger.jsonl``: a run
    holds more than one file called ``usage.jsonl``, and a reader told only the
    bare name cannot go and look at the one that is a link.
    """
    return ValueError(f'Refusing to read {_relative(path, base)}: a run file must not be a '
                      f'symlink ({path})')


def _no_symlink(path, base=None):
    """``path`` itself, or a refusal naming it. A run file is never a link."""
    path = Path(path)
    if path.is_symlink():
        raise _refusal(path, base)
    return path


def refuse_symlinks(review, base=None):
    """Refuse the review folder, or anything in it, being a symlink.

    One walk, so a symlinked ``review/`` directory, a symlinked
    ``usage/core-1/usage.jsonl`` and a symlinked ``audit_stats.md`` are all
    refused the same way and by name, rather than two of them being followed
    and the third dropped without a word. The walk does not descend through a
    symlinked directory, so a link that points at its own parent is named, not
    followed round.

    Each one is named from the review folder — ``citations/ledger.jsonl``, not
    ``ledger.jsonl`` — because the bare name of a file a run keeps several of
    does not say which one to go and look at. ``base`` is that folder, for a
    caller walking one directory inside the review folder and wanting the name
    written from the review folder itself.
    """
    base = base or review
    review = _no_symlink(review, base)
    for folder, folders, files in os.walk(review, followlinks=False):
        for name in sorted(folders) + sorted(files):
            path = Path(folder) / name
            if path.is_symlink():
                raise _refusal(path, base)
    return review


def _mapping(path, problems, label=None):
    """One JSON object, or ``None`` with a problem recorded saying why not.

    Absent is not a problem — a run that stopped early wrote few of these
    files. Present and unreadable is, because a caller that sees findings and
    no complaint is entitled to assume the files behind them were whole.
    """
    path, label = Path(path), label or Path(path).name
    if not path.is_file():
        return None
    try:
        text = path.read_bytes().decode('utf-8')
    except OSError as exc:
        problems.append(f'{label} could not be read ({_why(exc)}), so it is read as absent')
        return None
    except UnicodeDecodeError:
        problems.append(f'{label} is not valid UTF-8, so it is read as absent')
        return None
    try:
        value = json.loads(text)
    except ValueError:
        problems.append(f'{label} is not valid JSON, which is what a process killed mid-write '
                        'leaves, so it is read as absent')
        return None
    if not isinstance(value, dict):
        problems.append(f'{label} holds a {type(value).__name__} where a JSON object belongs, '
                        'so it is read as absent')
        return None
    return value


def _rows(path, problems, label=None):
    """Every record in a JSON-lines ledger, skipping what cannot be read.

    A half-written last line is the normal state of a killed run, so it is
    skipped and counted rather than raised, and undecodable bytes are replaced
    rather than refused. Either way the result says so.
    """
    path, label = Path(path), label or Path(path).name
    if not path.is_file():
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        problems.append(f'{label} could not be read ({_why(exc)}), so it is read as empty')
        return []
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('utf-8', errors='replace')
        problems.append(f'{label} is not valid UTF-8, and the bytes that are not were replaced, '
                        'so a quotation from it may not be the bytes the agent wrote')
    rows, skipped = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(record, dict):
            rows.append(record)
        else:
            skipped += 1
    if skipped:
        problems.append(f'{skipped} unreadable line(s) in {label} were skipped, and a run killed '
                        'mid-write leaves one')
    return rows


def _citations_planned(protocol, agents):
    """Whether this run's arm asked for a citations lane at all.

    It matters because a missing ledger file means two opposite things. An arm
    that runs without the citations lane — ``reviewers-only``, a
    ``draft-check`` on other tracks — keeps no table on purpose, and saying so
    would be noise. An arm that planned the lane and left no table behind did
    not check a single citation, and a reader who is shown the same silence
    for both cannot tell which run they are holding.

    The plan is the first source because it is what the arm asked for, written
    at ingest; the slots are the fallback for a folder whose ``protocol.json``
    was lost or written by another version, since a ``spec-citations`` or
    ``cite-N`` row only exists where the lane was planned.
    """
    plan = (protocol or {}).get('plan')
    plan = plan if isinstance(plan, dict) else {}
    specialists = plan.get('specialists')
    if isinstance(specialists, (list, tuple)) and 'citations' in specialists:
        return True
    workers = plan.get('citation_workers')
    if isinstance(workers, (list, tuple)) and workers:
        return True
    return any(row.get('track') == 'citations' or row.get('role') == 'citation-worker'
               for row in agents or () if isinstance(row, dict))


def _unreadable(review, root, exc):
    """The citations reading for a ledger that is there and cannot be read.

    One sentence, in one place, because both public readers give it and a
    consumer prints whichever it got. It is the state where nothing in the
    table can be counted, which is not the same as a table saying nothing was
    checked: the counts come back empty and the line says the file is there
    and could not be read, so no reader mistakes one for the other.
    """
    where = _relative(Path(review) / citation_ledger.LEDGER_PATH, root)
    lines = [f'{where} is present and could not be read ({_why(exc)}), so this run has no '
             'citation counts']
    return ({'enabled': True, 'ledger': str(Path(review) / citation_ledger.LEDGER_PATH),
             'counts': {}, 'complete': False,
             'summary': citation_ledger.summary_line({}), 'problems': lines}, lines)


def _kept_rows(ledger, source_text):
    """``{'reference': [...], 'use': [...]}`` in the order a reader counts in.

    Separate from :func:`read_citation_ledger` so that the whole of building
    the rows — reading the file, sorting it, dropping the source text — sits
    inside that function's one guard rather than its first line only.
    """
    rows, _ = ledger.load() if ledger.exists() else ({}, 0)
    kept = {'reference': [], 'use': []}
    for row_id in sorted(rows, key=_natural):
        row = rows[row_id]
        if row.get('kind') in kept:
            kept[row['kind']].append(row if source_text else _without_source_text(row))
    return kept


def _agent_rows(review, protocol, runtime, problems):
    """The run's agent rows, for a ``runtime.json`` holding anything at all.

    The file is read through :func:`_mapping` before it gets here, so what
    reaches :func:`agent_rows` is a JSON object; what it holds is another
    matter. ``attempt`` as a list raised ``TypeError`` and ``attempt`` as
    ``Infinity`` — which Python's ``json`` writes and reads — raised
    ``OverflowError``, each out of both public readers, and each was one field
    caught by type with the next one waiting. The fields are guarded where they
    are read; this is the guard under them, so that no content of this file can
    raise out of a reader whatever the next shape turns out to be.

    ``Exception`` on purpose: the promise is about the file rather than about
    the shapes a version of this library foresaw, and a reader holding a run
    folder someone wants to look at has nothing to gain from the raise. Rows
    that could not be read are no rows and a line saying so, in the same family
    as ``runtime.json could not be read``: :func:`read_run` puts it in the run's
    own ``problems``, and the citation reading drops it, because an unreadable
    ``runtime.json`` is a fact about the run and not about the citation table.
    """
    try:
        return agent_rows(review, protocol, runtime=runtime, on_problem=problems.append)
    except Exception as exc:  # noqa: BLE001 - deliberate; see the docstring
        problems.append("runtime.json could not be read as this run's record of its agents "
                        f'({_why(exc)}), so no agent is listed')
        return []


def _citations_reading(review, root=None, protocol=None, agents=None):
    """:func:`_citations` with ``enabled`` decided once, for both readers.

    :func:`read_run` and :func:`read_citation_ledger` promise the same answer
    about one folder, and they were deciding "did this arm plan a citations
    lane" from different evidence: ``read_run`` from the plan and then the
    slots, the reader from the plan alone. A ``protocol.json`` written before
    the plan was recorded, or one whose plan was lost, has the lane only in its
    slots — a ``spec-citations`` row, a ``cite-N`` row — and the two readings
    disagreed about whether a missing ledger was worth a word. The decision
    lives here now, and neither caller makes it.

    ``protocol`` and ``agents`` are passed in by a caller that has already read
    them, and read here for one that has not. **Read the same way**, through
    :func:`_mapping`, which is how :func:`read_run` reads both of these files:
    two readers that read one folder through two readers of the same file can
    disagree about it, and one of those readings used to raise. ``agent_rows``
    left to read ``runtime.json`` itself raises ``ValueError`` on the
    half-written file a killed run leaves, which broke the public reader's
    promise of two raises only; handed the file already read, it cannot.

    The problems those reads record are dropped on purpose. A torn
    ``runtime.json`` is a fact about the run — :func:`read_run` reports it in
    ``problems``, beside the tokens and the wall time it costs — and it says
    nothing about the citation table. Putting it in ``citations['problems']``
    would make the one list both readers return disagree with ``read_run``'s,
    which is the promise this function exists to keep.
    """
    review = Path(review)
    noted = []
    if protocol is None:
        protocol = _mapping(review / 'protocol.json', noted)
    if agents is None:
        agents = _agent_rows(review, protocol,
                             _mapping(review / 'runtime.json', noted) or {}, noted)
    return _citations(review, enabled=_citations_planned(protocol, agents), root=root)


def _citations(review, enabled=False, root=None):
    """What the citation ledger says, and one line per stage that did not finish.

    Two separate lists come out of this. The counts and the stage lines go to
    the caller, which puts the lines in ``problems`` *after* ``publishable`` is
    decided: an unfinished citation stage is something a reader has to be told,
    and it is not a reason to withhold the review. The findings the specialist
    confirmed are real whether or not the table behind them was finished, and
    the counts say in numbers how much of the reference list was checked.

    A ledger that cannot be read at all is the same kind of answer and is
    reported the same way. It is the strongest version of an unfinished table —
    nothing in it can be counted rather than some of it — and the rule the
    citations lane is built on is that a citation check never withholds a
    review. The counts come back empty and the line says the file is there and
    could not be read, so no reader mistakes the empty counts for a reference
    list nobody had anything to say about.

    ``enabled`` is what the arm planned, from :func:`_citations_planned`. With
    no ledger file it is the whole of the answer: planned and missing is a
    reported gap, not planned is nothing to report. ``root`` is the run folder
    the paths in the lines are written from.
    """
    ledger = citation_ledger.Ledger(review)
    empty = {'enabled': False, 'ledger': None, 'counts': {}, 'complete': False,
             'summary': citation_ledger.summary_line({}), 'problems': []}
    if not ledger.exists():
        if not enabled:
            return empty, []
        # What this run did is known — it planned the lane and wrote no table.
        # What the specialist did is not: a run whose ledger stage never ran
        # has no record of a reference anybody looked at or did not, so saying
        # not one was checked would claim more than the folder holds.
        lines = ['this run planned a citations lane and wrote no citation ledger, so there are '
                 'no ledger counts']
        return {**empty, 'enabled': True, 'problems': lines}, lines
    try:
        totals = citation_ledger.counts(ledger)
    except Exception as exc:  # noqa: BLE001 — see below. This is the last guard.
        # Deliberately every exception, not the three a damaged file is
        # expected to raise. This reading is printed on consumer pages beside
        # a review that is already finished, and the promise those pages are
        # built on is that a citation check never withholds a review and never
        # crashes the page. A ledger shape nobody foresaw is a ledger that
        # could not be read, which is a state this function already has a
        # sentence for. ``citation_ledger._readable`` is where a known bad
        # shape is handled, one row at a time; this is for the unknown ones.
        return _unreadable(review, root, exc)
    lines = citation_ledger.problems(totals)
    return ({'enabled': True,
             'ledger': str(Path(review) / citation_ledger.LEDGER_PATH),
             'counts': totals,
             'complete': bool(totals.get('complete')),
             'summary': citation_ledger.summary_line(totals),
             'problems': lines},
            lines)


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
    if (given / RUN_DIRECTORY).is_symlink():
        raise _refusal(given / RUN_DIRECTORY)
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


def _code_hashes(value, problems):
    """The manifest's code map: one digest per file of the engine and the plugin.

    It is how a reading says which code served the run, so a consumer can tell
    two runs of the same arm apart when the plugin changed between them. Absent
    is an empty map, because a run that stopped before ``ingest`` has the
    starter's thin manifest, which records no code at all.
    """
    if not value:
        return {}
    if not isinstance(value, dict):
        problems.append(f"manifest.json's code holds a {type(value).__name__} where a map of "
                        'file to digest belongs, so it is read as empty')
        return {}
    hashes = {str(name): digest for name, digest in value.items() if isinstance(digest, str)}
    if len(hashes) != len(value):
        problems.append(f"{len(value) - len(hashes)} entry/entries in manifest.json's code map "
                        'name no digest, and they are left out of `code_hashes`')
    return hashes


def _provider(protocol, problems):
    """What ran the review — ``claude-code`` for every run this version writes.

    It is in the protocol rather than inferred, so a folder written by another
    provider reads as that provider instead of as this one.
    """
    value = protocol.get('provider')
    if value is None:
        return None
    if not isinstance(value, str):
        problems.append(f"protocol.json's provider holds a {type(value).__name__} where a name "
                        'belongs, so it is read as absent')
        return None
    return value


def _models(*candidates):
    """The per-slot models, ``{}`` for a run that recorded none.

    A run written before 0.9.0 pinned every slot's model in the plugin agent
    files and wrote no mapping, so an absent one is the ordinary old case and
    not a damaged file. Anything that is not a mapping of strings is dropped
    rather than reported: the self-reported model in ``agents`` still says what
    ran, and this is the plan, not the measurement.
    """
    for value in candidates:
        if isinstance(value, dict) and value:
            return {str(slot): str(model) for slot, model in value.items()
                    if isinstance(model, str) and model}
    return {}


def _reviewer_models(*candidates):
    """The ordered list the core reviewers were drawn from, or ``[]``."""
    for value in candidates:
        if isinstance(value, list) and value:
            return [str(model) for model in value if isinstance(model, str) and model]
    return []


def _identity(manifest, protocol, paper, pdf_from_startup=None, problems=None, agents=None):
    """Who the run reviewed, from the files that say so rather than from a name.

    *paper* is the manuscript workspace's own manifest, which is where the
    paper id, the title and the source digest are written down at import.
    """
    manifest, protocol, paper = manifest or {}, protocol or {}, paper or {}
    problems = problems if problems is not None else []
    plan = manifest.get('plan') or protocol.get('plan') or {}
    plan = plan if isinstance(plan, dict) else {}
    return {
        'paper_id': manifest.get('paper_id') or protocol.get('paper_id') or paper.get('paper_id'),
        'source_sha256': (manifest.get('source_sha256')
                          or (paper.get('source') or {}).get('sha256')),
        'pdf': manifest.get('pdf') or protocol.get('pdf') or pdf_from_startup,
        'title': paper.get('title') or protocol.get('title'),
        'arm': protocol.get('arm') or protocol.get('mode') or plan.get('arm'),
        'reviewers': protocol.get('reviewers') if protocol.get('reviewers') is not None
        else plan.get('reviewers'),
        'specialists': plan.get('specialists'),
        # Which model ran in which slot, and the ordered list the core
        # reviewers were drawn from. Both are empty for a run written before
        # 0.9.0, when every slot's model was pinned in the agent files.
        'models': _models(protocol.get('models'), plan.get('models')),
        'reviewer_models': _reviewer_models(protocol.get('reviewer_models'),
                                            plan.get('reviewer_models')),
        # The slots that were assigned a model and never reported running one.
        # Not a problem and not a reason the review cannot be published — a
        # slot nothing observed is not a slot that ran something else — but the
        # plan for those slots is a plan nobody checked, and an application
        # showing `models` has to be able to say so. The report writer draws
        # its own line from the same rule, `models.unverified`.
        'models_unverified': model_plan.unverified(agents),
        'literature_cutoff': protocol.get('literature_cutoff'),
        'workflow_version': manifest.get('workflow_version') or protocol.get('version'),
        'finding_schema_version': (manifest.get('finding_schema_version')
                                   or protocol.get('finding_schema_version')),
        'claude_code_version': manifest.get('claude_code_version'),
        'provider': _provider(protocol, problems),
        'code_hashes': _code_hashes(manifest.get('code'), problems),
    }


def _manuscript(workspace, paper):
    """What state the run's manuscript workspace is in, from the workspace.

    *paper* is its ``manifest.json`` as far as it could be read. A manifest is
    written once, at the seal, so its presence is the line between a draft and
    a manuscript — and ``ai_read`` is the line between a base a model read the
    pages of and one nothing read. An application asking "did the Read stage
    stop, or did it never start?" had to look for ``draft.json`` by name,
    which is this package's own working file and not part of the folder a
    consumer is promised.
    """
    read = (paper.get('ai_read') or {}) if isinstance(paper, dict) else {}
    read = read if isinstance(read, dict) else {}
    if paper:
        state = MANUSCRIPT_SEALED_BY_READER if read.get('read') else MANUSCRIPT_SEALED_WITHOUT_READ
    elif (workspace / 'draft.json').is_file():
        state = MANUSCRIPT_DRAFT
    else:
        state = MANUSCRIPT_ABSENT
    return {
        'state': state,
        'sealed': state in (MANUSCRIPT_SEALED_BY_READER, MANUSCRIPT_SEALED_WITHOUT_READ),
        # Why no model read the pages, in the words of whatever wrote the
        # manifest: the Read stage was off, a reader failed twice and the run
        # sealed as converted, or the pieces are an application's own source.
        'reason': str(read.get('reason') or '') if not read.get('read') else '',
        # The model that read the pages, or that wrote the pieces an
        # application composed with a model. Empty for a manuscript that is
        # the converter's reading throughout.
        'reader_model': str(read.get('reader_model') or read.get('ai_model') or ''),
        # Each page the converter returned nothing from, with the document and
        # lines its transcription became. Empty for a manuscript that lost no
        # page, and for one sealed before this was recorded.
        'lost_pages': list(read.get('lost_pages') or []),
        # What the model that read the pages left for whoever reads the
        # manuscript: a table the converter dropped, a page whose order it had
        # to choose. Its own account, not the authors' text, and empty unless
        # it filed one.
        'note': str(read.get('note') or ''),
        'documents': list(paper.get('documents') or []) if isinstance(paper, dict) else [],
        'folder': str(workspace) if state != MANUSCRIPT_ABSENT else None,
    }


def _code_reviewed(progress, manifest, problems):
    """Whether the run read code beside the paper, and what it read.

    ``progress.json`` carries the desk's own record — where the code came
    from, the commit it was at, how many files the snapshot held and which of
    them a reviewer opened — and ``manifest.json`` carries the snapshot
    manifest under ``code_under_review``, which is the full list including the
    files that were left out. A run written before the code lane existed holds
    neither, and that is a run that reviewed no code rather than a folder with
    something missing from it: every reading says ``reviewed: False`` and the
    rest is empty, so an application never has to ask whether the key is there.
    """
    empty = {'reviewed': False, 'kind': '', 'source': '', 'commit': '', 'branch': '',
             'dirty': None, 'snapshot': None, 'files_in_snapshot': 0, 'files_skipped': 0,
             'truncated': '', 'source_cut': 0, 'files_opened': [], 'opened': 0,
             'searches': 0, 'data_listed': 0, 'data_fetched': 0, 'data_partial': 0,
             'data_refused': 0, 'data_bytes': 0, 'manifest': None}
    progress, manifest = progress or {}, manifest or {}
    record = progress.get('code')
    if record is None or record == {}:
        return empty
    if not isinstance(record, dict):
        problems.append(f"progress.json's code holds a {type(record).__name__} where the record "
                        'of the code this run reviewed belongs, so it is read as no code')
        return empty
    if not record.get('reviewed'):
        return empty
    snapshot = record.get('snapshot')
    taken = manifest.get('code_under_review')
    return {
        'reviewed': True,
        'kind': str(record.get('kind') or ''),
        'source': str(record.get('source') or ''),
        'commit': str(record.get('commit') or ''),
        'branch': str(record.get('branch') or ''),
        # Whether the folder had uncommitted changes in it when it was taken.
        # ``None`` for a snapshot of something that is not a repository, where
        # the question does not arise.
        'dirty': record.get('dirty') if isinstance(record.get('dirty'), bool) else None,
        'snapshot': str(snapshot) if snapshot else None,
        'files_in_snapshot': _count(record.get('files_in_snapshot')),
        'files_skipped': _count(record.get('files_skipped')),
        # Why the snapshot stopped early, in the words of whatever took it: a
        # file count, a byte total. Empty for a snapshot that holds the whole
        # of what it was pointed at, which is the ordinary case.
        'truncated': str(record.get('truncated') or ''),
        # How many program source files a cap left out. Above zero, the
        # snapshot is not the whole of the code and a finding from it may not
        # say the code never does something — a different fact from the file
        # count, which is why it is a number of its own here.
        'source_cut': _count(record.get('source_cut')),
        # Which files a reviewer actually opened, out of the snapshot. A lane
        # that opened none read the paper and nothing else, and a reading that
        # reported only "code: reviewed" could not say so.
        'files_opened': [str(name) for name in (record.get('files_opened') or [])],
        'opened': _count(record.get('opened')),
        'searches': _count(record.get('searches')),
        # Data files are listed by the snapshot and copied into it only when a
        # reviewer asks, so what was read is a different number from what was
        # there. A file fetched only as far as its head is ``partial`` and a
        # finding may quote only the lines that were stored; a file the budget
        # or its own format refused was never opened at all.
        'data_listed': _count(record.get('data_listed')),
        'data_fetched': _count(record.get('data_fetched')),
        'data_partial': _count(record.get('data_partial')),
        'data_refused': _count(record.get('data_refused')),
        'data_bytes': _count(record.get('data_bytes')),
        'manifest': taken if isinstance(taken, dict) else None,
    }


def _code_check_reading(review, document, progress, problems):
    """What the code check answered on this run, or ``None`` where it had none.

    The lane exists only on a run that was given code: between the reviewers
    and the synthesis, every finding whose basis is ``computed`` and every one
    whose reviewer marked an ``assumed_method`` is read against the snapshot
    and comes back ``confirmed``, ``contradicted`` or ``not-shown``.

    ``issues.json`` is the published record and is read first: its ``code_check``
    block is what REVIEW.md's own section is rendered from, and every path in it
    is relative to the run folder. A run that stopped before the review was
    written has no block and may still have answered claims, so the second
    reading is the lane's own append-only file, ``code-checks.jsonl``, with the
    cap from ``progress.json``. Either way the counts are taken over the rows
    that come back, so a consumer's tally and the rows it was given cannot
    disagree.

    ``None`` means this run had no code check at all: every run that was given
    no code, and every run written before the lane existed.
    """
    stated = {}
    for label, value in (('issues.json', (document or {}).get('code_check')),
                         ('progress.json', (progress or {}).get('code_check'))):
        if value is None:
            continue
        if not isinstance(value, dict):
            problems.append(f"{label}'s code_check holds a {type(value).__name__} where the "
                            'record of the code check belongs, so it is read as no record')
            continue
        stated[label] = value
    published = stated.get('issues.json')
    if published is not None:
        rows = [dict(row) for row in (published.get('checks') or []) if isinstance(row, dict)]
        record = published
    else:
        path = review / 'code-checks.jsonl'
        rows = dedupe(_rows(path, problems)) if path.is_file() else []
        record = stated.get('progress.json')
        if record is None and not rows:
            return None
        record = record or {}
    cap = record.get('cap')
    return {
        # The most findings the check is handed. ``None`` only for a record
        # written by something that did not say, which no version writes.
        'cap': cap if isinstance(cap, int) and not isinstance(cap, bool) else None,
        'checked': len(rows),
        # How many findings qualified past the cap. They are not cleared and
        # not thrown out: they are exactly as their reviewers filed them, and
        # the number is here so that nobody reads them as either.
        'over_cap': _count(record.get('over_cap')),
        'counts': {verdict: sum(1 for row in rows if row.get('verdict') == verdict)
                   for verdict in CODE_CHECK_VERDICTS},
        'checks': rows,
    }


def _count(value):
    """A count as a number, ``0`` for anything that is not one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def cause_of(runtime=None, progress=None, startup=None):
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


def _known(value, label, problems):
    """A status this version knows, ``unknown`` for one it does not, ``None`` for none."""
    text = str(value or '').strip()
    if not text:
        return None
    if text not in RUN_STATUSES:
        problems.append(f'{label} records the status {text!r}, which is not one of '
                        + ', '.join(RUN_STATUSES) + ', so it is reported as unknown')
        return UNKNOWN
    return text


def stage_gap_line(slots):
    """What is missing from a finished review whose stage never filed a report.

    One sentence, in one place, so that ``REVIEW.md``, ``read_run`` and the
    status note cannot describe the same gap three different ways. The findings
    are not the missing part: they were stored, pooled and ruled on. What is
    missing is the stage's own account of its work.
    """
    slots = list(slots)
    if not slots:
        return ''
    which = ', '.join(slots)
    whose = 'its' if len(slots) == 1 else 'their'
    verb = 'never filed a report' if len(slots) == 1 else 'never filed reports'
    return (f'{which} delivered findings but {verb}, so {whose} summary and limitations '
            'are not in this review')


def run_status(progress=None, runtime=None, startup=None, problems=None):
    """The one status rule, used by :func:`read_run` and by the report writer.

    Two files record a status and they can disagree. ``progress.json`` is the
    server's: it is written when the review itself is written. ``runtime.json``
    is the starter's: it is written when the session ends, which is later, and
    it is the only record of a run killed before the server wrote anything.

    **The rule.** A review that was written is complete. If ``progress.json``
    records ``complete`` or ``complete-without-findings``, that is the status,
    and a block the starter recorded afterwards — a rate limit or a token
    ceiling that fired after ``write_review`` — becomes the ``note`` of a
    finished run rather than its cause. Otherwise the starter's status wins,
    because it saw the end of the session; then the server's; then a run with a
    cause but no status at all is ``blocked``; and a folder that says nothing is
    ``unknown``.

    Returns ``{'status', 'cause', 'note'}``. ``cause`` is ``None`` for a
    complete run and the sentence from :func:`cause_of` otherwise. ``note``
    carries the caveats of a finished run: a block recorded after the review
    was written, and a stage that delivered findings without ever filing a
    report — the review is there, and so is the hole in it.
    """
    progress, runtime, startup = progress or {}, runtime or {}, startup or {}
    problems = problems if problems is not None else []
    written = _known(progress.get('status'), 'progress.json', problems)
    ended = _known(runtime.get('status'), 'runtime.json', problems)
    cause = cause_of(runtime, progress, startup)
    note = None
    if written in COMPLETE_STATUSES:
        status = written
        notes = []
        if ended == BLOCKED and cause:
            notes.append('the review was written and the run was marked blocked afterwards: '
                         + cause)
        gap = (progress.get('stages') or {}).get('missing_with_findings') or []
        if gap:
            notes.append(stage_gap_line(gap))
        note = '; '.join(notes) or None
        cause = None
    elif ended and ended != UNKNOWN:
        status = ended
    elif written and written != UNKNOWN:
        status = written
    elif cause or startup.get('problems'):
        status = BLOCKED
    else:
        status = ended or written or UNKNOWN
    return {'status': status, 'cause': None if status in COMPLETE_STATUSES else cause,
            'note': note}


def publishable_reasons(status, findings_source, guard_status, problems, quarantined=0,
                        substitutions=(), issues_source=None, issues_source_reason=None):
    """Why this run is not publishable, in words, or an empty list when it is.

    One rule, in one place, so that ``publishable`` and the reasons can never
    disagree: ``publishable`` is this list being empty.

    A quarantined finding is not one of the reasons. The review guard exists to
    throw out a finding whose quote it could not match to the lines it names,
    and a guard that did that has done its job — what is left is a whole review
    minus one bad quote. Stage A found the cost of the other rule: a correct
    catch of a planted error was quarantined for a paraphrased quote, and the
    run it came from read as not publishable with nothing at all wrong in it.

    **A review with no findings in it.** ``complete-without-findings`` is the
    status for an issues document that holds no findings, and three different
    runs end that way. If the guard quarantined findings, the reviewers did
    raise something and it is not in the review, so there is nothing
    publishable to show and the reason says how many were held. If nothing was
    quarantined, the reviewers read the manuscript and raised nothing — "we
    found nothing make-or-break" is a review, and a run that reached it with an
    issues document and a clean guard is publishable like any other. The third
    is a run whose findings were all contradicted by the code check: the
    reviewers raised something, the authors' own code disproved it, and the
    review says so in its thrown-out section with the passage that did it. That
    is a finished review with no surviving problem in it, so it is publishable
    too — the caller passes ``findings_source`` ``issues`` for it, because a
    thrown-out finding is accounted for rather than unaccounted for.

    What does stop a run from being publishable is the review not being there to
    show: a status that is not ``complete``, a review emptied by the guard,
    findings that come from the ledger instead of from ``issues.json``, no
    issues document at all, no guard verdict, or a file that could not be read
    in full.

    **Issues the server synthesised** stop it too. Every arm writes
    ``issues.json`` so that one reader reads them all, but an arm with no editor
    has its issues made one-per-finding by the server, and nothing grouped,
    ranked or judged them. That is the right outcome for an author's own check
    and it is not a peer review, which is what ``publishable`` asks about —
    ``issues_source`` says ``server`` there and ``editor`` where an editor
    wrote them. The caller passes it only when the run holds issues at all: an
    editor-less run nobody raised anything in has nothing that needed grouping.

    **A slot that ran a different model family than it was assigned** stops it
    too, and each one is its own reason. Claude Code substitutes a model it may
    not run rather than refusing, and a substitution inside the session happens
    after every check this package can make up front: the run then reads as an
    arm nobody ran, and publishing it would attribute a report to a model that
    never wrote it. ``substitutions`` holds those lines — see
    :func:`peerreviewagents.plugin.models.substitutions`, which also decides
    what is not one: the same family at another version is the version that
    family resolved to on this login, and the observed id is what gets
    reported.
    """
    reasons = []
    if status == COMPLETE_WITHOUT_FINDINGS:
        if quarantined:
            reasons.append(f'the review was written with no findings in it, and the review guard '
                           f'quarantined {quarantined}, so what the reviewers raised is not in '
                           'the review and there is nothing to publish')
    elif status != COMPLETE:
        reasons.append(f'the run is not complete: its status is {status}')
    if findings_source == 'ledger':
        reasons.append('the findings come from the ledger and this run has no readable '
                       'issues.json, so what is here is the raw findings rather than the review')
    elif findings_source == 'none' and status in COMPLETE_STATUSES:
        reasons.append('the run holds no findings and no issues document')
    if issues_source == 'server':
        # The cause comes from the document, because there are three of them
        # and this line used to print one: an arm with no editor, a ledger past
        # the editor's stated limit, and an editor that never closed the
        # review. An older file that does not say which keeps the general line.
        reasons.append('the issues were synthesised by the server, one per finding, so nothing '
                       'grouped or ranked them into a review'
                       + (': ' + str(issues_source_reason).rstrip('.')
                          if issues_source_reason else ''))
    if guard_status is None:
        reasons.append('no review-guard verdict was recorded, so it is not known whether the '
                       'guard ran over these findings')
    if problems:
        reasons.append(f'{len(problems)} file(s) could not be read in full, and they are named '
                       'in `problems`')
    reasons += list(substitutions)
    return reasons


def read_run(path):
    """Everything a caller needs about one run folder, from the run's own files.

    Returns a dict with the same keys for every folder, whatever state it is
    in. The keys that matter most:

    ``status``
        One of :data:`peerreviewagents.names.RUN_STATUSES`, or ``unknown`` for
        a folder whose status file was never written or holds a word this
        version does not know. :func:`run_status` is the rule, and
        ``complete-without-findings`` means the review was written with no
        findings in it, which happens three ways: the reviewers raised nothing,
        the guard quarantined everything they did raise, or the code check
        contradicted it. ``counts['quarantined']`` and
        ``code_check['counts']['contradicted']`` are which of the three.
    ``complete``, ``blocked``
        The two questions asked of a status, already answered. ``complete`` is
        true for ``complete`` and for ``complete-without-findings``, so a run
        that delivered no findings is finished rather than failed.
    ``publishable``, ``not_publishable_reasons``
        True only when the whole review is here to show: status ``complete``,
        the review from a readable ``issues.json``, a guard that
        recorded a verdict, and nothing in ``problems``. When it is false,
        ``not_publishable_reasons`` says why in words, one entry per reason, and
        it is never false with nothing named. A quarantined finding is not a
        reason: the guard threw out a finding it could not anchor, which is the
        guard working, and what is left is publishable. ``quarantined`` says
        what was thrown out, and whether that mattered is a judgement for a
        person rather than a flag. A finding the code check contradicted is not
        a reason either, for the same reason and one more: the finding is
        accounted for, in the review's own thrown-out section with the passage
        of the authors' code that disproved it. A review with no findings in it
        is publishable when the reviewers raised nothing, and when every
        finding they raised was disproved by the code; it is not when the guard
        emptied it. A slot that reported
        running a different model family than the run assigned it is a reason
        of its own, naming the slot, the model assigned and the model that ran:
        the review would otherwise be attributed to a model that never wrote
        it. Another version of the same family is not a reason — that is the
        version the family resolved to, and ``agents[].model`` reports it.
    ``cause``, ``note``
        ``cause`` is why a run that is not complete stopped — a rate limit, a
        breached token ceiling, the wall-clock limit, a stage that never
        reported, a start-up probe that refused to launch. ``note`` carries a
        limit that fired *after* the review was written, which ends the session
        without unwriting the review.
    ``problems``
        Every file that could not be read in full, named, every slot whose
        observed model is a different family from the one it was assigned, and
        one line per citation stage that did not finish. Empty for a whole
        folder. A reading with problems is still a reading — that is the point —
        but a consumer should not publish it or quote a number from it without
        saying so. The citation lines are the exception and are not in
        ``not_publishable_reasons``: see ``citations``. No line here contains a
        semicolon or an absolute path — consumers print them verbatim on their
        own pages, beside a run folder the reader already has.
    ``citations``
        The citation ledger. ``ledger`` is the absolute path to
        ``citations/ledger.jsonl`` or ``None`` when this run kept none —
        an arm without the citations specialist fills no table, and
        ``enabled`` is false there. ``counts`` is the whole table in numbers:
        references, how many resolved, were not found, could not be checked,
        disagreed with the indexed record or are retracted; in-text uses, how
        many were judged and how they were judged, and how many nobody could
        judge. ``complete`` is true only when every stage finished.
        ``summary`` is one sentence of the same, and ``problems`` is one line
        per stage that did not finish. Those lines are in the top-level
        ``problems`` as well, and they are deliberately *not* reasons against
        publishing: a confirmed finding is a finding whether or not the table
        behind it was finished, and the counts say how much of the reference
        list this run actually checked. A ledger file that is there and cannot
        be read is one of those lines too, naming the file from the run folder:
        it is the strongest form of an unfinished table rather than a different
        kind of damage, and a citation check does not withhold a review.
        :func:`read_citation_ledger` is the same reading with the rows in it.
    ``stage``
        ``startup`` when no session ever ran and no subscription usage was
        spent, ``session`` once one did.
    ``issues``, ``issues_source``
        The review itself: one entry per issue, in the order ``issues.json``
        holds them, each with its ``id``, ``name``, ``severity`` (``major``,
        ``moderate`` or ``minor``), ``category``, ``rank``, one paragraph of
        ``statement``, and its ``members`` — the findings it is raised from,
        each with its
        ``finding_uid``, slot, quote, the lines it was cited on and the offsets
        of the quote within them, kept as evidence.
        ``raised_by`` is how many reviewer slots are among those members.
        Every row carries every field of
        :data:`peerreviewagents.issues.ISSUE_FIELDS`, and every member every
        field of ``MEMBER_FIELDS``, so a consumer reads a field rather than
        testing for its presence: what does not apply to a row is ``None``, or
        an empty list where the field is a list.
        Nothing is capped and nothing is hidden: the major and moderate issues
        are the required changes in ``rank`` order, and the minor ones are the
        suggestions. :mod:`peerreviewagents.issues` has the helpers that sort
        and group them, so a consumer renders the same order ``REVIEW.md``
        does. ``issues_source`` is ``editor`` where an editor pooled the
        findings and ``server`` where nobody did, in which case each finding is
        its own issue and the document's ``issues_source_reason`` says which of
        the four reasons it was: an arm that runs no editor, a ledger past the
        editor path's stated maximum, an editor slot nobody claimed, or an
        editor that claimed it and never closed a review.
        It is empty for a run written before 0.10.0, which reported a
        body and an appendix instead of issues.
    ``findings``
        Every finding the ledger kept, de-duplicated to the latest row per
        ``finding_uid``, in full. The ledger is the finding set: the editor
        places nothing out of sight, so a finding is in the review through the
        issue that holds it. Each one says where it is in the manuscript the
        same way an issue member does — ``document``, ``line``, ``end_line``,
        ``start``, ``end`` and ``anchor_text``, where

            ``anchor_text == base.line_text(line, document)[start:end]``

        for the quote inside one line, and
        :func:`peerreviewagents.cited_text` reads it for a quote that crosses a
        line break too, so a viewer underlines the words without searching the
        line for them again. ``findings_source`` is ``issues`` when
        ``issues.json`` could be read, including the empty review of a
        manuscript nobody raised anything about and the empty review of one
        whose every finding the code check contradicted; ``ledger`` when it
        could not, or when it holds no issue over findings that are in the
        ledger and were not thrown out by the check — then what is here is the
        raw findings and not the review — and ``none`` for a run that holds
        neither.
    ``escalations``, ``escalation_rulings``
        The specialist findings that asked for a ruling, and the rulings given.
    ``identity``
        What was reviewed and what reviewed it: paper id, source hash, PDF
        path, arm, versions, ``provider`` — ``claude-code`` for every run this
        version writes — and ``code_hashes``, the manifest's digest of each
        plugin and engine file the run was served by, empty for a run that
        stopped before the manuscript was imported. ``models`` is which model
        each slot was asked to run and ``reviewer_models`` the ordered list the
        core reviewers were drawn from — both empty for a run written before
        0.9.0, which pinned one model per slot in the plugin agent files.
        ``models_unverified`` names the slots that were assigned a model and
        never reported running one: the review is still publishable, because
        nothing was seen to disagree with the plan, but for those slots the
        plan is all there is. A slot that ran a *different* model family is a
        different matter and is in ``problems`` and
        ``not_publishable_reasons``.
    ``manuscript``
        What state the run's manuscript workspace is in, which is a different
        question from the run's status: ``state`` is one of
        :data:`peerreviewagents.MANUSCRIPT_STATES` — ``absent`` for a run that
        stopped before the import, ``draft`` for a converted paper the Read
        stage never sealed, so it has no line numbers and nothing can cite it,
        ``sealed-by-reader`` for a base a model read the pages of, and
        ``sealed-without-read`` for one written with no page read at all.
        ``sealed`` is that question already answered. ``reason`` says why
        nothing read the pages, in the words of whatever wrote the manifest,
        and is empty when one did. ``reader_model`` is the model that read
        them, or that wrote an application's own ``ai`` pieces.
        ``lost_pages`` is each page the converter returned nothing from, with
        the document and lines its transcription became, and ``documents`` is
        the file names the base holds.
    ``code``
        Whether this run read the code behind the paper, and what it read.
        ``reviewed`` is false for every run that was given none — which is most
        of them, and for every run written before 0.10.0 — and the rest of the
        block is then empty rather than absent. Where it is true: ``kind`` is
        ``git-clone`` or ``folder``, ``source`` is where the code came from as
        the caller named it, ``commit``, ``branch`` and ``dirty`` are the state
        it was in, and ``snapshot`` is the folder under the run that holds the
        text of it. ``files_in_snapshot`` and ``files_skipped`` are what was
        taken and what was left out — a binary, a data file, one over the size
        cap, a file a repository does not track — ``truncated`` says in words
        where the snapshot stopped early and is empty when it did not,
        ``source_cut`` is how many program source files a cap left out and is
        the one number that limits what the review may claim, and
        ``manifest`` is the snapshot's own
        manifest, which names every file either way. ``files_opened``,
        ``opened`` and ``searches`` are what a reviewer actually did with it: a
        lane that opened no file read the paper and nothing else, and a reading
        that said only "code: reviewed" could not have told you so.
    ``code_check``
        What the code lane's second pass answered, and ``None`` on a run that
        was given no code or that is older than the lane. A finding that
        asserts how something was computed, sampled, filtered or selected goes
        to it between the reviewers and the synthesis and comes back
        ``confirmed``, ``contradicted`` or ``not-shown``:
        :data:`peerreviewagents.names.CODE_CHECK_VERDICTS` is the three words.
        ``cap`` is the most findings the check is handed, ``checked`` how many
        it answered, ``over_cap`` how many qualified past the cap and were left
        exactly as their reviewers filed them, ``counts`` the tally per verdict,
        and ``checks`` one row per answer — the finding it is about, the
        ``code_evidence`` that decides it (verified against the snapshot before
        it was stored) or the ``searches`` behind a ``not-shown``, and the
        ``note`` written for the authors. **A contradicted finding is in no
        issue**: it is in the ledger, in this block and in the review's own
        thrown-out section, so a consumer that counts required changes must not
        count it and a consumer that reports what the review threw out reads it
        here rather than opening ``issues.json`` itself.
    ``startup``
        The start-up probe: ``probe`` is the harness's own init event, the
        plugins, MCP servers, agents and commands the session reported, or
        ``None`` when no session ever reached it, and ``problems`` is what the
        probe refused to launch over. ``startup_problems`` is that same list at
        the top level, kept for callers that already read it.
    ``agents``
        One row per agent slot: role, track, ``model`` — what the agent was
        observed running as, and ``None`` when nothing observed it, which means
        *not recorded* and never *what it was asked to run* — ``assigned_model``,
        what the run asked that slot to run, server and session tool calls,
        tokens, state. Empty for a run that never claimed a slot.
    ``reports``
        Absolute paths to what exists on disk — ``REVIEW.md``, ``REVIEW.txt``,
        each ``audit_<track>.md``, ``issues.json``, and ``editor_log`` when the
        editor's submission trace is there — and nothing else.

    Raises :class:`NotARunFolder` for a folder that is not a run, and
    ``ValueError`` for a symlink anywhere under the review folder. Nothing else
    is raised for anything on disk.
    """
    root, review = locate(path)
    refuse_symlinks(review)
    problems: list[str] = []
    protocol = _mapping(review / 'protocol.json', problems)
    manifest = _mapping(review / 'manifest.json', problems)
    progress = _mapping(review / 'progress.json', problems) or {}
    runtime = _mapping(review / 'runtime.json', problems) or {}
    startup = _mapping(review / 'startup.json', problems) or {}
    workspace = root / MANUSCRIPT_DIRECTORY
    _no_symlink(workspace / 'manifest.json')
    paper = _mapping(workspace / 'manifest.json', problems)

    state = run_status(progress, runtime, startup, problems)
    status, complete = state['status'], state['status'] in COMPLETE_STATUSES

    document_problems: list[str] = []
    document = _mapping(review / 'issues.json', document_problems) or {}
    problems += document_problems
    if document_problems:
        # Falling back to the ledger here is right — the findings are real —
        # but the raw findings are not the review, and a consumer that is not
        # told cannot know the difference.
        problems.append('issues.json is present but could not be read, so what is below is the '
                        'raw findings from the ledger and not the review')
    # The stamp is checked, not just written. An issues document from an engine
    # this version does not read is not a document to read optimistically: the
    # ids, the statuses and the member shape in it may not mean what this
    # version would take them for. The findings in the ledger are still real,
    # so they are what is reported, with the version named as the reason.
    stamp_problem = issue_model.schema_problem(document) if document else None
    if stamp_problem:
        problems.append(stamp_problem + ', so what is below is the raw findings from the ledger '
                        'and not the review')
        document = {}
    # What the code check answered, read before the findings because one of its
    # answers decides what a ledger with no issue over it means.
    code_check = _code_check_reading(review, document, progress, problems)
    # The ledger is the finding set. Nothing is placed out of sight any more, so
    # a finding reaches the review through the issue that holds it, and the two
    # readings never disagree about how much was found.
    findings = dedupe(_rows(review / 'findings.jsonl', problems))
    # Every field of the document whose type this version cannot read, named
    # one by one. A reader that took the shape on trust crashed instead of
    # reporting it: a `members` held as a string was iterated one character at
    # a time into `str.get` and raised an AttributeError out of this function,
    # over a file that was merely malformed. The readers below skip what they
    # cannot read, these lines say what was skipped, and a run with a damaged
    # document reads as not publishable with the field named.
    problems += issue_model.field_problems(document)
    issue_rows = issue_model.rows_of(document)
    if issue_rows:
        source = 'issues'
    elif document and not document_problems:
        # An issues document that holds no issue is a review of a manuscript
        # nothing was raised about, which is a reading, not a damaged file. A
        # ledger with findings in it and no issue over any of them is the other
        # thing: the review does not account for what was found, so what is
        # here is the raw findings rather than the review.
        #
        # A finding the code check contradicted is the third case, and it is an
        # accounted-for finding: the review is refused if an issue holds one,
        # and the review's own thrown-out section is where it is reported. A
        # run whose only finding was disproved by the authors' own code is a
        # finished review with no surviving problem in it — the first one read
        # as raw findings and as not publishable, with nothing wrong in it at
        # all.
        thrown_out = {str(row.get('finding_uid'))
                      for row in (code_check or {}).get('checks', ())
                      if row.get('verdict') == 'contradicted'}
        unaccounted = [one for one in findings
                       if str(one.get('finding_uid')) not in thrown_out]
        source = 'ledger' if unaccounted else 'issues'
    else:
        source = 'ledger' if findings else 'none'
    if not findings:
        # A run whose ledger is gone still has the issues' own member rows, and
        # they carry the uid, the slot, the quote, the cited lines and the
        # offsets within them — which is why the offsets are on a member: this
        # path reports members as findings, and a consumer that highlights a
        # finding must not get less here than it gets from the ledger.
        # Through the same guard as every other reader of this document: a
        # `members` held as a number was iterated here and raised a TypeError
        # out of `read_run` on the one path that had no ledger to fall back on.
        findings = dedupe([dict(member) for row in issue_rows
                           for member in issue_model.members_of(row)])
    rulings = issue_model.objects_in(document.get('escalation_rulings'))
    quarantined = _rows(review / 'quarantined.jsonl', problems)
    guard = document.get('review_guard_status') or progress.get('review_guard_status')
    issue_counts = issue_model.counts(issue_rows)

    audits = {name.split('audit_', 1)[1].removesuffix('.md'): str(review / name)
              for name in sorted(path.name for path in review.glob('audit_*.md'))}
    reports = {name: str(review / filename) for name, filename in
               (('markdown', 'REVIEW.md'), ('text', 'REVIEW.txt'),
                ('issues', 'issues.json'), ('editor_log', 'editor-log.jsonl'))
               if (review / filename).is_file()}
    reports['audits'] = audits
    # These two read the usage receipts and the starter's own accounting, which
    # a killed run leaves half-written as often as anything else, so they are
    # read before `publishable` is decided rather than inside the result.
    agents = _agent_rows(review, protocol, runtime, problems)
    usage = observed_usage(review, on_problem=problems.append)

    # What each slot was assigned against what it reported running. It is
    # computed before the reasons so that the "N file(s) could not be read in
    # full" count is the files, and appended to `problems` after, because a
    # substituted model is a problem with this run's record of itself.
    swapped = model_plan.substitutions(agents)
    # A citation worker's substitution is reported and does not withhold the
    # review: it writes no report and no finding, and every row of its table
    # that becomes one is re-read by the citations specialist first.
    worker_swapped = model_plan.worker_substitutions(agents)
    # The citation stage lines are deliberately not in `problems` yet: an
    # unfinished ledger is reported, not published over, and a ledger that
    # could not be read at all is the same answer rather than a stronger one.
    citations, citation_lines = _citations_reading(review, root=root, protocol=protocol,
                                                   agents=agents)
    # The synthesis is only a reason when there is something it synthesised: a
    # run of an editor-less arm that nobody raised anything in is a review of a
    # manuscript with no issues, which is a reading and not an ungrouped pile.
    reasons = publishable_reasons(status, source, guard, problems, len(quarantined), swapped,
                                  issues_source=(document.get('issues_source') if issue_rows
                                                 else None),
                                  issues_source_reason=document.get('issues_source_reason'))
    problems += swapped + worker_swapped + citation_lines

    probe = startup.get('probe')
    if probe is not None and not isinstance(probe, dict):
        problems.append(f"startup.json's probe holds a {type(probe).__name__} where the harness's "
                        'own start-up event belongs, so it is read as absent')
        probe = None

    return {
        'run': str(root),
        'review': str(review),
        'status': status,
        'complete': complete,
        'publishable': not reasons,
        'not_publishable_reasons': reasons,
        'blocked': status == BLOCKED,
        'cause': state['cause'],
        'note': state['note'],
        'stage': 'startup' if (startup.get('problems') and not runtime.get('result')
                               and not progress.get('stages')) else 'session',
        'findings': findings,
        'issues': issue_rows,
        'issues_source': document.get('issues_source'),
        'findings_source': source,
        'counts': {'findings': len(findings),
                   'issues': issue_counts['issues'],
                   'required': issue_counts['required'],
                   'suggestions': issue_counts['suggestions'],
                   'issue_members': issue_counts['issue_members'],
                   'by_severity': issue_counts['by_severity'],
                   'by_category': issue_counts['by_category'],
                   'quarantined': len(quarantined),
                   'findings_delivered': progress.get('findings_delivered'),
                   'findings_submitted': progress.get('findings_submitted')},
        'quarantined': quarantined,
        'citations': citations,
        'guard_status': guard,
        'escalations': [f for f in findings if f.get('escalate')],
        'escalation_rulings': rulings,
        'summary': document.get('summary'),
        'verdict': document.get('verdict'),
        'limitations': document.get('limitations') or [],
        'identity': _identity(manifest, protocol, paper,
                              (probe or {}).get('pdf'), problems, agents),
        'manuscript': _manuscript(workspace, paper),
        'code': _code_reviewed(progress, manifest, problems),
        'code_check': code_check,
        'stages': progress.get('stages') or (runtime.get('stages') if runtime else None),
        'runtime': {'wall_seconds': runtime.get('wall_seconds'),
                    'tokens_total': runtime.get('tokens_total'),
                    'rate_limits': runtime.get('rate_limits') or [],
                    'per_agent_token_ceiling': runtime.get('per_agent_token_ceiling'),
                    'per_run_token_ceiling': runtime.get('per_run_token_ceiling'),
                    'claude_code_version': runtime.get('claude_code_version'),
                    'recorded': runtime.get('recorded')},
        'agents': agents,
        'usage': usage,
        'reports': reports,
        'startup': {'probe': probe, 'problems': list(startup.get('problems') or [])},
        'startup_problems': list(startup.get('problems') or []),
        'problems': problems,
        'files': {name: str(review / name) for name in RUN_MARKERS
                  if (review / name).is_file()},
    }


def read_fix_check(path):
    """Everything a caller needs about one fix-check run folder, from its own files.

    The reading :func:`read_run` is for a review: one call, the same keys for
    every folder, whatever state it is in, and no exception for anything on
    disk. A fix check is a different kind of run — it raises nothing, so it has
    no findings, no issues, no guard and no editor — and this is its reading.

    ``status``
        One of :data:`peerreviewagents.names.RUN_STATUSES`, or ``unknown``,
        by the same rule :func:`run_status` applies to a review.
        ``complete-without-findings`` on a fix check means the check was
        written with no verdict in it: every problem it was given is reported
        as not checked.
    ``complete``, ``blocked``, ``cause``, ``note``
        As in :func:`read_run`: ``complete`` is true for both complete
        statuses, and ``cause`` is why a run that is not complete stopped.
    ``problems``
        The problems the check was given, as ``--problems`` held them: ``id``,
        ``statement``, ``severity``, the quotations of the *old* draft and the
        authors' reply. Empty for a folder whose check was never written and
        whose given list cannot be read. Not to be confused with
        ``file_problems``, which is this reading's own damage report — the
        fix check's vocabulary and the reader's collided, and the problems
        under review won the name.
    ``checks``
        One row per problem that was answered, in the order they were
        answered: ``problem_id``, ``verdict`` (one of
        :data:`peerreviewagents.names.FIX_VERDICTS`), ``statement``,
        ``severity``, ``author_reply``, ``old_evidence`` (each old quotation
        with whether it is still in the new draft), ``basis``,
        ``draft_evidence`` (the anchored quotation of the new draft, or
        ``None``), ``searches``, ``reason``, ``note`` and ``recorded``.
    ``counts``, ``checked``, ``not_checked``, ``over_cap``, ``cap``
        The check in numbers. ``counts`` is one key per verdict.
        ``not_checked`` is every problem with no verdict — the ones past the
        cap and the ones a checker never reached — and ``unchecked`` names
        them. A problem that was not checked is neither fixed nor unfixed, and
        the count is here so that nobody reads it as either.
    ``tokens``, ``agents``, ``usage``, ``runtime``
        What the check spent: the run's own total and ceilings, the per-slot
        rows :func:`read_run` reports, and the usage receipts behind them.
    ``reports``
        Absolute paths to what is on disk — ``fix-check.json``, ``FIXES.md``
        and the lane's own ``fix-checks.jsonl`` — and nothing else.
    ``file_problems``
        Every file that could not be read in full, named, one line each, plus
        one line for a folder that is a review rather than a fix check. Empty
        for a whole folder.

    Raises :class:`NotARunFolder` for a folder that is not a run, and
    ``ValueError`` for a symlink anywhere under the review folder. Nothing else
    is raised for anything on disk.
    """
    root, review = locate(path)
    refuse_symlinks(review)
    file_problems: list[str] = []
    progress = _mapping(review / 'progress.json', file_problems) or {}
    runtime = _mapping(review / 'runtime.json', file_problems) or {}
    startup = _mapping(review / 'startup.json', file_problems) or {}
    protocol = _mapping(review / 'protocol.json', file_problems)
    state = run_status(progress, runtime, startup, file_problems)

    document = _mapping(review / FIX_CHECK_FILE, file_problems) or {}
    stamped = str(document.get('schema_version') or '')
    if document and stamped != FIX_CHECK_SCHEMA_VERSION:
        # The same rule the review reading applies to issues.json: a document
        # from an engine this version does not read is not read optimistically.
        # The lane's own append-only file is still real, so it is what is
        # reported, with the version named as the reason.
        file_problems.append(f'{FIX_CHECK_FILE} is stamped {stamped or "nothing"} and this '
                             f'version reads {FIX_CHECK_SCHEMA_VERSION}, so what is below is the '
                             "lane's own rows and not the written check")
        document = {}
    # The written document first, then the lane's file for a run that stopped
    # before anything was written. Either way the counts are taken over the
    # rows that come back, so a consumer's tally and the rows cannot disagree.
    if document:
        checks = [dict(row) for row in (document.get('checks') or []) if isinstance(row, dict)]
        record = document
    else:
        # One row per problem, latest wins: the lane's file is append-only like
        # every other ledger here, and a retried slot can write a second row
        # for a problem whose first answer was lost.
        rows, seen = [], {}
        for row in _rows(review / 'fix-checks.jsonl', file_problems):
            key = str(row.get('problem_id'))
            if key not in seen:
                rows.append(key)
            seen[key] = row
        checks = [seen[key] for key in rows]
        record = progress.get('fix_check') if isinstance(progress.get('fix_check'), dict) else {}
    if not record and not checks and progress.get('fix_check') is None:
        file_problems.append('this run folder holds no fix check: it is a review, and read_run '
                             'is the reading for one')
    given = [dict(one) for one in (document.get('given') or []) if isinstance(one, dict)]
    answered = {str(row.get('problem_id')) for row in checks}
    unchecked = ([dict(one) for one in (document.get('unchecked') or [])
                  if isinstance(one, dict)]
                 or [{'id': one.get('id'), 'statement': one.get('statement'),
                      'severity': one.get('severity')}
                     for one in given if str(one.get('id')) not in answered])
    cap = record.get('cap')
    total = record.get('problems')
    agents = _agent_rows(review, protocol, runtime, file_problems)
    reports = {name: str(review / filename) for name, filename in
               (('fix_check', FIX_CHECK_FILE), ('markdown', 'FIXES.md'),
                ('checks', 'fix-checks.jsonl'))
               if (review / filename).is_file()}
    return {
        'run': str(root),
        'review': str(review),
        'status': state['status'],
        'complete': state['status'] in COMPLETE_STATUSES,
        'blocked': state['status'] == BLOCKED,
        'cause': state['cause'],
        'note': state['note'],
        'paper_id': document.get('paper_id') or (protocol or {}).get('paper_id'),
        'title': document.get('title'),
        'arm': document.get('arm') or (protocol or {}).get('arm'),
        'cap': cap if isinstance(cap, int) and not isinstance(cap, bool) else None,
        'problems': given,
        'checks': checks,
        'checked': len(checks),
        'not_checked': len(unchecked) if (given or document) else _count(record.get('not_checked')),
        'unchecked': unchecked,
        'over_cap': _count(record.get('over_cap')),
        'counts': {verdict: sum(1 for row in checks if row.get('verdict') == verdict)
                   for verdict in FIX_VERDICTS},
        'problems_given': total if isinstance(total, int) and not isinstance(total, bool)
        else len(given) or None,
        'tokens': runtime.get('tokens_total'),
        'runtime': {'wall_seconds': runtime.get('wall_seconds'),
                    'tokens_total': runtime.get('tokens_total'),
                    'rate_limits': runtime.get('rate_limits') or [],
                    'per_agent_token_ceiling': runtime.get('per_agent_token_ceiling'),
                    'per_run_token_ceiling': runtime.get('per_run_token_ceiling'),
                    'claude_code_version': runtime.get('claude_code_version'),
                    'recorded': runtime.get('recorded')},
        'agents': agents,
        'usage': observed_usage(review, on_problem=file_problems.append),
        'reports': reports,
        'written': document.get('written'),
        'file_problems': file_problems,
    }


def read_citation_ledger(path, source_text=False):
    """The citation ledger's own rows, for a caller that needs more than counts.

    :func:`read_run` reports the ledger in numbers, which is what a page that
    shows a review needs. A caller that wants the table itself — which
    reference resolved to which record, which in-text use was judged how —
    had only ``peerreviewagents.engine.citation_ledger`` to reach for, and the
    engine is not a public module: every consumer that went there
    re-implemented the one rule the file has, that the last row written for a
    ``row_id`` is that row. This is that reading, kept beside the writer.

    ``path`` is a run folder or the review folder inside one, the same two
    spellings :func:`read_run` takes. Returns:

    ``run``, ``review``, ``ledger``
        The folders, and the absolute path of ``citations/ledger.jsonl`` or
        ``None`` when this run kept no table.
    ``exists``
        Whether the file is there. A run from an arm without the citations
        lane keeps no table, which is not a damaged run.
    ``references``, ``uses``
        The rows, the last one written for each ``row_id``, in **natural**
        ``row_id`` order — a run of digits is compared as the number it is, so
        ``ref:r10`` comes after ``ref:r2`` and ``use:paper.md:11:10:r1`` after
        ``use:paper.md:11:2:r1``, which is the order a reference list is
        printed in and the
        order every consumer was re-sorting into. Two readings of the same
        folder agree. A reference row
        carries the printed entry and, once the lane looked it up, its
        ``resolution``; a use row carries the in-text marker, the reference it
        points at and the verdict a worker gave it.
    ``counts``, ``complete``, ``summary``, ``problems``
        Exactly what ``read_run()['citations']`` holds, from the same
        functions and the same reading of what the arm planned, so the two
        cannot disagree — including about a ``protocol.json`` that records the
        citation slots and no plan.

    **Source text is left out by default.** A resolved reference carries the
    abstract and the opening of the full text that a worker was shown, which
    is most of the bytes in the file and is someone else's copyrighted prose. A
    row that had them comes back without them and with
    ``resolution['has_source_text']`` true instead, so a caller can tell a
    reference nobody fetched text for from one whose text was dropped here.
    Pass ``source_text=True`` for the rows exactly as stored.

    The guards are :func:`read_run`'s. A symlinked ``review/``, ``citations/``
    or ``ledger.jsonl`` is refused by name rather than followed, because the
    server writes them in place and a link in their place points at a file
    another process owns. Damaged content never raises: a line that is not
    readable JSON, or whose shape the ledger cannot hold — see
    ``citation_ledger._readable`` — is skipped and counted in
    ``counts['unreadable_rows']``, with a line in ``problems`` saying so, and a
    ledger this process may not open at all comes back as no rows, ``exists``
    true and the same ``problems`` line :func:`read_run` gives it — the one
    citation state where nothing can be counted is the one a consumer page is
    most likely to meet, and it must not be the one that raises. Below both
    sits a last guard: whatever a file does to the counting or to the building
    of the rows, including an exception neither this module nor that one
    foresaw, both readers give that same sentence rather than raise.
    :class:`NotARunFolder` for a folder that is not a run and ``ValueError``
    for a symlink are the only two raises.
    """
    root, review = locate(path)
    _no_symlink(review, review)
    folder = review / citation_ledger.LEDGER_DIRECTORY
    if folder.is_dir() or folder.is_symlink():
        refuse_symlinks(folder, review)
    ledger = citation_ledger.Ledger(review)
    citations, _ = _citations_reading(review, root=root)
    empty = {'reference': [], 'use': []}
    if citations['ledger'] and not citations['counts']:
        # The file is there and the counting could not read it. The rows are
        # empty for the same reason the counts are, and reading it a second
        # time here to find that out again would only risk a different answer.
        return _reading(root, review, ledger, empty, citations)
    try:
        kept = _kept_rows(ledger, source_text)
    except Exception as exc:  # noqa: BLE001 — the same last guard _citations has.
        # A file that is there and cannot be read into rows, for any reason at
        # all rather than the three a damaged file is expected to give. The
        # counting met the same file and has its own guard, so the answer here
        # is the answer it gives: no rows, no counts, and the one line saying
        # the file is present and could not be read. A consumer printing this
        # table must not be handed a raise by a run folder somebody tore.
        kept = empty
        citations, _ = _unreadable(review, root, exc)
    return _reading(root, review, ledger, kept, citations)


def _reading(root, review, ledger, kept, citations):
    """:func:`read_citation_ledger`'s answer, assembled in one place.

    The rows and the counts come back from two readings of one file, and this
    is where they are put together, so a degraded answer cannot be assembled
    one way and a whole one another.
    """
    return {
        'run': str(root),
        'review': str(review),
        'ledger': str(ledger.path) if ledger.exists() else None,
        'exists': ledger.exists(),
        'references': kept['reference'],
        'uses': kept['use'],
        'counts': citations['counts'],
        'complete': citations['complete'],
        'summary': citations['summary'],
        'problems': citations['problems'],
    }


def _natural(row_id):
    """A sort key that reads a run of digits in ``row_id`` as the number it is.

    Row ids are built from counters — ``ref:r2``, ``ref:r10``,
    ``use:paper.md:11:2:r1``, ``use:paper.md:11:10:r1`` — and sorting them as
    text puts the
    tenth reference second, because ``'1'`` is before ``'2'``. Every consumer
    that printed the table in order sorted it again, so the order the ledger
    hands out is the order it is printed in: digits compared as numbers,
    everything else as text.

    Each part is tagged with its kind so that no comparison ever puts a number
    beside a string, whatever a row id holds.

    A run of digits longer than Python will convert — 4,300 of them, the
    interpreter's own limit — is compared as text instead. Nothing the server
    writes is anything like that long, and a sort key is the last place a
    reading of a damaged file should raise.
    """
    parts = []
    for part in re.split(r'(\d+)', row_id):
        try:
            parts.append((1, int(part)) if part.isdigit() else (0, part))
        except ValueError:
            parts.append((0, part))
    return tuple(parts)


def _without_source_text(row):
    """``row`` with the fetched abstract and full text replaced by a flag.

    A copy, because the caller is handed these rows and the ledger's own
    reading is not this function's to edit.
    """
    resolution = row.get('resolution')
    if not isinstance(resolution, dict):
        return row
    if not (resolution.get('abstract') or resolution.get('full_text')):
        return {**row, 'resolution': {**resolution, 'has_source_text': False}}
    kept = {name: value for name, value in resolution.items()
            if name not in ('abstract', 'full_text')}
    return {**row, 'resolution': {**kept, 'has_source_text': True}}


__all__ = ['read_run', 'read_fix_check', 'read_citation_ledger', 'locate', 'dedupe', 'run_status',
           'cause_of',
           'stage_gap_line', 'refuse_symlinks', 'NotARunFolder', 'RUN_MARKERS', 'UNKNOWN']
