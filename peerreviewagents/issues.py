"""The issue: the unit a review is reported in, and the shape ``issues.json`` holds.

A finding is what one reviewer filed about one passage. An **issue** is what the
editor says the paper has to answer for, and it is the unit the review is
written in: a severity, a category, a rank, one paragraph of statement, and the
findings that evidence it kept as members. "Raised by N" is not a number the
editor asserts — it is counted here, from the distinct reviewer slots among an
issue's members.

Nothing in a review is capped or hidden. Major and moderate issues are the
**required changes**, ranked against each other; minor issues are
**suggestions**, grouped by category. There is no body, no appendix and no
duplicate placement: two reviewers who found the same thing are two members of
one issue, which is what ``raised_by`` reports.

This module imports nothing from the rest of the package. The server builds
``issues.json`` with it, the engine's renderer reads an issues document with it,
and :func:`peerreviewagents.runs.read_run` returns what it describes, so the
writer, the renderer and the reader cannot drift apart.
"""

from __future__ import annotations

import re

#: The schema every ``issues.json`` is stamped with, in its ``schema_version``.
ISSUES_SCHEMA_VERSION = 'review-issues-v3'

#: Every schema this version can read. A stamp is only worth writing if someone
#: checks it: :func:`schema_problem` is that check, and the reader, the prior-round
#: loader and the renderer all go through it rather than each deciding for itself.
#: ``v2`` is where evidence became lines in the manuscript instead of block ids,
#: and a ``v1`` file is refused rather than read as though its ids meant
#: something here: nothing in this release can turn one into the other.
#: ``v3`` added the offsets of the quote within its line to every member, which
#: is additive: a ``v2`` file is read as it stands, with those fields missing
#: rather than wrong, which is why a prior round's file still loads.
KNOWN_ISSUES_SCHEMAS = ('review-issues-v2', 'review-issues-v3')

#: An issue's severity. The same three words a finding carries, because an
#: issue's severity is the editor's judgement of the same scale.
ISSUE_SEVERITIES = ('major', 'moderate', 'minor')

#: The severities that make an issue a required change, in report order.
REQUIRED_SEVERITIES = ('major', 'moderate')

#: The severities that make an issue a suggestion.
SUGGESTION_SEVERITIES = ('minor',)

#: What an issue is about. A finding carries no category — ``kind`` and
#: ``severity`` are all it has — so the category is the editor's own word, and
#: a fixed vocabulary is what makes "suggestions, grouped by category"
#: comparable between two runs of the same paper.
ISSUE_CATEGORIES = (
    'claims-and-conclusions',
    'statistics-and-inference',
    'methods-and-reporting',
    'results-and-numbers',
    'figures-and-tables',
    'literature-and-citations',
    'reproducibility',
    'presentation',
)

#: Where an issue stands against the prior round named at ingest.
#:
#: ``new``
#:     This round is the first to raise it.
#: ``persisting``
#:     The editor matched it to a prior issue and the problem still stands.
#: ``regressed``
#:     Matched, and worse than it was.
#: ``resolved``
#:     The editor ruled the prior issue fixed, by name and with a reason. It is
#:     reported in the round it resolved and carried no further.
#: ``unverified``
#:     The editor did not raise it again and did not rule it fixed. It is still
#:     open: a finding can vanish from text nobody touched, which Stage A
#:     recorded happening, so "nobody filed it this time" is not evidence of a
#:     fix and never becomes ``resolved`` by itself.
ISSUE_STATUSES = ('new', 'persisting', 'regressed', 'resolved', 'unverified')

#: The statuses an issue the editor matched to a prior one may carry.
MATCHED_STATUSES = ('persisting', 'regressed')

#: The statuses the editor may rule a prior issue it did not raise again. There
#: is no third option and no default: see :func:`peerreviewagents.plugin.server.submit_editor`.
PRIOR_RULING_STATUSES = ('resolved', 'unverified')

#: The statuses of an issue that is not one of *this* round's required changes or
#: suggestions. A prior issue the editor closed out is reported against the prior
#: round, not among the issues the authors have to answer now.
CLOSED_STATUSES = ('resolved', 'unverified')

#: How many issues one ``submit_issues`` call may carry for an ordinary ledger,
#: and the floor :func:`issue_batch_limit` never goes below. The editor submits
#: in batches so that the work in one turn is bounded whatever the finding count
#: is; see :func:`peerreviewagents.plugin.server.submit_issues`.
MAX_ISSUE_BATCH = 12

