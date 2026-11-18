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
ISSUES_SCHEMA_VERSION = 'review-issues-v4'

#: Every schema this version can read. A stamp is only worth writing if someone
#: checks it: :func:`schema_problem` is that check, and the reader and the
#: renderer both go through it rather than each deciding for itself.
#: ``v4`` is the one this release reads, and every earlier stamp is refused by
#: name rather than read as far as it happens to parse. ``v1`` held evidence as
#: block ids, which nothing here can turn into lines of the manuscript; ``v2``
#: and ``v3`` were never in a release — both were written and superseded inside
#: the unreleased one; and a ``v3`` row has no ``rebuttal_document``, so a
#: rebuttal quoted from a
#: supplement would be read as the manuscript's and anchored at a line of
#: ``paper.md`` that says something else. Reading any of them would be a
#: compatibility path that answers with the wrong passage. Forward only, as the
#: manuscript base is.
KNOWN_ISSUES_SCHEMAS = ('review-issues-v4',)

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
#: ``fingerprint`` is the digest of the whole passage the quote was found in: it
#: names the passage itself, so a consumer can recognise the same passage after
#: the converter has broken the lines somewhere else, which is the one thing
#: line numbers cannot survive.
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

#: Every field on every issue row of ``issues.json``, edited or synthesised. A
#: field that does not apply to a row is ``null``, or ``[]`` where the field is
#: a list — the keys themselves are always there, so a consumer reads a field by
#: name instead of testing for its presence, and ``docs/INTEGRATIONS.md``
#: documents this list.
ISSUE_FIELDS = ('id', 'name', 'severity', 'category', 'rank', 'statement', 'members',
                'raised_by', 'rebuttal_document', 'rebuttal_line', 'rebuttal_quote', 'audits')


def issue_id(number: int) -> str:
    """The id minted for the *number*-th new issue of a run: ``i01``, ``i02``, …"""
    return f'i{int(number):02d}'


#: An issue id as this engine mints it: ``i`` and two digits or more. An id is
#: what a reader of ``issues.json`` points at one issue with, so a consumer
#: checks the form rather than taking whatever the field holds.
ISSUE_ID_PATTERN = r'i\d{2,}'


def is_issue_id(value) -> bool:
    """True where *value* is an issue id in this engine's own form.

    Text, and :data:`ISSUE_ID_PATTERN`. ``5``, ``True`` and ``'7'`` are not
    ids: a consumer that read them as ids would link to an issue no row of the
    file holds.
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


def _rank(issue):
    rank = (issue or {}).get('rank')
    return rank if isinstance(rank, int) else 10 ** 6


def objects_in(issues) -> list:
    """The issue rows of a list that may hold anything, in order.

    The orderers below are handed ``document['issues']`` by every renderer, and
    a row that is not an object raised an ``AttributeError`` from inside the
    renderer. A row this version cannot read is dropped here, which is the same
    answer :func:`rows_of` gives a reader that starts from the document, and
    :func:`field_problems` is where it is named.
    """
    return [issue for issue in issues if isinstance(issue, dict)] if isinstance(
        issues, (list, tuple)) else []


def required_issues(issues) -> list:
    """The required changes, in rank order."""
    return sorted((i for i in objects_in(issues) if is_required(i)), key=_rank)


def suggestion_issues(issues) -> list:
    """The suggestions, in rank order. The renderer groups them by category."""
    return sorted((i for i in objects_in(issues) if not is_required(i)), key=_rank)


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
_LIST_FIELDS = ('members', 'audits')


def field_problems(document, label='issues.json') -> list:
    """Every field of an issues document whose type this version cannot read.

    Shape only: the stamp is :func:`schema_problem`. The readers skip what they
    cannot read — :func:`rows_of`, :func:`members_of` — and this is how a reader
    says so, which is what keeps a malformed file a run that is not publishable
    with the field named instead of a crash in the reader or a review rendered
    from half a document.

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
        entries = row.get('members')
        if isinstance(entries, list):
            odd = sorted({type(entry).__name__ for entry in entries
                          if not isinstance(entry, dict)})
            if odd:
                count = sum(1 for entry in entries if not isinstance(entry, dict))
                problems.append(f'{label} holds {identity} with {count} members entry(s) that '
                                f"are not objects ({', '.join(odd)}), and they were not read")
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
    rows = objects_in(issues)
    by_severity = {name: sum(1 for i in rows if i.get('severity') == name)
                   for name in ISSUE_SEVERITIES}
    categories: dict = {}
    for issue in rows:
        name = str(issue.get('category') or 'uncategorised')
        categories[name] = categories.get(name, 0) + 1
    return {
        'issues': len(rows),
        'required': len(required_issues(rows)),
        'suggestions': len(suggestion_issues(rows)),
        # One name for this count everywhere it is published: the issues.json
        # block, progress.json and read_run() all say issue_members.
        'issue_members': len(set(member_uids(rows))),
        'by_severity': by_severity,
        'by_category': categories,
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


def synthesize(findings, reason=None) -> list:
    """One issue per finding, for a run no editor grouped.

    Every arm writes an ``issues.json``, because every consumer reads one — the
    paper-scaffold ledger, ``read_run``, ``REVIEW.md``. Where no editor pooled
    the findings, nothing is pooled: each finding is its own issue, ranked major
    first, and the document says ``issues_source: 'server'`` so a reader can
    tell a synthesised file from an edited one.

    ``reason`` is why this run was not grouped, in the run's own words, and the
    caller publishes it as the document's ``issues_source_reason``. An arm with
    no editor is only one of the ways here — a ledger past the editor's stated
    limit and an editor that never closed are the others — so the caller says
    which rather than this function assuming the first.
    """
    order = {name: index for index, name in enumerate(ISSUE_SEVERITIES)}
    rows = sorted(findings or [], key=lambda f: order.get(str(f.get('severity')), len(order)))
    out = []
    for rank, finding in enumerate(rows, 1):
        severity = str(finding.get('severity'))
        out.append({
            'id': issue_id(rank),
            'name': str(finding.get('title') or 'Untitled finding')[:120],
            'severity': severity if severity in ISSUE_SEVERITIES else 'minor',
            'category': category_for(finding),
            'rank': rank,
            'statement': str(finding.get('comment') or ''),
            'members': [member_of(finding)],
            'raised_by': 1,
            'rebuttal_document': None,
            'rebuttal_line': None,
            'rebuttal_quote': None,
            'audits': [],
        })
    return out


__all__ = ['ISSUES_SCHEMA_VERSION', 'KNOWN_ISSUES_SCHEMAS', 'ISSUE_SEVERITIES',
           'REQUIRED_SEVERITIES', 'SUGGESTION_SEVERITIES', 'ISSUE_CATEGORIES',
           'MAX_ISSUE_BATCH', 'MAX_ISSUE_BATCHES', 'TRACK_CATEGORY', 'MEMBER_FIELDS',
           'ISSUE_FIELDS', 'issue_id', 'issue_batch_limit', 'schema_problem', 'field_problems',
           'rows_of', 'objects_in', 'members_of', 'lines_of', 'audits_of',
           'is_required', 'required_issues', 'suggestion_issues',
           'by_category', 'raised_by', 'member_uids', 'counts', 'member_of',
           'category_for', 'synthesize']
