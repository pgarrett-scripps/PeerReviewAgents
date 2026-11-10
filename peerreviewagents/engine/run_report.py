"""Readable Markdown and plain-text reports of a review run, written from its saved JSON."""
import textwrap
from pathlib import Path

from .storage import read

WIDTH = 88
NOTICE = ('This review was generated automatically. Exact quotations and issue accounting were validated. '
          'Scientific correctness was not.')


def _optional(path):
    path = Path(path)
    return read(path) if path.is_file() else None


def find_workspace(run):
    """Locate the reviewed workspace through a tool configuration saved with the run."""
    for config in sorted(Path(run).glob('**/execution/workspace/tool-config.json')):
        paper = read(config).get('paper')
        if paper and (Path(paper) / 'paper.json').is_file():
            return Path(paper)
    return None


def _reviewer_packages(run):
    folders = [path for path in run.glob('reviewer-*') if path.name.split('-', 1)[1].isdigit()]
    return [(int(path.name.split('-', 1)[1]), read(path / 'editor-package.json'))
            for path in sorted(folders, key=lambda path: int(path.name.split('-', 1)[1]))
            if (path / 'editor-package.json').is_file()]


def _final_review(run, mode):
    """Ensemble, independent, panel and self-revision runs save an editor package. Plain and workspace runs do not."""
    for path in (run / 'editor-package.json', run / 'responses' / f'{mode}-guarded.json'):
        if path.is_file():
            return read(path)
    return None


def _workflow(protocol, reviewers, synthesis):
    if protocol.get('mode') == 'ensemble':
        policy = 'ordinary' if synthesis is not None and synthesis.name == 'ordinary-synthesis' else 'debate'
        count = protocol.get('reviewers') or reviewers
        return f'Ensemble: {count} independent reviewers, then one {policy} synthesis'
    return f"{protocol.get('mode', 'unknown')} review"


def _ledger(validation):
    ledger = (validation or {}).get('component_ledger')
    if not ledger:
        return None
    verdict = 'valid' if validation.get('valid') else 'invalid'
    return (f"{ledger.get('accounted_components')} of {ledger.get('expected_components')} components "
            f'accounted, synthesis {verdict}')


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
    from .ensemble import observed_usage
    run = Path(run).resolve()
    protocol = _optional(run / 'protocol.json')
    if protocol is None:
        raise ValueError('Not a review run folder: protocol.json is missing')
    paper_id = (_optional(run / 'manifest.json') or {}).get('paper_id') or protocol.get('paper_id')
    workspace = Path(workspace).resolve() if workspace else find_workspace(run)
    paper = _optional(workspace / 'paper.json') if workspace else None
    if workspace and paper is None:
        raise ValueError('The workspace has no paper.json')
    if paper and paper_id and paper.get('id') != paper_id:
        raise ValueError('The workspace does not match the reviewed manuscript')
    reviewers = _reviewer_packages(run)
    synthesis = next((run / name for name in ('debate', 'ordinary-synthesis') if (run / name).is_dir()), None)
    first = _optional(run / 'reviewer-1' / 'protocol.json') or {}
    progress = _optional(run / 'progress.json') or {}
    status = progress.get('status', 'unknown')
    if status == 'running':
        status += ' (last recorded state, the run may have stopped)'
    usage = observed_usage(run)
    tokens = f"{usage['observed_tokens']:,}" + (' recorded, some usage is unknown' if usage['unknown'] else '')
    fields = [('Paper', (paper or {}).get('title')), ('Paper ID', paper_id),
              ('Source', (paper or {}).get('source_url')),
              ('Workflow', _workflow(protocol, len(reviewers), synthesis)),
              ('Model', f"{protocol.get('model')} ({protocol.get('provider')}, {protocol.get('effort')} effort)"),
              ('Literature cutoff', protocol.get('literature_cutoff') or first.get('literature_cutoff')),
              ('Run status', status), ('Model calls', usage['calls']), ('Observed tokens', tokens),
              ('Component ledger', _ledger(_optional(synthesis / 'synthesis-validation.json') if synthesis else None)),
              ('Run folder', str(run))]
    title = f"Review: {paper['title']}" if paper and paper.get('title') else f'Review run {run.name}'
    blocks = [('title', title), ('fields', [(key, value) for key, value in fields if value not in (None, '')])]
    final = _final_review(run, protocol.get('mode'))
    if final or reviewers:
        blocks.append(('paragraph', NOTICE))
    if progress.get('error') and progress.get('status') != 'complete':
        blocks.append(('labeled', 'Run error', progress['error']))
    heading = 'Final review (synthesis)' if protocol.get('mode') == 'ensemble' or reviewers or synthesis else 'Review'
    blocks += _review(heading, final) if final else [('heading2', heading), ('paragraph', 'No final review was produced.')]
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
    paths['markdown'].write_text(render_markdown(blocks))
    paths['text'].write_text(render_text(blocks))
    return paths
