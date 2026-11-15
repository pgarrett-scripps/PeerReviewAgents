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

#: The schema every ``issues.json`` is stamped with, in its ``schema_version``.
ISSUES_SCHEMA_VERSION = 'review-issues-v1'

#: Every schema this version can read. A stamp is only worth writing if someone
#: checks it: :func:`schema_problem` is that check, and the reader, the prior-round
#: loader and the renderer all go through it rather than each deciding for itself.
KNOWN_ISSUES_SCHEMAS = ('review-issues-v1',)

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

#: The member fields an issue keeps as evidence. The quote and the block ids
#: are kept on the member, so a reader of ``issues.json`` alone can go to the
#: manuscript without opening ``findings.jsonl``.
MEMBER_FIELDS = ('finding_uid', 'slot', 'track', 'severity', 'kind', 'basis', 'escalate',
                 'title', 'block_id', 'quote', 'evidence_blocks')

#: Every field on every issue row of ``issues.json``, live or closed, edited or
#: synthesised. A field that does not apply to a row is ``null``, or ``[]``
#: where the field is a list — the keys themselves are always there, so a
#: consumer reads a field by name instead of testing for its presence, and
#: ``docs/INTEGRATIONS.md`` documents this list.
ISSUE_FIELDS = ('id', 'name', 'severity', 'category', 'rank', 'statement', 'members',
                'raised_by', 'rebuttal_block_id', 'rebuttal_quote', 'prior_id', 'prior_rank',
                'status', 'status_reason', 'text_unchanged_reason', 'surviving_quotes',
                'audits')


def issue_id(number: int) -> str:
    """The id minted for the *number*-th new issue of a run: ``i01``, ``i02``, …"""
    return f'i{int(number):02d}'


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


def live(issues) -> list:
    """Every issue this round raised: the closed-out prior ones are not findings."""
    return [issue for issue in issues or [] if not is_closed(issue)]


def required_issues(issues) -> list:
    """The required changes, in rank order."""
    return sorted((i for i in live(issues) if is_required(i)), key=_rank)


def suggestion_issues(issues) -> list:
    """The suggestions, in rank order. The renderer groups them by category."""
    return sorted((i for i in live(issues) if not is_required(i)), key=_rank)


def resolved_issues(issues) -> list:
    """The prior-round issues the editor ruled fixed, in prior rank order."""
    return sorted((i for i in issues or [] if is_resolved(i)), key=_rank)


def unverified_issues(issues) -> list:
    """The prior issues nobody raised again and nobody ruled fixed."""
    return sorted((i for i in issues or [] if is_unverified(i)), key=_rank)


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
    return [issue for issue in issues or [] if not is_resolved(issue)]


def by_category(issues) -> list:
    """``[(category, [issue, …]), …]`` in the vocabulary's own order.

    A category nothing was filed under is left out, and a category this version
    does not know is listed after the ones it does rather than dropped: the
    report shows what the file holds.
    """
    grouped: dict = {}
    for issue in issues or []:
        grouped.setdefault(str(issue.get('category') or 'uncategorised'), []).append(issue)
    known = [(name, sorted(grouped.pop(name), key=_rank))
             for name in ISSUE_CATEGORIES if name in grouped]
    rest = [(name, sorted(rows, key=_rank)) for name, rows in sorted(grouped.items())]
    return known + rest


def raised_by(issue) -> int:
    """How many reviewer slots raised this issue, counted from its members."""
    return len({str(member.get('slot')) for member in (issue or {}).get('members') or []
                if member.get('slot')})


def member_uids(issues) -> list:
    """Every ``finding_uid`` the issues hold as evidence, in order, with repeats."""
    return [str(member.get('finding_uid')) for issue in issues or []
            for member in issue.get('members') or [] if member.get('finding_uid')]


def counts(issues) -> dict:
    """The issue counts a run publishes: totals, and one tally per axis."""
    rows = live(issues)
    by_severity = {name: sum(1 for i in rows if i.get('severity') == name)
                   for name in ISSUE_SEVERITIES}
    categories: dict = {}
    for issue in rows:
        name = str(issue.get('category') or 'uncategorised')
        categories[name] = categories.get(name, 0) + 1
    statuses = {name: sum(1 for i in issues or [] if i.get('status') == name)
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


def synthesize(findings) -> list:
    """One issue per finding, for an arm that runs no editor.

    Every arm writes an ``issues.json``, because every consumer reads one — the
    paper-scaffold ledger, ``read_run``, ``REVIEW.md``. Where no editor pooled
    the findings, nothing is pooled: each finding is its own issue, ranked major
    first, and the document says ``issues_source: 'server'`` so a reader can
    tell a synthesised file from an edited one.
    """
    order = {name: index for index, name in enumerate(ISSUE_SEVERITIES)}
    rows = sorted(findings or [], key=lambda f: order.get(str(f.get('severity')), len(order)))
    out = []
    for number, finding in enumerate(rows, 1):
        severity = str(finding.get('severity'))
        out.append({
            'id': issue_id(number),
            'name': str(finding.get('title') or 'Untitled finding')[:120],
            'severity': severity if severity in ISSUE_SEVERITIES else 'minor',
            'category': category_for(finding),
            'rank': number,
            'statement': str(finding.get('comment') or ''),
            'members': [member_of(finding)],
            'raised_by': 1,
            'rebuttal_block_id': None,
            'rebuttal_quote': None,
            'prior_id': None,
            'prior_rank': None,
            'status': 'new',
            'status_reason': 'No editor pooled this run, so each finding is its own issue.',
            'text_unchanged_reason': None,
            'surviving_quotes': [],
            'audits': [],
        })
    return out


def prior_tally(issues) -> dict:
    """How this round stands against the prior one, by status."""
    return {name: sum(1 for issue in issues or [] if issue.get('status') == name)
            for name in ISSUE_STATUSES}


__all__ = ['ISSUES_SCHEMA_VERSION', 'KNOWN_ISSUES_SCHEMAS', 'ISSUE_SEVERITIES',
           'REQUIRED_SEVERITIES', 'SUGGESTION_SEVERITIES', 'ISSUE_CATEGORIES', 'ISSUE_STATUSES',
           'MATCHED_STATUSES', 'PRIOR_RULING_STATUSES', 'CLOSED_STATUSES', 'MAX_ISSUE_BATCH',
           'MAX_ISSUE_BATCHES', 'TRACK_CATEGORY', 'MEMBER_FIELDS', 'ISSUE_FIELDS', 'issue_id',
           'issue_batch_limit', 'schema_problem', 'is_required', 'is_resolved', 'is_unverified',
           'is_closed', 'live', 'required_issues', 'suggestion_issues', 'resolved_issues',
           'unverified_issues', 'closed_issues', 'carried_forward', 'by_category', 'raised_by',
           'member_uids', 'counts', 'member_of', 'category_for', 'synthesize', 'prior_tally']
