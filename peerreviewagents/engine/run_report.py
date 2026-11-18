"""Readable Markdown and plain-text reports of a review run, written from its saved JSON.

Nothing here makes a model call. Everything on the page comes from files the run
wrote about itself: ``protocol.json`` for the arm and the agent slots,
``runtime.json`` and the usage receipts for what the run really spent,
``citations/ledger.jsonl`` for what the citation checks reached, and
``issues.json`` for the review.

``issues.json`` is the only thing the review itself is rendered from. The page
prints the verdict, the required changes in the editor's rank order and the
suggestions grouped by category, and every issue carries its member findings as
evidence. Nothing is capped and nothing is left out: an issue the editor raised
is on the page, whatever the count.
"""
import textwrap
from pathlib import Path

from .. import issues as issue_model
from ..names import RUN_DIRECTORY
from ..plugin import models as model_plan
from . import citation_ledger, code_source, paper_base
from .storage import read, relative_name
from .usage import agent_rows, observed_usage, runtime_usage

WIDTH = 88
MANUSCRIPT = 'manuscript'
NOTICE = ('This review was generated automatically. Exact quotations and issue accounting were validated. '
          'Scientific correctness was not.')
AGENT_COLUMNS = ('Agent', 'Model asked', 'Model ran', 'Server calls', 'Session calls',
                 'Tokens', 'State')
# What makes a folder a run folder, for a run that stopped before the server
# wrote its protocol: the starter's own files, and the server's first ones.
RUN_FILES = ('protocol.json', 'progress.json', 'runtime.json', 'startup.json', 'findings.jsonl')


def _optional(path):
    """One JSON file, or ``None`` when it is absent or cannot be read.

    A report of a killed run is rendered from the files that run left behind,
    and one of them is usually half-written. A traceback from the renderer loses
    the whole page, including the reason the run stopped, so an unreadable file
    is read as absent and the page says what it can.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return read(path)
    except (ValueError, UnicodeDecodeError):
        return None


def find_workspace(run):
    """Locate the reviewed workspace: the manuscript folder the run imported into."""
    run = Path(run)
    for candidate in (run.parent / MANUSCRIPT, run / MANUSCRIPT):
        if (candidate / 'manifest.json').is_file():
            return candidate
    return None


def _reviewer_packages(run):
    folders = [path for path in run.glob('reviewer-*') if path.name.split('-', 1)[1].isdigit()]
    packages = [(int(path.name.split('-', 1)[1]), _optional(path / 'findings.json'))
                for path in sorted(folders, key=lambda path: int(path.name.split('-', 1)[1]))]
    return [(number, package) for number, package in packages if isinstance(package, dict)]


def _issues_document(run):
    """The run's issues document: the one file the review is rendered from."""
    document = _optional(run / 'issues.json')
    if not isinstance(document, dict):
        # No issues.json at all is normal for a run whose editor never closed,
        # and that run renders "No review was produced". A folder from an engine
        # that wrote its review as `editor-package.json` is a different thing:
        # the review exists, this engine cannot read it, and rendering would
        # replace a real REVIEW.md with that sentence. Refuse instead.
        if (Path(run) / 'editor-package.json').is_file():
            raise ValueError(
                str(Path(run) / 'editor-package.json') + ' holds the review and there is no '
                'issues.json beside it, so that folder was written by an engine before the '
                'review became issues. This engine will not re-render it; REVIEW.md is left '
                'untouched.')
        return None
    # A run folder written by an engine this version does not read is refused,
    # not rendered. Rendering it would print "No review was produced" over a
    # review that was produced, and `write_reports` would then overwrite the
    # REVIEW.md that engine wrote. The refusal happens before anything is
    # written, so the existing file is left exactly as it is.
    problem = issue_model.schema_problem(document, label=str(Path(run) / 'issues.json'))
    if problem:
        raise ValueError(problem + '. The review in that folder was written by another engine '
                         'and this one will not re-render it; REVIEW.md is left untouched.')
    return document


def _workflow(protocol):
    """What the arm really ran, as the server's own plan recorded it."""
    plan = protocol.get('plan') or {}
    arm = protocol.get('arm') or protocol.get('mode') or 'unknown'
    specialists = plan.get('specialists') or []
    parts = [f"{plan.get('reviewers') or protocol.get('reviewers') or 0} core reviewer(s)",
             f'{len(specialists)} specialist track(s)' if specialists else 'no specialist track',
             *(['one revision pass'] if plan.get('revision') else []),
             'an editor' if plan.get('editor') else 'no editor']
    return f'{arm}: ' + ', '.join(parts)


