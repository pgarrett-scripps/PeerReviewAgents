"""Readable Markdown and plain-text reports of a review run, written from its saved JSON.

Nothing here makes a model call. Everything on the page comes from files the run
wrote about itself: ``protocol.json`` for the arm and the agent slots,
``runtime.json`` and the usage receipts for what the run really spent,
``citations/ledger.jsonl`` for what the citation checks reached, and
``editor-package.json`` for the review.
"""
import textwrap
from pathlib import Path

from ..plugin import models as model_plan
from . import citation_ledger
from .storage import read
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
        if (candidate / 'paper.json').is_file():
            return candidate
    return None


def _reviewer_packages(run):
    folders = [path for path in run.glob('reviewer-*') if path.name.split('-', 1)[1].isdigit()]
    packages = [(int(path.name.split('-', 1)[1]), _optional(path / 'editor-package.json'))
                for path in sorted(folders, key=lambda path: int(path.name.split('-', 1)[1]))]
    return [(number, package) for number, package in packages if isinstance(package, dict)]


def _final_review(run):
    """The editor's pooled package. An arm that runs without an editor writes none."""
    package = _optional(run / 'editor-package.json')
    return package if isinstance(package, dict) else None


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
            ('Ledger', str(Path(run) / citation_ledger.LEDGER_PATH))]
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
    return f"block {finding['block_id']}" if finding.get('block_id') else 'not located'


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
    paper = _optional(workspace / 'paper.json') if workspace else None
    if workspace and paper is None:
        # The workspace is where the manuscript was imported, and a run that
        # never got that far has none. A caller naming one that does not exist
        # yet asked for the reports of this run, not for an exception: the run
        # folder is enough, and the paper fields are left out of the page.
        workspace = find_workspace(run)
        paper = _optional(workspace / 'paper.json') if workspace else None
    if paper and paper_id and paper.get('id') != paper_id:
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
              ('Run status', status),
              ('Agents', len(agents) or None),
              ('Server tool calls', _total(agents, 'tool_calls')),
              ('Session tool calls', _total(agents, 'session_calls')),
              ('Tokens', _tokens(runtime, usage)),
              ('Wall time', _duration(runtime.get('wall_seconds'))),
              ('Claude Code', runtime.get('claude_code_version')),
              ('Run folder', str(run))]
    title = f"Review: {paper['title']}" if paper and paper.get('title') else f'Review run {run.name}'
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
    final = _final_review(run)
    if final or reviewers:
        blocks.append(('paragraph', NOTICE))
    if progress.get('error') and progress.get('status') != 'complete':
        blocks.append(('labeled', 'Run error', progress['error']))
    blocks += (_review('Final review (editor)', final) if final else
               [('heading2', 'Review'), ('paragraph', 'No final review was produced.')])
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
