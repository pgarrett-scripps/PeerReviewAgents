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

#: Where an issue stands against the prior round named at ingest. ``new`` for a
#: run with no prior round, ``persisting`` and ``regressed`` for a prior issue
#: the editor matched, ``resolved`` for a prior issue nothing in this round
#: matched.
ISSUE_STATUSES = ('new', 'persisting', 'regressed', 'resolved')

#: The statuses an issue the editor matched to a prior one may carry.
MATCHED_STATUSES = ('persisting', 'regressed')

#: How many issues one ``submit_issues`` call may carry. The editor submits in
#: batches so that the work in one turn is bounded whatever the finding count
#: is; see :func:`peerreviewagents.plugin.server.submit_issues`.
MAX_ISSUE_BATCH = 12

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


def issue_id(number: int) -> str:
    """The id minted for the *number*-th new issue of a run: ``i01``, ``i02``, …"""
    return f'i{int(number):02d}'


def is_required(issue) -> bool:
    """True for a major or moderate issue: the ones that are required changes."""
    return str((issue or {}).get('severity')) in REQUIRED_SEVERITIES


def is_resolved(issue) -> bool:
    """True for a prior-round issue nothing in this round matched."""
    return str((issue or {}).get('status')) == 'resolved'


def _rank(issue):
    rank = (issue or {}).get('rank')
    return rank if isinstance(rank, int) else 10 ** 6


def live(issues) -> list:
    """Every issue this round raised: the resolved prior ones are not findings."""
    return [issue for issue in issues or [] if not is_resolved(issue)]


def required_issues(issues) -> list:
    """The required changes, in rank order."""
    return sorted((i for i in live(issues) if is_required(i)), key=_rank)


def suggestion_issues(issues) -> list:
    """The suggestions, in rank order. The renderer groups them by category."""
    return sorted((i for i in live(issues) if not is_required(i)), key=_rank)


def resolved_issues(issues) -> list:
    """The prior-round issues this round found nothing of, in prior rank order."""
    return sorted((i for i in issues or [] if is_resolved(i)), key=_rank)


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
        'members': len(set(member_uids(rows))),
        'by_severity': by_severity,
        'by_category': categories,
        'by_status': statuses,
    }


def member_of(finding) -> dict:
    """One finding as an issue keeps it: the evidence fields and nothing else."""
    return {key: finding.get(key) for key in MEMBER_FIELDS if key in finding}


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
            'prior_id': None,
            'status': 'new',
            'status_reason': 'No editor pooled this run, so each finding is its own issue.',
        })
    return out


def prior_tally(issues) -> dict:
    """How this round stands against the prior one, by status."""
    return {name: sum(1 for issue in issues or [] if issue.get('status') == name)
            for name in ISSUE_STATUSES}


__all__ = ['ISSUES_SCHEMA_VERSION', 'ISSUE_SEVERITIES', 'REQUIRED_SEVERITIES',
           'SUGGESTION_SEVERITIES', 'ISSUE_CATEGORIES', 'ISSUE_STATUSES', 'MATCHED_STATUSES',
           'MAX_ISSUE_BATCH', 'TRACK_CATEGORY', 'MEMBER_FIELDS', 'issue_id', 'is_required',
           'is_resolved', 'live', 'required_issues', 'suggestion_issues', 'resolved_issues',
           'by_category', 'raised_by', 'member_uids', 'counts', 'member_of', 'category_for',
           'synthesize', 'prior_tally']