def _duration(seconds):
    """Wall time in minutes and seconds, or nothing when the run never recorded it."""
    if not isinstance(seconds, (int, float)):
        return None
    whole = round(float(seconds))
    return f'{whole // 60} min {whole % 60} s' if whole >= 60 else f'{whole} s'


def _total(rows, key):
    """The sum of one numeric column, or nothing when no agent reported it."""
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return f'{sum(values):,}' if values else None


def _tokens(runtime, usage):
    """What the run spent, from the session's own stream, with receipts as fallback.

    ``runtime.json`` is the stream's accounting and the number to publish. The
    usage receipts are the mid-run guard and answer for a run whose session
    ended before the starter could write a runtime file.
    """
    total, source = runtime.get('tokens_total'), 'session stream'
    if not total:
        total, source = usage['observed_tokens'], 'usage receipts'
    if not total:
        return None
    text = f'{total:,} ({source})'
    return text + '; some usage is unknown' if usage.get('unknown') else text


def _agent_label(row):
    attempt = row.get('attempt') or 1
    return row['slot'] if attempt == 1 else f"{row['slot']}#{attempt}"


def _agent_table(agents):
    """One row per agent slot, with the model it was asked to run and the one that ran.

    The slots do not all run one model, so there is no single model for a run
    and the table is the only honest answer. The two model columns are two
    different facts and are never merged: ``Model asked`` is the run's plan
    from ``protocol.json`` and ``Model ran`` is what that agent reported, so a
    slot that was given Opus and ran Sonnet shows it here rather than reading
    as a run of the arm it is filed under. A slot that reported nothing says
    ``not recorded``, which is not the same as having run what it was asked.

    The slot name carries the role and the track already (``core-2``,
    ``spec-stats``, ``editor``), so neither gets a column of its own. Two model
    ids and both call counts put this table past the prose width of the
    plain-text report, which is the one place that is worth it: the table is
    read as a table, and dropping either model column would hide the thing it
    is here to show.

    ``Server calls`` is what the server served that agent, which is the count
    the ledger bounds. ``Session calls`` is every tool call Claude Code saw it
    make, which is larger and is the session's count rather than the server's.
    """
    if not agents:
        return []
    body = [[_agent_label(row),
             row.get('assigned_model') or 'not recorded',
             row.get('model') or 'not recorded',
             row.get('tool_calls') if row.get('tool_calls') is not None else '—',
             row.get('session_calls') if row.get('session_calls') is not None else '—',
             f"{row['tokens']:,}" if isinstance(row.get('tokens'), int) else '—',
             row.get('state') or '—'] for row in agents]
    return [('heading2', 'Agents'), ('table', list(AGENT_COLUMNS), body)]


def _citation_blocks(run):
    """What the citation ledger checked, in numbers, and what it did not.

    The page prints the table's own counts rather than the specialist's prose
    about them, because the one thing a reader cannot get from a findings list
    is how much of the reference list was looked at. A run whose citations
    specialist found nothing and a run that checked nothing both show no
    citation findings; these counts are the difference.

    An unfinished stage is printed as a line of its own and is not a failure of
    the run: see :func:`peerreviewagents.engine.citation_ledger.problems`.
    """
    ledger = citation_ledger.Ledger(run)
    if not ledger.exists():
        return []
    try:
        totals = citation_ledger.counts(ledger)
    except (OSError, UnicodeDecodeError, ValueError):
        # Same rule as `_optional`: a half-written ledger must not cost the
        # reader the rest of the page, including why the run stopped.
        return [('heading2', 'Citations'),
                ('paragraph', 'A citation ledger is in this run folder but it could not be '
                              'read, so none of its counts are shown.')]
    rows = [('References', totals['references']),
            ('Resolved', totals['resolved']),
            ('Metadata mismatch', totals['mismatched']),
            ('Not found', totals['not_found']),
            # Beside the others, because a reference two indexes do not list is
            # a state of its own: much of the literature is in neither, so it
            # is not the same answer as an unregistered DOI, and leaving it off
            # the page let a reader add the printed numbers, miss the total and
            # read the difference as resolved.
            ('Not located', totals['not_located']),
            ('Could not be checked', totals['could_not_check']),
            ('Never looked up', totals['unresolved']),
            ('Retracted', totals['retracted']),
            ('In-text uses', totals['uses']),
            ('Uses judged', totals['uses_judged']),
            ('Supported', totals['supported']),
            ('Partly supported', totals['partly_supported']),
            ('Not supported', totals['not_supported']),
            ('Cannot tell', totals['cannot_tell']),
            ('Uses never judged', totals['uses_unjudged']),
            ('Lookups made', totals['lookups']),
            # The name, not the path. This row wrote the absolute location of
            # the ledger into REVIEW.md and REVIEW.txt, so the two documents
            # the authors are sent named the machine that ran the review and a
            # folder that was moved or copied read as a review of a file that
            # is not there. A run-relative POSIX name is what every other
            # published file is named by.
            ('Ledger', relative_name(Path(run) / citation_ledger.LEDGER_PATH, _run_root(run)))]
    blocks = [('heading2', 'Citations'),
              ('paragraph', citation_ledger.summary_line(totals)),
              ('fields', [(key, value) for key, value in rows])]
    unfinished = citation_ledger.problems(totals)
    if unfinished:
        blocks.append(('labeled', 'Citation checks unfinished',
                       'the findings below still stand, and these are the parts of the '
                       'reference list this run did not finish checking'))
        blocks.append(('bullets', unfinished))
    return blocks