#: The most ``submit_issues`` calls the editor's turn cap pays for. The batch
#: limit grows with the ledger so that this number holds: a bigger ledger means
#: bigger batches, not more calls, because the calls are the scarce thing — see
#: :data:`peerreviewagents.plugin.server.EDITOR_MAX_TURNS`.
MAX_ISSUE_BATCHES = 8

#: Which category a synthesised issue takes from the track that filed it. Used
#: only where there is no editor in the arm: one issue per finding, so that
#: every arm writes an ``issues.json`` and every consumer reads one shape.
TRACK_CATEGORY = {
    'core': 'claims-and-conclusions',
    'field': 'claims-and-conclusions',
    'stats': 'statistics-and-inference',
    'methods-reporting': 'methods-and-reporting',
    'numbers': 'results-and-numbers',
    'figures-tables': 'figures-and-tables',
    'citations': 'literature-and-citations',
}

#: The member fields an issue keeps as evidence. The quote and the lines it was
#: found on are kept on the member, so a reader of ``issues.json`` alone can
#: open ``paper.md`` at the line without opening ``findings.jsonl``.
#: ``document`` is empty for the manuscript and names the supplement otherwise.
#: ``fingerprint`` is the digest of the whole passage the quote was found in: a
#: later round reads it to recognise the same passage after the converter has
#: broken the lines somewhere else, which is the one thing line numbers cannot
#: survive.
#:
#: ``start``, ``end`` and ``anchor_text`` are where the quote sits *inside* that
#: line, and they are on the member for the same reason the line is: an app that
#: has ``issues.json`` knew which line to open and then had to search the line
#: for the words again to underline them — the same search the engine already
#: did, done a second time by code that cannot refuse. The rule they keep is the
#: one every citation in a run keeps::
#:
#:     anchor_text == base.line_text(line, document)[start:end]
#:
#: for the quote inside one line, and for a quote that crosses a line break
#: ``start`` is where it begins in ``line`` and ``end`` where it ends in
#: ``end_line``. :func:`peerreviewagents.cited_text` is that read, both cases.
#: ``anchor_text`` is the manuscript's own characters, so it is what a viewer
#: highlights; ``quote`` is what the reviewer typed, and may differ in a dash,
#: a quotation mark or a letter's case. All three are ``null`` on a member the
#: guard could not place, which is a finding with words and no position.
MEMBER_FIELDS = ('finding_uid', 'slot', 'track', 'severity', 'kind', 'basis', 'escalate',
                 'title', 'document', 'line', 'end_line', 'start', 'end', 'anchor_text',
                 'quote', 'evidence_lines', 'fingerprint')

#: Every field on every issue row of ``issues.json``, live or closed, edited or
#: synthesised. A field that does not apply to a row is ``null``, or ``[]``
#: where the field is a list — the keys themselves are always there, so a
#: consumer reads a field by name instead of testing for its presence, and
#: ``docs/INTEGRATIONS.md`` documents this list.
ISSUE_FIELDS = ('id', 'name', 'severity', 'category', 'rank', 'statement', 'members',
                'raised_by', 'rebuttal_line', 'rebuttal_quote', 'prior_id', 'prior_rank',
                'status', 'status_reason', 'text_unchanged_reason', 'surviving_quotes',
                'audits')


def issue_id(number: int) -> str:
    """The id minted for the *number*-th new issue of a run: ``i01``, ``i02``, …"""
    return f'i{int(number):02d}'


#: An issue id as this engine mints it: ``i`` and two digits or more. An id is
#: the one thing a later round matches against, so a reader of a prior round's
#: file checks the form rather than taking whatever the field holds.
ISSUE_ID_PATTERN = r'i\d{2,}'


def is_issue_id(value) -> bool:
    """True where *value* is an issue id in this engine's own form.

    Text, and :data:`ISSUE_ID_PATTERN`. ``5``, ``True`` and ``'7'`` are not
    ids: a round that read them as ids would mint an id an earlier round had
    already reported under, which is the one thing a stable id is for.
    """
    return isinstance(value, str) and re.fullmatch(ISSUE_ID_PATTERN, value) is not None


