"""Run the Stage A matrix, one review at a time, and score each run as it lands.

The matrix is the comparison Stage A exists for: the same planted paper put
through three arms, so the cost of the full two-track review can be set against
what a single reviewer finds on its own.

    stage        what it runs
    pilot        the first two planted papers, each in all three arms
    main         the other eight planted papers, each in all three arms
    control      the two unaltered papers, full-two-track only
    reviewers1   reviewers-only with one reviewer, on three of the papers,
                 against the three-reviewer reviewers-only run of the same
                 paper that `main` or `pilot` already did

Rules this driver keeps to, because they are the ones that stop a long night of
real runs from going wrong:

* One review at a time. Never two at once, never a retry loop.
* A run that comes back `blocked` stops the matrix there. Whatever has already
  been scored stays scored; nothing is thrown away and nothing is retried.
* A token ceiling, checked after every run against the run's own accounting.
* Every run is scored immediately and appended to `runs/results.jsonl`, so an
  interrupted matrix is still a result.

Nothing here is a judgement about a run; scoring is `score.py`'s written rule.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import score as scoring  # noqa: E402

ROOT = Path('/home/ty/pra-stage-a')
ARMS = ('single', 'reviewers-only', 'full-two-track')
CEILING = 20_000_000
# The brief's expectation, for the progress line: about 12 million in total.
EXPECTED = 12_000_000
PILOT = 2
REVIEWERS1 = 3
STAGES = ('pilot', 'main', 'control', 'reviewers1')


def planted(root: Path) -> list:
    """Every planted paper, as (slug, kind, altered pdf, key), in key order."""
    out = []
    for path in sorted((Path(root) / 'keys').glob('*.json')):
        key = json.loads(path.read_text(encoding='utf-8'))
        pdf = Path(key['altered_pdf'])
        if pdf.is_file():
            out.append({'paper': key['paper'], 'kind': key['kind'],
                        'pdf': str(pdf), 'key': str(path)})
    return out


def controls(root: Path, slugs: tuple) -> list:
    """The unaltered papers, straight from the candidate folder."""
    out = []
    for slug in slugs:
        pdf = Path(root) / 'candidates' / f'{slug}.pdf'
        if pdf.is_file():
            out.append({'paper': slug, 'kind': 'control', 'pdf': str(pdf), 'key': ''})
    return out


def job(paper: dict, arm: str, reviewers: int, root: Path, stage: str) -> dict:
    name = f'{paper["paper"]}--{paper["kind"]}--{arm}--r{reviewers}'
    return {
        'id': name, 'stage': stage, 'paper': paper['paper'], 'kind': paper['kind'],
        'arm': arm, 'reviewers': reviewers, 'pdf': paper['pdf'], 'key': paper['key'],
        'run': str(Path(root) / 'runs' / name),
        'log': str(Path(root) / 'logs' / f'{name}.log'),
    }


def jobs(root: Path, stages=STAGES, control_slugs=()) -> list:
    """The whole matrix in the order it should run."""
    papers = planted(root)
    out = []
    for index, paper in enumerate(papers):
        stage = 'pilot' if index < PILOT else 'main'
        if stage not in stages:
            continue
        for arm in ARMS:
            out.append(job(paper, arm, 3, root, stage))
    if 'control' in stages:
        for paper in controls(root, control_slugs):
            out.append(job(paper, 'full-two-track', 3, root, 'control'))
    if 'reviewers1' in stages:
        for paper in papers[:REVIEWERS1]:
            out.append(job(paper, 'reviewers-only', 1, root, 'reviewers1'))
    return out


def already_done(run: Path) -> bool:
    """Has this run folder already finished? A finished run is never redone.

    The reading is :func:`read_run`'s, so a folder whose status word this
    version does not know, or whose ``runtime.json`` was truncated by a kill,
    reads as ``unknown`` and is run again rather than counted as finished.
    """
    if not Path(run).is_dir():
        return False
    reading = scoring.reading_of(Path(run))
    return reading['status'] in ('complete', 'complete-without-findings', 'blocked')


def execute(spec: dict, peerreview: str, timeout: int) -> int:
    """Run one review, with its output kept in a log file beside the run."""
    run, log = Path(spec['run']), Path(spec['log'])
    run.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    command = [peerreview, 'review', spec['pdf'], str(run),
               '--arm', spec['arm'], '--reviewers', str(spec['reviewers'])]
    with log.open('w', encoding='utf-8') as handle:
        handle.write(' '.join(command) + '\n\n')
        handle.flush()
        finished = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT,
                                  timeout=timeout, check=False)
    return finished.returncode


def result(spec: dict, exit_code: int) -> dict:
    """Score the run that just finished, by the written rule.

    The folder is read once, with :func:`read_run`, and the reading is handed to
    the scorer, so the status, the token counts and the findings in a row all
    come from the same reading of the same folder.
    """
    run = Path(spec['run'])
    reading = scoring.reading_of(run)
    if spec['key']:
        key = json.loads(Path(spec['key']).read_text(encoding='utf-8'))
        row = scoring.score(run, key, reading)
    else:
        row = scoring.score_control(run, spec['paper'], reading)
    row.update({'id': spec['id'], 'stage': spec['stage'], 'arm': spec['arm'],
                'reviewers': spec['reviewers'], 'exit_code': exit_code})
    return row


def record(root: Path, row: dict) -> None:
    path = Path(root) / 'runs' / 'results.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row) + '\n')


def rescore(root: Path, specs: list) -> dict:
    """Score every run that is already on disk again, and rewrite the results.

    The scoring rule is written down and it gets corrected — the pilot's first
    run showed that a short planted value matched as a run of digits — and a
    corrected rule has to be applied to every run, not only the ones that run
    after it. No review is run here and no run folder is touched.
    """
    state = {'root': str(root), 'planned': len(specs), 'ran': 0, 'skipped': 0,
             'tokens': 0, 'stopped_because': '', 'rows': []}
    lines = []
    for spec in specs:
        if not already_done(Path(spec['run'])):
            continue
        row = result(spec, 0)
        state['skipped'] += 1
        state['tokens'] += row.get('tokens') or 0
        state['rows'].append(row)
        lines.append(json.dumps(row))
        print(line(row))
    path = Path(root) / 'runs' / 'results.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(text + '\n' for text in lines), encoding='utf-8')
    print(f'\nrescored {state["skipped"]} run(s) into {path}')
    return state


def line(row: dict) -> str:
    """One line a person can read while the matrix is running."""
    caught = row.get('caught')
    verdict = ('caught' if caught else 'missed') if row['kind'] != 'control' else \
        f'{row.get("planted_style_allegations", 0)} allegations'
    # `publishable` is printed because a row scored from a folder that could not
    # be read in full is not the same evidence as a row scored from a whole one.
    flag = 'publishable' if row.get('publishable') else \
        f'NOT publishable ({len(row.get("problems") or [])} problems)'
    return (f'{row["id"]:70} {row.get("status") or "?":8} {verdict:14} '
            f'{row.get("findings_total", 0):3} findings  '
            f'{row.get("tokens", 0):>9,} tokens  {row.get("minutes", 0):5.1f} min  {flag}')


def drive(root: Path, stages=STAGES, control_slugs=(), peerreview='peerreview',
          ceiling=CEILING, timeout=7200, dry_run=False, only='', limit=0,
          rescore_only=False) -> dict:
    """Run the matrix in order and stop on the first rule that says to stop."""
    queue = [spec for spec in jobs(root, stages, control_slugs)
             if not only or only in spec['id']]
    if limit:
        queue = queue[:limit]
    state = {'root': str(root), 'planned': len(queue), 'ran': 0, 'skipped': 0,
             'tokens': 0, 'stopped_because': '', 'rows': []}
    if dry_run:
        for spec in queue:
            print(f'{spec["stage"]:10} {spec["id"]}')
        return state
    if rescore_only:
        return rescore(root, queue)
    for spec in queue:
        if already_done(Path(spec['run'])):
            row = result(spec, 0)
            state['skipped'] += 1
            state['tokens'] += row.get('tokens') or 0
            state['rows'].append(row)
            print('(already done) ' + line(row))
            continue
        started = time.time()
        try:
            exit_code = execute(spec, peerreview, timeout)
        except subprocess.TimeoutExpired:
            exit_code = -1
        row = result(spec, exit_code)
        row['driver_seconds'] = round(time.time() - started, 1)
        record(root, row)
        state['ran'] += 1
        state['tokens'] += row.get('tokens') or 0
        state['rows'].append(row)
        print(line(row))
        if (row.get('status') or '') == 'blocked':
            state['stopped_because'] = f'blocked: {row.get("blocked_reason") or "unknown"}'
            break
        if state['tokens'] > ceiling:
            state['stopped_because'] = f'token ceiling {ceiling:,} passed'
            break
    summary = Path(root) / 'runs' / 'matrix-state.json'
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({**state, 'rows': [row['id'] for row in state['rows']]},
                                  indent=1), encoding='utf-8')
    print(f'\nran {state["ran"]}, already done {state["skipped"]}, '
          f'{state["tokens"]:,} tokens of an expected {EXPECTED:,}'
          + (f'; stopped: {state["stopped_because"]}' if state['stopped_because'] else ''))
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='matrix.py', description='Run and score the Stage A matrix, one review at a time.')
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--stage', action='append', choices=list(STAGES),
                        help='repeatable; the default is every stage in order')
    parser.add_argument('--control', action='append', default=[],
                        help='an unaltered paper slug, repeatable')
    parser.add_argument('--peerreview', default='peerreview', help='the CLI to call')
    parser.add_argument('--ceiling', type=int, default=CEILING,
                        help='stop once the runs have cost this many tokens')
    parser.add_argument('--timeout', type=int, default=7200, help='seconds per run')
    parser.add_argument('--only', default='', help='only jobs whose id contains this')
    parser.add_argument('--limit', type=int, default=0, help='at most this many jobs')
    parser.add_argument('--dry-run', action='store_true', help='print the order, run nothing')
    parser.add_argument('--rescore', action='store_true',
                        help='score the runs already on disk again and rewrite results.jsonl')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    state = drive(Path(args.root), tuple(args.stage or STAGES), tuple(args.control),
                  args.peerreview, args.ceiling, args.timeout, args.dry_run,
                  args.only, args.limit, args.rescore)
    return 1 if state['stopped_because'] else 0


if __name__ == '__main__':
    sys.exit(main())