def _location(finding):
    if finding.get('placement') not in (None, 'anchored'):
        return f"unplaced ({finding['placement']})"
    if not finding.get('line'):
        return 'not located'
    where = str(finding['line'])
    if finding.get('end_line') and int(finding['end_line']) > int(finding['line']):
        where += '–' + str(finding['end_line'])
    # The main document is not named: a reader of the review opens `paper.md`
    # at the line, and printing its name on every finding of every report is
    # noise. A supplement is named, because there the reader needs the file.
    document = str(finding.get('document') or '')
    named = document and document != paper_base.MAIN_DOCUMENT
    return (f"{document} line " if named else 'line ') + where


def _finding(number, finding):
    blocks = [('heading3', f"{number}. {finding.get('title') or 'Untitled finding'}"),
              ('inline', [('Severity', finding.get('severity') or 'n/a'), ('Type', finding.get('kind') or 'n/a'),
                          ('Location', _location(finding))])]
    if finding.get('quote'):
        blocks += [('paragraph', 'Manuscript passage:'), ('quote', finding['quote'])]
    if finding.get('comment'):
        blocks.append(('paragraph', finding['comment']))
    if finding.get('replacement'):
        blocks += [('paragraph', 'Suggested replacement:'), ('quote', finding['replacement'])]
    if finding.get('counterevidence'):
        blocks.append(('labeled', 'Strongest counterpoint', finding['counterevidence']))
    return blocks


def _member(member):
    """One member finding of an issue: who raised it, where, and the passage itself."""
    label = ' · '.join(str(part) for part in
                       (member.get('slot'), member.get('track'), _location(member)) if part)
    # `str`, because the title is read off a file this renderer did not write:
    # a member whose title was a number concatenated into a TypeError out of
    # `write_reports`, which is the one call that has to work on a damaged
    # folder — it is how the folder gets read at all.
    blocks = [('paragraph', (label + ': ' + str(member.get('title') or '')).strip(' :'))]
    if member.get('quote'):
        blocks.append(('quote', member['quote']))
    return blocks


def _issue(issue):
    """One issue: its statement, then the findings it is raised from as evidence."""
    rank = issue.get('rank')
    heading = f"{rank}. {issue.get('name') or 'Unnamed issue'}" if rank else str(
        issue.get('name') or 'Unnamed issue')
    raised = issue.get('raised_by')
    raised = issue_model.raised_by(issue) if raised is None else raised
    inline = [('Severity', issue.get('severity') or 'n/a'),
              ('Category', issue.get('category') or 'n/a'),
              ('Raised by', f'{raised} reviewer(s)'),
              ('Findings', len(issue_model.members_of(issue)))]
    if issue.get('id'):
        inline.append(('Issue', issue['id']))
    # Only a run given a prior round has a status to report; in a first round
    # every issue is new and the column would say the same thing on every line.
    if str(issue.get('status') or 'new') != 'new':
        inline.append(('Since the prior round', issue['status']))
    blocks = [('heading3', heading), ('inline', inline)]
    if issue.get('statement'):
        blocks.append(('paragraph', issue['statement']))
    if issue.get('status_reason'):
        blocks.append(('labeled', 'Against the prior round', issue['status_reason']))
    if issue.get('rebuttal_quote'):
        # Where the quote was anchored, and in which document: a rebuttal
        # quoted from a supplement says so, because a line number on its own
        # would read as a line of paper.md.
        where = (f" in {issue['rebuttal_document']}" if issue.get('rebuttal_document') else '')
        where += (f" on line {issue['rebuttal_line']}"
                  if issue.get('rebuttal_line') else '')
        blocks += [('paragraph', 'Ruled a suggestion rather than a required change because '
                                 'the manuscript already carries its own qualification'
                                 + where + ':'),
                   ('quote', issue['rebuttal_quote'])]
    for member in issue_model.members_of(issue):
        blocks += _member(member)
    audits = issue_model.audits_of(issue)
    if audits:
        blocks.append(('labeled', 'Specialist audit', ', '.join(audits)))
    return blocks


