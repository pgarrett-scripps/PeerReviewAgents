"""Read one run folder: :func:`read_run`, the supported way into a finished run.

An application that shows a review has to answer four questions about a folder
on disk — did it finish, why did it stop, what did it find, and what did it
cost — and until 0.9 the only way to answer them was to open
``editor-package.json``, ``findings.jsonl``, ``runtime.json``,
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
from pathlib import Path

from .engine.usage import agent_rows, observed_usage
from .names import (
    BLOCKED,
    COMPLETE,
    COMPLETE_STATUSES,
    COMPLETE_WITHOUT_FINDINGS,
    MANUSCRIPT_DIRECTORY,
    RUN_DIRECTORY,
    RUN_STATUSES,
)

# The one module that decides which model a slot runs also decides what counts
# as a different model, which is what this reader has to compare.
from .plugin import models as model_plan

#: The files that make a folder a review folder. A run that was killed before
#: the server wrote anything still has ``startup.json`` from the probe.
RUN_MARKERS = ('protocol.json', 'progress.json', 'runtime.json', 'startup.json',
               'findings.jsonl', 'editor-package.json')

#: The status reported for a folder whose status file was never written, or
#: whose status is a word this version does not know.
UNKNOWN = 'unknown'


class NotARunFolder(ValueError):
    """The path is not a run folder and not the review folder inside one."""


def _refusal(path):
    return ValueError(f'Refusing to read {Path(path).name}: a run file must not be a symlink '
                      f'({path})')


def _no_symlink(path):
    """``path`` itself, or a refusal naming it. A run file is never a link."""
    path = Path(path)
    if path.is_symlink():
        raise _refusal(path)
    return path


def refuse_symlinks(review):
    """Refuse the review folder, or anything in it, being a symlink.

    One walk, so a symlinked ``review/`` directory, a symlinked
    ``usage/core-1/usage.jsonl`` and a symlinked ``audit_stats.md`` are all
    refused the same way and by name, rather than two of them being followed
    and the third dropped without a word. The walk does not descend through a
    symlinked directory, so a link that points at its own parent is named, not
    followed round.
    """
    review = _no_symlink(review)
    for folder, folders, files in os.walk(review, followlinks=False):
        for name in sorted(folders) + sorted(files):
            path = Path(folder) / name
            if path.is_symlink():
                raise _refusal(path)
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
        problems.append(f'{label} could not be read ({exc.strerror or exc}); it is read as absent')
        return None
    except UnicodeDecodeError:
        problems.append(f'{label} is not valid UTF-8; it is read as absent')
        return None
    try:
        value = json.loads(text)
    except ValueError:
        problems.append(f'{label} is not valid JSON, which is what a process killed mid-write '
                        'leaves; it is read as absent')
        return None
    if not isinstance(value, dict):
        problems.append(f'{label} holds a {type(value).__name__} where a JSON object belongs; '
                        'it is read as absent')
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
        problems.append(f'{label} could not be read ({exc.strerror or exc}); it is read as empty')
        return []
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('utf-8', errors='replace')
        problems.append(f'{label} is not valid UTF-8; the bytes that are not were replaced, so '
                        'a quotation from it may not be the bytes the agent wrote')
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
        problems.append(f'{skipped} unreadable line(s) in {label} were skipped; a run killed '
                        'mid-write leaves one')
    return rows


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
                        'file to digest belongs; it is read as empty')
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
                        'belongs; it is read as absent')
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


def _identity(manifest, protocol, paper, pdf_from_startup=None, problems=None):
    """Who the run reviewed, from the files that say so rather than from a name."""
    manifest, protocol, paper = manifest or {}, protocol or {}, paper or {}
    problems = problems if problems is not None else []
    plan = manifest.get('plan') or protocol.get('plan') or {}
    plan = plan if isinstance(plan, dict) else {}
    return {
        'paper_id': manifest.get('paper_id') or protocol.get('paper_id') or paper.get('id'),
        'source_sha256': manifest.get('source_sha256') or paper.get('source_sha256'),
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
        'literature_cutoff': protocol.get('literature_cutoff'),
        'workflow_version': manifest.get('workflow_version') or protocol.get('version'),
        'finding_schema_version': (manifest.get('finding_schema_version')
                                   or protocol.get('finding_schema_version')),
        'claude_code_version': manifest.get('claude_code_version'),
        'provider': _provider(protocol, problems),
        'code_hashes': _code_hashes(manifest.get('code'), problems),
    }


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
                        + ', '.join(RUN_STATUSES) + '; it is reported as unknown')
        return UNKNOWN
    return text


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
    complete run and the sentence from :func:`cause_of` otherwise.
    """
    progress, runtime, startup = progress or {}, runtime or {}, startup or {}
    problems = problems if problems is not None else []
    written = _known(progress.get('status'), 'progress.json', problems)
    ended = _known(runtime.get('status'), 'runtime.json', problems)
    cause = cause_of(runtime, progress, startup)
    note = None
    if written in COMPLETE_STATUSES:
        status = written
        if ended == BLOCKED and cause:
            note = ('the review was written and the run was marked blocked afterwards: ' + cause)
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
                        substitutions=()):
    """Why this run is not publishable, in words, or an empty list when it is.

    One rule, in one place, so that ``publishable`` and the reasons can never
    disagree: ``publishable`` is this list being empty.

    A quarantined finding is not one of the reasons. The review guard exists to
    throw out a finding whose quote it could not match to the block it names,
    and a guard that did that has done its job — what is left is a whole review
    minus one bad quote. Stage A found the cost of the other rule: a correct
    catch of a planted error was quarantined for a paraphrased quote, and the
    run it came from read as not publishable with nothing at all wrong in it.

    **A review with no findings in it.** ``complete-without-findings`` is the
    status for an editor package that delivered nothing, and two different runs
    end that way. If the guard quarantined findings, the reviewers did raise
    something and it is not in the review, so there is nothing publishable to
    show and the reason says how many were held. If nothing was quarantined, the
    reviewers read the manuscript and raised nothing — "we found nothing
    make-or-break" is a review, and a run that reached it with an editor package
    and a clean guard is publishable like any other.

    What does stop a run from being publishable is the review not being there to
    show: a status that is not ``complete``, a review emptied by the guard,
    findings that come from the ledger instead of the editor's package, no
    editor package at all, no guard verdict, or a file that could not be read in
    full.

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
        reasons.append("the findings come from the ledger rather than from the editor's "
                       'editor-package.json, so they are not the editor\'s own review')
    elif findings_source == 'none' and status in COMPLETE_STATUSES:
        reasons.append('the run holds no findings and no editor package')
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
        findings in it: either the reviewers raised nothing, or the guard
        quarantined everything they did raise. ``counts['quarantined']`` is
        which of the two.
    ``complete``, ``blocked``
        The two questions asked of a status, already answered. ``complete`` is
        true for ``complete`` and for ``complete-without-findings``, so a run
        that delivered no findings is finished rather than failed.
    ``publishable``, ``not_publishable_reasons``
        True only when the whole review is here to show: status ``complete``,
        the findings from a readable ``editor-package.json``, a guard that
        recorded a verdict, and nothing in ``problems``. When it is false,
        ``not_publishable_reasons`` says why in words, one entry per reason, and
        it is never false with nothing named. A quarantined finding is not a
        reason: the guard threw out a finding it could not anchor, which is the
        guard working, and what is left is publishable. ``quarantined`` says
        what was thrown out, and whether that mattered is a judgement for a
        person rather than a flag. A review with no findings in it is
        publishable when the reviewers raised nothing and the guard held
        nothing, and is not when the guard emptied it. A slot that reported
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
        Every file that could not be read in full, named, and every slot whose
        observed model is a different family from the one it was assigned.
        Empty for a whole folder. A reading with problems is still a reading —
        that is the point — but a consumer should not publish it or quote a
        number from it without saying so.
    ``stage``
        ``startup`` when no session ever ran and no subscription usage was
        spent, ``session`` once one did.
    ``findings``, ``body``, ``appendix``
        The findings, de-duplicated to the latest row per ``finding_uid``, from
        ``editor-package.json`` when the editor wrote one and from the ledger
        (``findings.jsonl``) when it did not. ``findings_source`` says which,
        because a ledger reading has no editor placement: everything the server
        kept is in ``body`` and ``appendix`` is empty. A uid the editor put in
        both lists is in ``body`` only, so ``findings`` holds each finding once.
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
        each ``audit_<track>.md``, ``editor-package.json`` — and nothing else.

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
    _no_symlink(workspace / 'paper.json')
    paper = _mapping(workspace / 'paper.json', problems)

    state = run_status(progress, runtime, startup, problems)
    status, complete = state['status'], state['status'] in COMPLETE_STATUSES

    package_problems: list[str] = []
    package = _mapping(review / 'editor-package.json', package_problems) or {}
    damaged = bool(package_problems)
    problems += package_problems
    if damaged:
        # Falling back to the ledger here is right — the findings are real —
        # but the fallback is not the editor's review, and a consumer that is
        # not told cannot know the difference.
        problems.append('editor-package.json is present but could not be read, so the findings '
                        'below come from the ledger and are not the editor\'s own review')
    if package.get('findings') is not None or package.get('appendix') is not None:
        for key in ('findings', 'appendix'):
            # A string or a number here is a package this version cannot read,
            # and dropping that list in silence understates the review.
            if package.get(key) is not None and not isinstance(package.get(key), list):
                problems.append(f"editor-package.json's {key} is not a list, so the findings in "
                                'it could not be read')
        body = dedupe([f for f in package.get('findings') or [] if isinstance(f, dict)])
        appendix = dedupe([f for f in package.get('appendix') or [] if isinstance(f, dict)])
        source = 'editor-package'
    else:
        body = dedupe(_rows(review / 'findings.jsonl', problems))
        appendix = []
        source = 'ledger' if body else 'none'
    # A uid the editor placed in both lists was one finding, and counting it
    # twice would overstate the review and double it in `escalations`.
    in_body = {f.get('finding_uid') for f in body if f.get('finding_uid')}
    appendix = [f for f in appendix if f.get('finding_uid') not in in_body]
    findings = body + appendix
    rulings = [r for r in package.get('escalation_rulings') or [] if isinstance(r, dict)]
    quarantined = _rows(review / 'quarantined.jsonl', problems)
    guard = package.get('review_guard_status') or progress.get('review_guard_status')

    audits = {name.split('audit_', 1)[1].removesuffix('.md'): str(review / name)
              for name in sorted(path.name for path in review.glob('audit_*.md'))}
    reports = {name: str(review / filename) for name, filename in
               (('markdown', 'REVIEW.md'), ('text', 'REVIEW.txt'),
                ('editor_package', 'editor-package.json'))
               if (review / filename).is_file()}
    reports['audits'] = audits
    # These two read the usage receipts and the starter's own accounting, which
    # a killed run leaves half-written as often as anything else, so they are
    # read before `publishable` is decided rather than inside the result.
    agents = agent_rows(review, protocol, runtime=runtime, on_problem=problems.append)
    usage = observed_usage(review, on_problem=problems.append)

    # What each slot was assigned against what it reported running. It is
    # computed before the reasons so that the "N file(s) could not be read in
    # full" count is the files, and appended to `problems` after, because a
    # substituted model is a problem with this run's record of itself.
    swapped = model_plan.substitutions(agents)
    reasons = publishable_reasons(status, source, guard, problems, len(quarantined), swapped)
    problems += swapped

    probe = startup.get('probe')
    if probe is not None and not isinstance(probe, dict):
        problems.append(f"startup.json's probe holds a {type(probe).__name__} where the harness's "
                        'own start-up event belongs; it is read as absent')
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
        'body': body,
        'appendix': appendix,
        'findings_source': source,
        'counts': {'findings': len(findings), 'body': len(body), 'appendix': len(appendix),
                   'quarantined': len(quarantined),
                   'findings_delivered': progress.get('findings_delivered'),
                   'findings_submitted': progress.get('findings_submitted')},
        'quarantined': quarantined,
        'guard_status': guard,
        'escalations': [f for f in findings if f.get('escalate')],
        'escalation_rulings': rulings,
        'summary': package.get('summary'),
        'verdict': package.get('verdict'),
        'limitations': package.get('limitations') or [],
        'identity': _identity(manifest, protocol, paper,
                              (probe or {}).get('pdf'), problems),
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


__all__ = ['read_run', 'locate', 'dedupe', 'run_status', 'cause_of', 'refuse_symlinks',
           'NotARunFolder', 'RUN_MARKERS', 'UNKNOWN']