def issue_batch_limit(findings) -> int:
    """How many issues one ``submit_issues`` call may carry for this ledger.

    :data:`MAX_ISSUE_BATCH` for an ordinary run, and larger for a ledger big
    enough that twelve at a time would not fit the editor's turn cap. The worst
    case is one issue per finding, so the limit is sized to keep the number of
    batches at :data:`MAX_ISSUE_BATCHES` whatever the count: the editor's calls
    are what runs out, not its issues.
    """
    count = max(0, int(findings or 0))
    return max(MAX_ISSUE_BATCH, -(-count // MAX_ISSUE_BATCHES))


def schema_problem(document, label='issues.json'):
    """Why this issues document's ``schema_version`` cannot be read, or ``None``.

    One rule for every reader. A document stamped with a version this release
    does not know is refused by name rather than read as far as it happens to
    parse: the fields it holds may mean something else, and a review rendered
    from a half-understood file is worse than no review.
    """
    if not isinstance(document, dict):
        return f'{label} holds a {type(document).__name__} where the issues document belongs'
    stamp = document.get('schema_version')
    if stamp is None:
        return (f'{label} carries no schema_version, so it was written by an engine from before '
                'the issues document existed')
    if str(stamp) not in KNOWN_ISSUES_SCHEMAS:
        return (f'{label} is stamped {str(stamp)!r} and this version reads '
                + ', '.join(KNOWN_ISSUES_SCHEMAS))
    return None


def is_required(issue) -> bool:
    """True for a major or moderate issue: the ones that are required changes."""
    return str((issue or {}).get('severity')) in REQUIRED_SEVERITIES


def is_resolved(issue) -> bool:
    """True for a prior-round issue the editor ruled fixed."""
    return str((issue or {}).get('status')) == 'resolved'


def is_unverified(issue) -> bool:
    """True for a prior issue this round neither raised again nor ruled fixed."""
    return str((issue or {}).get('status')) == 'unverified'


def is_closed(issue) -> bool:
    """True for a prior issue closed out this round: resolved or unverified.

    Those are reported against the prior round. They are not the round's own
    required changes or suggestions, and they carry no rank, so they cannot
    collide with the ranks of the issues that are.
    """
    return str((issue or {}).get('status')) in CLOSED_STATUSES


def _rank(issue):
    rank = (issue or {}).get('rank')
    return rank if isinstance(rank, int) else 10 ** 6


def objects_in(issues) -> list:
    """The issue rows of a list that may hold anything, in order.

    The orderers below are handed ``document['issues']`` by every renderer, and
    a row that is not an object reached ``is_closed`` and raised an
    ``AttributeError`` from inside the renderer. A row this version cannot read
    is dropped here, which is the same answer :func:`rows_of` gives a reader
    that starts from the document, and :func:`field_problems` is where it is
    named.
    """
    return [issue for issue in issues if isinstance(issue, dict)] if isinstance(
        issues, (list, tuple)) else []


def live(issues) -> list:
    """Every issue this round raised: the closed-out prior ones are not findings."""
    return [issue for issue in objects_in(issues) if not is_closed(issue)]


def required_issues(issues) -> list:
    """The required changes, in rank order."""
    return sorted((i for i in live(issues) if is_required(i)), key=_rank)


def suggestion_issues(issues) -> list:
    """The suggestions, in rank order. The renderer groups them by category."""
    return sorted((i for i in live(issues) if not is_required(i)), key=_rank)


def resolved_issues(issues) -> list:
    """The prior-round issues the editor ruled fixed, in prior rank order."""
    return sorted((i for i in objects_in(issues) if is_resolved(i)), key=_rank)


def unverified_issues(issues) -> list:
    """The prior issues nobody raised again and nobody ruled fixed."""
    return sorted((i for i in objects_in(issues) if is_unverified(i)), key=_rank)


def closed_issues(issues) -> list:
    """Every prior issue closed out this round, resolved first, then unverified."""
    return resolved_issues(issues) + unverified_issues(issues)


def carried_forward(issues) -> list:
    """The issues a *later* round reads as its prior round.

    A resolved issue is reported in the round that resolved it and then dropped:
    carrying it on would have every round recount every fix ever made, which is
    how round three came to report thirty-seven resolved against nineteen live
    issues. Everything else is still open, ``unverified`` included — that status
    exists precisely because it is not a fix.
    """
    return [issue for issue in objects_in(issues) if not is_resolved(issue)]


def by_category(issues) -> list:
    """``[(category, [issue, …]), …]`` in the vocabulary's own order.

    A category nothing was filed under is left out, and a category this version
    does not know is listed after the ones it does rather than dropped: the
    report shows what the file holds.
    """
    grouped: dict = {}
    for issue in objects_in(issues):
        grouped.setdefault(str(issue.get('category') or 'uncategorised'), []).append(issue)
    known = [(name, sorted(grouped.pop(name), key=_rank))
             for name in ISSUE_CATEGORIES if name in grouped]
    rest = [(name, sorted(rows, key=_rank)) for name, rows in sorted(grouped.items())]
    return known + rest


def rows_of(document) -> list:
    """The issue rows of an issues document: the objects in ``issues``, in order.

    One tolerant reader for every caller that iterates a document it did not
    write. Anything that is not a list of objects reads as no rows, and
    :func:`field_problems` is where a reader says so.
    """
    rows = (document or {}).get('issues') if isinstance(document, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def members_of(issue) -> list:
    """The member findings of one issue: the objects in ``members``, in order.

    Every ``issues.json`` this engine writes holds ``members`` as a list of
    objects, and the readers used to take that on trust. A file that held it as
    a string was iterated one character at a time into ``str.get`` and raised an
    ``AttributeError`` out of :func:`peerreviewagents.read_run`, so a damaged
    file crashed the reader instead of being reported by it. A member that is
    not an object is evidence this version cannot read: it is skipped here and
    named by :func:`field_problems`.
    """
    members = (issue or {}).get('members') if isinstance(issue, dict) else None
    return [member for member in members
            if isinstance(member, dict)] if isinstance(members, list) else []


def quotes_of(issue) -> list:
    """The surviving quotes of one closed issue: the objects, in order.

    The same rule as :func:`members_of`. A closed row carries no members, so
    these are the only evidence it has, and a reader of a file it did not write
    cannot assume their shape either.
    """
    quotes = (issue or {}).get('surviving_quotes') if isinstance(issue, dict) else None
    return [quote for quote in quotes if isinstance(quote, dict)] if isinstance(quotes,
                                                                               list) else []


def lines_of(row) -> list:
    """Every manuscript line a finding or member points at: anchor, then evidence.

    The anchor is ``line`` through ``end_line`` — one line where the quote sits
    on one — and ``evidence_lines`` is a list of further line numbers. A reader
    that spread either into a list crashed on a number and silently took a
    string apart one character at a time, so the numbers come through here
    instead: ints, in order, each once, with anything that is not a positive
    line number dropped.
    """
    row = row if isinstance(row, dict) else {}
    evidence = row.get('evidence_lines')
    evidence = evidence if isinstance(evidence, (list, tuple)) else []
    first, last = _line(row.get('line')), _line(row.get('end_line'))
    anchor = list(range(first, max(first, last or first) + 1)) if first else []
    out = []
    for number in [*anchor, *[_line(value) for value in evidence]]:
        if number and number not in out:
            out.append(number)
    return out


def _line(value):
    """*value* as a line number, or ``None`` where it is not one."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def audits_of(issue) -> list:
    """The specialist audits named on one issue, as text, in order."""
    audits = (issue or {}).get('audits') if isinstance(issue, dict) else None
    audits = audits if isinstance(audits, (list, tuple)) else []
    return [str(name) for name in audits if name]


#: The issue fields every ``issues.json`` holds as a list, with the member
#: reader each one belongs to. A field of any other type is malformed, not
#: empty, and :func:`field_problems` reports it under this name.
_LIST_FIELDS = ('members', 'surviving_quotes', 'audits')


def field_problems(document, label='issues.json') -> list:
    """Every field of an issues document whose type this version cannot read.

    Shape only: the stamp is :func:`schema_problem` and what a *prior* round
    additionally requires is :func:`validate_prior_issues`. The readers skip
    what they cannot read — :func:`rows_of`, :func:`members_of`,
    :func:`quotes_of` — and this is how a reader says so, which is what keeps a
    malformed file a run that is not publishable with the field named instead of
    a crash in the reader or a review rendered from half a document.

    Each problem is one sentence, naming *label* and the field.
    """
    if not isinstance(document, dict):
        return [f'{label} holds a {type(document).__name__} where the issues document belongs']
    rows = document.get('issues')
    if rows is None:
        return []
    if not isinstance(rows, list):
        return [f"{label}'s issues is not a list but a {type(rows).__name__}, so no issue in "
                'it could be read']
    problems = []
    strange = sorted({type(row).__name__ for row in rows if not isinstance(row, dict)})
    if strange:
        count = sum(1 for row in rows if not isinstance(row, dict))
        problems.append(f'{label} holds {count} issue row(s) that are not objects '
                        f"({', '.join(strange)}), and they were not read")
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        identity = (f'issue {row["id"]}' if isinstance(row.get('id'), str) and row['id']
                    else f'the issue in row {position}')
        for field in _LIST_FIELDS:
            if field not in row or isinstance(row[field], list):
                continue
            # `field in row`, not `row.get(field) is not None`: a list field
            # this engine writes is always a list, empty where it does not
            # apply, so a row that holds `"members": null` says something this
            # version does not read rather than nothing at all. A row with no
            # `members` key is the older, narrower shape and is read as a row
            # with no members, which is what every reader above does with it.
            problems.append(f'{label} holds {identity} whose {field} is '
                            + ('null' if row[field] is None
                               else 'a ' + type(row[field]).__name__)
                            + f' where a list belongs, so its {field} were not read')
        for field, entries in (('members', row.get('members')),
                               ('surviving_quotes', row.get('surviving_quotes'))):
            if not isinstance(entries, list):
                continue
            odd = sorted({type(entry).__name__ for entry in entries
                          if not isinstance(entry, dict)})
            if odd:
                count = sum(1 for entry in entries if not isinstance(entry, dict))
                problems.append(f'{label} holds {identity} with {count} {field} entry(s) that '
                                f"are not objects ({', '.join(odd)}), and they were not read")
    return problems


def validate_prior_issues(document, label='issues.json') -> list:
    """Every problem that stops *document* being read as a prior round, in words.

    An empty list means a next round can read this file: ``--prior-issues``
    loads it, matches its ids against the issues the new round raises, and rules
    on each one. This is the check that loader runs, not a copy of it, so a
    consumer about to publish an issues document can hold it against the code
    that will read it next round rather than against the documentation of that
    code.

    What it asks, in order: the ``schema_version`` is one this release reads;
    every field is the type a reader expects (:func:`field_problems`); the
    ``issues`` list is there, because a prior round is the rows it holds; every
    row carries an ``id`` that is non-empty text, because the id is the only
    thing a later round matches on; no id appears twice; ``issue_ids_used`` is
    present and a list of ids in this engine's own form, so the next round
    mints past the ones already spent; and something is left after the resolved
    rows are dropped (:func:`carried_forward`) — a file of nothing but fixes is
    a round that closed, and there is nothing in it for a later round to match.

    A field this function says nothing about is a field the next round loads,
    which is why every requirement of the loader is a requirement here: the
    loader asks nothing outside this list, so a document that passes is a
    document that opens.

    *label* is how the file is named in each problem. The loader passes the
    path it read.
    """
    stamp = schema_problem(document, label=label)
    if stamp:
        return [stamp]
    problems = field_problems(document, label=label)
    rows = document.get('issues')
    if not isinstance(rows, list):
        # A run folder's own document may be read without its issues — a run
        # that stopped before the editor closed has none, and `read_run`
        # reports what it has. A *prior* round is its rows: a file with no
        # issues list has nothing for this round to match its own issues
        # against, and it was refused by name long before this function
        # existed. Checked here rather than in `field_problems`, so the
        # tolerance of one reader is not the requirement of the other.
        problems.append(f'{label} holds no issues list, so it is not an issues.json this '
                        'version can match against')
    seen: dict = {}
    for position, row in enumerate(rows if isinstance(rows, list) else (), 1):
        if not isinstance(row, dict):
            continue
        identity = row.get('id')
        if not isinstance(identity, str) or not identity.strip():
            problems.append(f'{label} holds an issue in row {position} whose id is '
                            + ('absent' if identity is None
                               else f'a {type(identity).__name__}')
                            + ', and an id is what a later round matches on')
            continue
        seen[identity] = seen.get(identity, 0) + 1
    for identity, times in seen.items():
        if times > 1:
            problems.append(f'{label} holds the id {identity!r} more than once ({times} rows), '
                            'so a ruling on it could not say which row it ruled on')
    spent = document.get('issue_ids_used')
    if not isinstance(spent, list):
        problems.append(f'{label} holds '
                        + ('no issue_ids_used list' if spent is None
                           else f'an issue_ids_used that is a {type(spent).__name__}')
                        + ', and that list — every id the round and its own prior rounds spent — '
                          'is what a later round mints above, so without it a resolved issue\'s '
                          'id would be handed to a different problem')
    else:
        odd = sorted({str(value) for value in spent if not is_issue_id(value)})
        if odd:
            problems.append(f"{label} holds {len(odd)} entr(ies) in issue_ids_used that are not "
                            f"issue ids of this engine ({', '.join(odd[:5])}); an id is text of "
                            'the form i01 upwards, and a round cannot mint above a list it '
                            'cannot read')
    if not problems and not carried_forward(rows_of(document)):
        problems.append(f'{label} holds no open issue a later round could match: it holds no '
                        'issues at all, or every row in it is resolved — and a resolved issue is '
                        'reported by the round that resolved it and then dropped')
    return problems


def raised_by(issue) -> int:
    """How many reviewer slots raised this issue, counted from its members."""
    return len({str(member.get('slot')) for member in members_of(issue) if member.get('slot')})


def member_uids(issues) -> list:
    """Every ``finding_uid`` the issues hold as evidence, in order, with repeats."""
    return [str(member.get('finding_uid')) for issue in objects_in(issues)
            for member in members_of(issue) if member.get('finding_uid')]


def counts(issues) -> dict:
    """The issue counts a run publishes: totals, and one tally per axis."""
    rows = live(issues)
    by_severity = {name: sum(1 for i in rows if i.get('severity') == name)
                   for name in ISSUE_SEVERITIES}
    categories: dict = {}
    for issue in rows:
        name = str(issue.get('category') or 'uncategorised')
        categories[name] = categories.get(name, 0) + 1
    statuses = {name: sum(1 for i in objects_in(issues) if i.get('status') == name)
                for name in ISSUE_STATUSES}
    return {
        'issues': len(rows),
        'required': len(required_issues(rows)),
        'suggestions': len(suggestion_issues(rows)),
        'resolved': len(resolved_issues(issues)),
        'unverified': len(unverified_issues(issues)),
        # One name for this count everywhere it is published: the issues.json
        # block, progress.json and read_run() all say issue_members.
        'issue_members': len(set(member_uids(rows))),
        'by_severity': by_severity,
        'by_category': categories,
        'by_status': statuses,
    }


def member_of(finding) -> dict:
    """One finding as an issue keeps it: the evidence fields and nothing else.

    Every field in :data:`MEMBER_FIELDS` is present on every member, null where
    the finding did not carry it, so a consumer reads a member by field name
    rather than by what happens to be in the dict.
    """
    return {key: finding.get(key) for key in MEMBER_FIELDS}


def category_for(finding) -> str:
    """The category a synthesised issue takes for one finding.

    An edit is presentation whatever track filed it; everything else follows
    the track. This is the no-editor path only: where there is an editor, the
    category is its own judgement.
    """
    if str(finding.get('kind')) == 'edit':
        return 'presentation'
    return TRACK_CATEGORY.get(str(finding.get('track')), 'claims-and-conclusions')


def last_rank(prior):
    """The last rank a prior row actually held, closed or live.

    A closed row has no ``rank`` — it would collide with this round's 1..N — so
    the rank it held before closing is in ``prior_rank``. Reading only ``rank``
    reported null for every issue that had been closed once already.
    """
    if not isinstance(prior, dict):
        return None
    return prior.get('rank') if prior.get('rank') is not None else prior.get('prior_rank')


def synthesize(findings, reason=None, prior=(), spent=(), surviving=None) -> list:
    """One issue per finding, for a run no editor grouped.

    Every arm writes an ``issues.json``, because every consumer reads one — the
    paper-scaffold ledger, ``read_run``, ``REVIEW.md``. Where no editor pooled
    the findings, nothing is pooled: each finding is its own issue, ranked major
    first, and the document says ``issues_source: 'server'`` so a reader can
    tell a synthesised file from an edited one.

    ``reason`` is why this run was not grouped, in the run's own words, and it
    goes in every row's ``status_reason``. An arm with no editor is only one of
    the ways here — a ledger past the editor's stated limit and an editor that
    never closed are the others — so the caller says which rather than this
    function assuming the first.

    ``prior`` is the prior round's carried rows and ``spent`` every id this line
    of rounds has used. This path numbered from ``i01`` regardless of either, so
    a degraded second round handed i01 to a new finding while i01 was still the
    id of a prior issue, and published a file in which the prior issues had
    vanished — neither resolved nor open, simply gone. The new issues are minted
    above every spent id, and each prior issue nobody continued is carried as
    ``unverified``: no editor ruled on it, so the honest answer is that it still
    stands unchecked. ``surviving`` maps a prior id to the quotes of its
    evidence the caller could still find in the manuscript.
    """
    order = {name: index for index, name in enumerate(ISSUE_SEVERITIES)}
    rows = sorted(findings or [], key=lambda f: order.get(str(f.get('severity')), len(order)))
    prior_rows = [row for row in prior or () if isinstance(row, dict) and row.get('id')]
    taken = {str(row['id']) for row in prior_rows} | {str(name) for name in spent or ()}
    surviving = surviving or {}
    out, number = [], 0
    for rank, finding in enumerate(rows, 1):
        severity = str(finding.get('severity'))
        number += 1
        identifier = issue_id(number)
        while identifier in taken:
            number += 1
            identifier = issue_id(number)
        taken.add(identifier)
        out.append({
            'id': identifier,
            'name': str(finding.get('title') or 'Untitled finding')[:120],
            'severity': severity if severity in ISSUE_SEVERITIES else 'minor',
            'category': category_for(finding),
            'rank': rank,
            'statement': str(finding.get('comment') or ''),
            'members': [member_of(finding)],
            'raised_by': 1,
            'rebuttal_line': None,
            'rebuttal_quote': None,
            'prior_id': None,
            'prior_rank': None,
            'status': 'new',
            'status_reason': ('No editor pooled this run, so each finding is its own issue: '
                              + str(reason).rstrip('.') + '.') if reason else
                             'No editor pooled this run, so each finding is its own issue.',
            'text_unchanged_reason': None,
            'surviving_quotes': [],
            'audits': [],
        })
    # The prior issues, carried. Nobody ruled on them: there was no editor to
    # read the revision against them, and a row this round cannot speak to is
    # `unverified` — not resolved, which would tell the authors a problem was
    # fixed on no evidence, and not dropped, which would tell them nothing at
    # all. They keep no rank; the renderer lists closed rows after the live ones.
    for prior_row in prior_rows:
        identifier = str(prior_row['id'])
        out.append({
            'id': identifier,
            'name': prior_row.get('name'),
            'severity': prior_row.get('severity'),
            'category': prior_row.get('category'),
            'rank': None,
            'statement': prior_row.get('statement'),
            'members': [],
            'raised_by': 0,
            'rebuttal_line': None,
            'rebuttal_quote': None,
            'prior_id': identifier,
            'prior_rank': last_rank(prior_row),
            'status': 'unverified',
            'status_reason': ('No editor ruled on this issue from the prior round: '
                              + str(reason).rstrip('.') + '. It stands as it was filed.')
                             if reason else
                             ('No editor ruled on this issue from the prior round, so it stands '
                              'as it was filed.'),
            'text_unchanged_reason': None,
            'surviving_quotes': list(surviving.get(identifier) or []),
            'audits': [],
        })
    return out


def prior_tally(issues) -> dict:
    """How this round stands against the prior one, by status."""
    return {name: sum(1 for issue in objects_in(issues) if issue.get('status') == name)
            for name in ISSUE_STATUSES}


__all__ = ['ISSUES_SCHEMA_VERSION', 'KNOWN_ISSUES_SCHEMAS', 'ISSUE_SEVERITIES',
           'REQUIRED_SEVERITIES', 'SUGGESTION_SEVERITIES', 'ISSUE_CATEGORIES', 'ISSUE_STATUSES',
           'MATCHED_STATUSES', 'PRIOR_RULING_STATUSES', 'CLOSED_STATUSES', 'MAX_ISSUE_BATCH',
           'MAX_ISSUE_BATCHES', 'TRACK_CATEGORY', 'MEMBER_FIELDS', 'ISSUE_FIELDS', 'issue_id',
           'issue_batch_limit', 'schema_problem', 'field_problems', 'validate_prior_issues',
           'rows_of', 'objects_in', 'members_of', 'quotes_of', 'lines_of', 'audits_of',
           'is_required',
           'is_resolved', 'is_unverified', 'is_closed', 'live', 'required_issues',
           'suggestion_issues', 'resolved_issues', 'unverified_issues', 'closed_issues',
           'carried_forward', 'by_category', 'raised_by', 'member_uids', 'counts', 'member_of',
           'category_for', 'synthesize', 'prior_tally']