#: What each prior-round status means, printed with the tally so a reader of
#: REVIEW.md does not have to find the documentation to know what it claims.
_STATUS_MEANINGS = (
    ('new', 'raised for the first time this round'),
    ('persisting', 'the prior issue was raised again, unchanged in substance'),
    ('regressed', 'the prior issue was raised again and is worse'),
    ('resolved', 'the editor ruled the prior issue fixed, with a reason'),
    ('unverified', 'the prior issue was not raised again this round and was not '
                   'verified as fixed — it is not a resolution'),
)


def _run_folder(run):
    """The run's own folder name, not the name of the directory inside it.

    Every caller hands this module the review directory, so the header printed
    "review" for every run ever written — a row that told a reader nothing and
    a title that read "Review run review". The run folder is its parent, and
    that name is the run id a reader can look up.
    """
    if run.name == RUN_DIRECTORY and run.parent.name:
        return run.parent.name
    return run.name


def _run_root(run):
    """The run folder a published name is relative to.

    Every caller hands this module the review directory, and a file in it is
    published as ``review/<name>`` — the same name the manifest, the issues
    document and every message use.
    """
    run = Path(run)
    return run.parent if run.name == RUN_DIRECTORY and run.parent.name else run


def _closed(issue):
    """A prior issue this round closed: what it said, and the ruling against it."""
    name = str(issue.get('name') or 'Unnamed issue')
    heading = f"{issue['id']}: {name}" if issue.get('id') else name
    inline = [('Ruling', issue.get('status') or 'resolved')]
    if issue.get('prior_rank') is not None:
        inline.append(('Rank in the prior round', issue['prior_rank']))
    if issue.get('severity'):
        inline.append(('Severity then', issue['severity']))
    blocks = [('heading3', heading), ('inline', inline)]
    if issue.get('statement'):
        blocks.append(('paragraph', issue['statement']))
    if issue.get('status_reason'):
        blocks.append(('labeled', 'Why it is closed', issue['status_reason']))
    if issue.get('text_unchanged_reason'):
        blocks.append(('labeled', 'Why it is fixed although the passages it cited still '
                                  'read the same', issue['text_unchanged_reason']))
    quotes = [str(quote['quote']) for quote in issue_model.quotes_of(issue) if quote.get('quote')]
    if quotes:
        # Labelled, because an unlabelled blockquote under "Why it is closed"
        # read as part of the ruling — the authors could not tell whether they
        # were being quoted their own revision or the prior round's complaint.
        # This is the revision as it now reads, and what it means depends on
        # the ruling: under `unverified` nobody checked it, and the passage
        # standing is the whole of what this round can say about the issue.
        blocks.append((
            'labeled',
            'The prior round\'s evidence still in the manuscript',
            'Quoted from the revision as it now reads. '
            + ('No ruling was made on this issue this round, so the passage standing is all '
               'this round can say about it.' if issue.get('status') == 'unverified' else
               'The ruling above was made with these passages in front of it.')))
        blocks += [('quote', quote) for quote in quotes]
    return blocks


def _prior_round_blocks(document, rows):
    """The prior round: where it came from, how it was ruled, and what closed."""
    prior = document.get('prior_round') or {}
    closed = issue_model.closed_issues(rows)
    if not prior and not closed:
        return []
    blocks = [('heading2', 'Since the prior round')]
    if prior:
        fields = [('Prior round file', prior.get('file')),
                  ('Prior round digest', prior.get('digest')),
                  ('Issues carried in', prior.get('issues'))]
        tally = prior.get('tally') or {}
        fields += [(status.capitalize(), tally[status])
                   for status in issue_model.ISSUE_STATUSES if status in tally]
        blocks.append(('fields', [(key, value) for key, value in fields if value is not None]))
    blocks.append(('bullets', [f'{status}: {meaning}' for status, meaning in _STATUS_MEANINGS]))
    if closed:
        for issue in closed:
            blocks += _closed(issue)
    elif prior:
        blocks.append(('paragraph', 'No prior issue was closed this round.'))
    return blocks


def _escalation_blocks(document, rows):
    """Each escalated finding's ruling, named against the issue that holds it."""
    rulings = issue_model.objects_in(document.get('escalation_rulings'))
    if not rulings:
        return []
    held = {}
    for issue in rows:
        for member in issue_model.members_of(issue):
            if member.get('finding_uid'):
                held[str(member['finding_uid'])] = (issue, member)
    blocks = [('heading2', 'Escalations ruled on')]
    for ruling in rulings:
        uid = str(ruling.get('finding_uid') or '').strip()
        pair = held.get(uid)
        if pair:
            issue, member = pair
            name = str(issue.get('name') or 'Unnamed issue')
            heading = f"{issue['id']}: {name}" if issue.get('id') else name
            title = str(member.get('title') or '')
        else:
            heading = 'Not held by any issue in this review'
            title = ''
        blocks.append(('heading3', heading))
        inline = [('Finding', uid or 'unknown')]
        if title:
            inline.append(('Raised as', title))
        blocks += [('inline', inline), ('paragraph', str(ruling.get('ruling') or ''))]
    return blocks


def _code_check_blocks(document):
    """What reading the code did to the findings that rested on a method.

    Nothing where this run was given no code, which is most of them. Where
    there was code, this is the only place a contradicted finding appears: it
    is in no issue and in no required change, and an author who is told nothing
    about it cannot see that it was raised, read against the code, and thrown
    out. So each one is printed with the passage of the paper it was raised
    against, the passage of the code that decided it, and the one or two
    sentences the check wrote about the difference.

    Confirmed findings are in the issues like any other, and are listed here
    with their code quote so the author can see the method claim was not taken
    on trust. ``not-shown`` says what was searched, because a finding the code
    does not settle stands on the reviewer's reading alone, and the author is
    better off knowing the code was looked at than guessing.
    """
    record = document.get('code_check')
    if not isinstance(record, dict):
        return []
    checks = issue_model.objects_in(record.get('checks'))
    over_cap = int(record.get('over_cap') or 0)
    if not checks and not over_cap:
        return []
    blocks = [('heading2', 'Checked against the code'),
              ('paragraph', 'Each finding below asserted how something was computed, sampled, '
                            'filtered or selected. The code was read at the passage that decides '
                            'it before the finding was allowed into this review.')]
    titles = {'contradicted': 'Thrown out: the code does not do what the finding said',
              'confirmed': 'Confirmed by the code',
              'not-shown': 'The code does not settle it'}
    for verdict in ('contradicted', 'confirmed', 'not-shown'):
        group = [row for row in checks if str(row.get('verdict')) == verdict]
        if not group:
            continue
        blocks.append(('heading3', titles[verdict]))
        if verdict == 'contradicted':
            blocks.append(('paragraph', 'These are in no issue and in no required change. They '
                                        'are printed here because they were raised, and because '
                                        'the reason they were dropped is itself something an '
                                        'author may want to argue with.'))
        for row in group:
            label = ' · '.join(str(part) for part in
                               (row.get('finding_slot'), row.get('finding_track'),
                                _location(row)) if part)
            blocks.append(('paragraph', (label + ': ' + str(row.get('title') or '')).strip(' :')))
            if row.get('quote'):
                blocks.append(('quote', str(row['quote'])))
            evidence = row.get('code_evidence')
            if isinstance(evidence, dict) and evidence.get('text'):
                where = str(evidence.get('path') or '')
                start, end = evidence.get('start_line'), evidence.get('end_line')
                if start:
                    where += ' lines ' + str(start) + ('–' + str(end) if end and end != start
                                                       else '')
                blocks += [('labeled', 'Code', where.strip()),
                           ('quote', str(evidence['text']))]
            searched = [str(one) for one in (row.get('searches') or []) if str(one).strip()]
            if searched:
                blocks.append(('labeled', 'Searched', '; '.join(searched)))
            if row.get('note'):
                blocks.append(('paragraph', str(row['note'])))
    if over_cap:
        blocks.append(('labeled', 'Not checked',
                       f'{over_cap} further finding(s) rested on a method and were past the '
                       f"stage's limit of {record.get('cap')}. They were left exactly as their "
                       'reviewers filed them, and the code was not read against them.'))
    return blocks


def _verdict_blocks(document):
    verdict = document.get('verdict') or {}
    if not verdict:
        return []
    fields = [('Significance', verdict.get('significance')),
              ('Strength of evidence', verdict.get('strength_of_evidence'))]
    blocks = [('heading2', 'Verdict'),
              ('fields', [(key, value) for key, value in fields if value])]
    if verdict.get('reasoning'):
        blocks.append(('paragraph', verdict['reasoning']))
    if verdict.get('limitation'):
        blocks.append(('labeled', 'Limitation', verdict['limitation']))
    return blocks


def _issue_review(document):
    """The review itself, from the issues document and from nothing else."""
    # The rows every block below is handed come through the shared guard, not
    # off the document: an `issues` that was not a list reached the escalation
    # and prior-round blocks raw and was iterated there, so a damaged document
    # ended `write_reports` instead of being rendered as far as it reads.
    rows = issue_model.rows_of(document)
    counts = document.get('counts') or issue_model.counts(rows)
    blocks = [('heading2', 'Review')]
    if document.get('summary'):
        blocks.append(('paragraph', document['summary']))
    blocks.append(('inline', [('Issues', counts.get('issues', 0)),
                              ('Required changes', counts.get('required', 0)),
                              ('Suggestions', counts.get('suggestions', 0)),
                              ('Findings pooled', counts.get('issue_members', 0))]))
    if document.get('issues_source') == 'server':
        # The cause is the run's to state. This label read "No editor in this
        # arm" over every synthesised file, including the runs whose arm has an
        # editor that was refused the slot over the size of the ledger.
        blocks.append(('labeled', 'Nobody grouped these findings',
                       (str(document['issues_source_reason']).rstrip('.') + '. '
                        if document.get('issues_source_reason') else '')
                       + 'Each finding is its own issue and the ranking is by severity alone.'))
    blocks += _verdict_blocks(document)
    required = issue_model.required_issues(rows)
    blocks.append(('heading2', 'Required changes'))
    if not required:
        blocks.append(('paragraph', 'No major or moderate issues were raised.'))
    for issue in required:
        blocks += _issue(issue)
    suggestions = issue_model.suggestion_issues(rows)
    blocks.append(('heading2', 'Suggestions'))
    if not suggestions:
        blocks.append(('paragraph', 'No minor issues were raised.'))
    for category, group in issue_model.by_category(suggestions):
        blocks.append(('heading3', category.replace('-', ' ').capitalize()))
        for issue in group:
            blocks += _issue(issue)
    blocks += _prior_round_blocks(document, rows)
    blocks += _escalation_blocks(document, rows)
    blocks += _code_check_blocks(document)
    if document.get('limitations'):
        blocks += [('heading2', 'Limitations'),
                   ('bullets', [str(item) for item in document['limitations']])]
    return blocks


def _review(heading, review):
    blocks = [('heading2', heading)]
    if review.get('summary'):
        blocks.append(('paragraph', review['summary']))
    if review.get('investigation_summary'):
        blocks.append(('labeled', 'Investigation', review['investigation_summary']))
    findings = review.get('findings') or []
    if not findings:
        blocks.append(('paragraph', 'No findings were retained.'))
    for number, finding in enumerate(findings, 1):
        blocks += _finding(number, finding)
    if review.get('limitations'):
        blocks += [('heading3', 'Limitations'), ('bullets', [str(item) for item in review['limitations']])]
    return blocks


def _code_line(progress):
    """What the page says about the code this paper was checked against.

    ``None`` for a run that was given none, which is most of them: a row
    reading "Code reviewed: no" on every other review tells a reader nothing.
    Where there was code, the line is what an author needs in order to answer
    back — which code, at which commit, and how much of it a reviewer opened,
    because "the code was reviewed" over a lane that opened one file of forty
    is a claim the folder does not support.

    A local folder is named by its own name and not its path, for the reason
    ``Run folder`` is: REVIEW.md goes to authors and editors, and the absolute
    path names the home directory of whoever ran the review. A URL is printed
    whole, because that is the name of the remote and the authors' own.
    """
    record = progress.get('code')
    if not isinstance(record, dict) or not record.get('reviewed'):
        return None
    source = str(record.get('source') or '')
    if record.get('kind') == 'folder':
        source = Path(source).name if source else ''
    line = source or 'the code this run was given'
    commit = str(record.get('commit') or '')
    if commit:
        line += ' at ' + commit[:12]
        if record.get('dirty'):
            line += ' with uncommitted changes'
    files, opened = record.get('files_in_snapshot'), record.get('opened')
    if isinstance(files, int) and isinstance(opened, int):
        line += f', {opened} of {files} file(s) opened'
    listed, got = record.get('data_listed'), record.get('data_fetched')
    if isinstance(listed, int) and listed and isinstance(got, int):
        # Data is listed and fetched on demand, so the honest number is how
        # much of what was there a reviewer actually asked for — and a file
        # read only as far as its head, or refused, is not a file read.
        line += f', {got} of {listed} data file(s) fetched'
        partial, refused = record.get('data_partial'), record.get('data_refused')
        said = [f'{partial} partial' if isinstance(partial, int) and partial else '',
                f'{refused} refused' if isinstance(refused, int) and refused else '']
        said = [one for one in said if one]
        if said:
            line += ' (' + ', '.join(said) + ')'
    skipped = record.get('files_skipped')
    if isinstance(skipped, int) and skipped:
        # Paths, not files: a folder never walked into is one entry, which is
        # what keeps a vendored dependency tree from being ten thousand.
        line += f', {skipped} path(s) left out of the snapshot'
    truncated = str(record.get('truncated') or '')
    if truncated:
        line += f' ({truncated})'
    source_cut = record.get('source_cut')
    warning = code_source.SOURCE_CUT_WARNING
    if isinstance(source_cut, int) and source_cut and warning not in truncated:
        # A cap that cut program source changes what the review may claim, so
        # the line that says what was reviewed says it even where the
        # snapshot's own words did not.
        line += f' — {source_cut} program source file(s) left out: ' + warning
    return line


def report_blocks(run, workspace=None):
    """Collect the run details, final review and each independent reviewer as renderable blocks."""
    run = Path(run).resolve()
    protocol = _optional(run / 'protocol.json')
    if protocol is None:
        # A run that stopped before the server recorded its protocol is still a
        # run folder, and the reason it stopped is the whole point of rendering
        # it. Only a folder that recorded nothing at all is refused.
        if not any((run / name).is_file() for name in RUN_FILES):
            raise ValueError('Not a review run folder: none of ' + ', '.join(RUN_FILES)
                             + ' is in it')
        protocol = {}
    paper_id = (_optional(run / 'manifest.json') or {}).get('paper_id') or protocol.get('paper_id')
    workspace = Path(workspace).resolve() if workspace else find_workspace(run)
    # The manuscript as its workspace records it: the manifest carries the
    # paper id, the title and where the source came from.
    paper = _optional(workspace / 'manifest.json') if workspace else None
    if workspace and paper is None:
        # The workspace is where the manuscript was imported, and a run that
        # never got that far has none. A caller naming one that does not exist
        # yet asked for the reports of this run, not for an exception: the run
        # folder is enough, and the paper fields are left out of the page.
        workspace = find_workspace(run)
        paper = _optional(workspace / 'manifest.json') if workspace else None
    if paper and paper_id and paper.get('paper_id') != paper_id:
        raise ValueError('The workspace does not match the reviewed manuscript')
    reviewers = _reviewer_packages(run)
    progress = _optional(run / 'progress.json') or {}
    runtime = runtime_usage(run, on_problem=lambda message: None) or {}
    startup = _optional(run / 'startup.json') or {}
    # One rule decides the status, and `read_run` uses the same one: a page
    # that says blocked while REVIEW.md beside it says complete is worse than
    # either answer. See `peerreviewagents.runs.run_status`.
    from ..runs import run_status
    state = run_status(progress, runtime, startup)
    status = state['status']
    if status == 'running':
        status += ' (last recorded state, the run may have stopped)'
    usage = observed_usage(run, on_problem=lambda message: None)
    agents = agent_rows(run, protocol, runtime=runtime)
    # The plan and the observation, side by side and never merged: a page that
    # printed one list would be saying the run ran what it asked for.
    asked = sorted({row['assigned_model'] for row in agents if row.get('assigned_model')})
    models = sorted({row['model'] for row in agents if row.get('model')})
    if state['cause']:
        status = f"{status}: {state['cause']}"
    elif state['note']:
        status = f"{status} ({state['note']})"
    fields = [('Paper', (paper or {}).get('title')), ('Paper ID', paper_id),
              ('Source', (paper or {}).get('source_url')),
              ('Workflow', _workflow(protocol)),
              ('Models asked for', ', '.join(asked) or None),
              ('Models that ran', ', '.join(models) or None),
              ('Literature cutoff', protocol.get('literature_cutoff')),
              ('Code reviewed', _code_line(progress)),
              ('Run status', status),
              ('Agents', len(agents) or None),
              ('Server tool calls', _total(agents, 'tool_calls')),
              ('Session tool calls', _total(agents, 'session_calls')),
              ('Tokens', _tokens(runtime, usage)),
              ('Wall time', _duration(runtime.get('wall_seconds'))),
              ('Claude Code', runtime.get('claude_code_version')),
              # The folder's own name, not the path to it. REVIEW.md is handed
              # to authors and editors, and the absolute path names the home
              # directory of whoever ran the review — which is neither theirs
              # nor anything they can use.
              ('Run folder', _run_folder(run))]
    title = (f"Review: {paper['title']}" if paper and paper.get('title')
             else f'Review run {_run_folder(run)}')
    blocks = [('title', title), ('fields', [(key, value) for key, value in fields if value not in (None, '')])]
    if usage.get('usage_limitation'):
        blocks.append(('labeled', 'Usage limitation', usage['usage_limitation']))
    for limit in runtime.get('rate_limits') or []:
        blocks.append(('labeled', 'Rate limit', str(limit.get('reason'))))
    blocks += _agent_table(agents)
    # A slot nothing observed does not hold a review back, but a page showing
    # `Model asked` for it and nothing beside it would read as agreement. The
    # line says plainly that those slots are unverified. See `models.unverified`.
    missing = model_plan.unverified(agents)
    if missing:
        blocks.append(('labeled', 'Models unverified',
                       model_plan.unverified_line(missing).capitalize() + '.'))
    blocks += _citation_blocks(run)
    document = _issues_document(run)
    if document or reviewers:
        blocks.append(('paragraph', NOTICE))
    if progress.get('error') and progress.get('status') != 'complete':
        blocks.append(('labeled', 'Run error', progress['error']))
    blocks += (_issue_review(document) if document else
               [('heading2', 'Review'), ('paragraph', 'No review was produced.')])
    for number, review in reviewers:
        blocks += _review(f'Reviewer {number} (independent)', review)
    return blocks


def _cell(value):
    return ' '.join(str(value).split()).replace('|', '\\|')


def render_markdown(blocks):
    lines = []
    for kind, *content in blocks:
        if kind in ('title', 'heading2', 'heading3'):
            lines += [{'title': '#', 'heading2': '##', 'heading3': '###'}[kind] + ' ' + content[0], '']
        elif kind == 'fields':
            lines += ['| Detail | Value |', '| --- | --- |']
            lines += [f'| {key} | {_cell(value)} |' for key, value in content[0]] + ['']
        elif kind == 'table':
            headers, rows = content
            lines += ['| ' + ' | '.join(_cell(head) for head in headers) + ' |',
                      '| ' + ' | '.join('---' for _ in headers) + ' |']
            lines += ['| ' + ' | '.join(_cell(value) for value in row) + ' |' for row in rows] + ['']
        elif kind == 'inline':
            lines += [' · '.join(f'**{key}:** {value}' for key, value in content[0]), '']
        elif kind == 'paragraph':
            lines += [str(content[0]).strip(), '']
        elif kind == 'labeled':
            lines += [f'**{content[0]}:** {str(content[1]).strip()}', '']
        elif kind == 'quote':
            lines += ['> ' + line if line.strip() else '>' for line in str(content[0]).strip().splitlines()] + ['']
        elif kind == 'bullets':
            lines += [f'- {" ".join(str(item).split())}' for item in content[0]] + ['']
    return '\n'.join(lines).rstrip() + '\n'


def _wrap(text, first='', rest=None, width=WIDTH):
    rest = first if rest is None else rest
    lines = []
    for index, paragraph in enumerate(str(text).strip().splitlines() or ['']):
        indent = first if index == 0 else rest
        lines += textwrap.wrap(paragraph, width, initial_indent=indent, subsequent_indent=rest,
                               break_long_words=False, break_on_hyphens=False) or [indent.rstrip()]
    return lines


def render_text(blocks, width=WIDTH):
    lines = []
    for kind, *content in blocks:
        if kind in ('title', 'heading2'):
            rule = '=' if kind == 'title' else '-'
            lines += _wrap(content[0], width=width) + [rule * min(len(content[0]), width), '']
        elif kind == 'heading3':
            lines += _wrap(content[0], width=width) + ['']
        elif kind == 'fields':
            pad = max(len(key) for key, _ in content[0]) + 2
            for key, value in content[0]:
                lines += _wrap(' '.join(str(value).split()), f'{key + ":":<{pad}}', ' ' * pad, width)
            lines.append('')
        elif kind == 'table':
            cells = [[_cell(value) for value in row] for row in (content[0], *content[1])]
            pad = [max(len(row[column]) for row in cells) for column in range(len(content[0]))]
            for index, row in enumerate(cells):
                lines.append('  '.join(value.ljust(pad[column])
                                       for column, value in enumerate(row)).rstrip())
                if index == 0:
                    lines.append('  '.join('-' * size for size in pad))
            lines.append('')
        elif kind == 'inline':
            lines += _wrap(' | '.join(f'{key}: {value}' for key, value in content[0]), width=width) + ['']
        elif kind == 'paragraph':
            lines += _wrap(content[0], width=width) + ['']
        elif kind == 'labeled':
            lines += _wrap(f'{content[0]}: {str(content[1]).strip()}', width=width) + ['']
        elif kind == 'quote':
            lines += _wrap(content[0], '    ', width=width) + ['']
        elif kind == 'bullets':
            for item in content[0]:
                lines += _wrap(' '.join(str(item).split()), '- ', '  ', width)
            lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def write_reports(run, workspace=None, output_dir=None):
    """Write REVIEW.md and REVIEW.txt with the same content. Model calls are never made."""
    blocks = report_blocks(run, workspace)
    target = Path(output_dir).resolve() if output_dir else Path(run).resolve()
    target.mkdir(parents=True, exist_ok=True)
    paths = {'markdown': target / 'REVIEW.md', 'text': target / 'REVIEW.txt'}
    paths['markdown'].write_text(render_markdown(blocks), encoding='utf-8')
    paths['text'].write_text(render_text(blocks), encoding='utf-8')
    return paths
