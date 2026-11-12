"""Start one plugin review in a headless Claude Code session and account for it.

The server owns the review; this starter owns the launch and the accounting.
It does four things a workflow script cannot:

* **Probes first.** A run is only started once a throwaway session's start-up
  event shows the plugin loaded and ``peerreview`` connected with its tools
  under the names the agents are allowed to call. A silent misconfiguration
  would otherwise look like a review with no findings.
* **Never uses bare mode.** ``--bare`` skips plugin sync and reads credentials
  only from an API key, so it is both useless here and the wrong login.
* **Reads the whole stream.** Under ``-p`` the Workflow tool is a background
  task: the first ``result`` event arrives while the review is still running.
  The starter waits for the task notification and keeps the last result.
* **Enforces budgets and records provenance from the stream.**
  ``system/task_progress`` carries each agent's tokens, tool calls, state and
  model, so a per-agent or per-run token ceiling is enforced here, and the model
  each agent actually ran as is recorded rather than taken from its own word.

A rate limit, a breached budget or a missing stage leaves the run ``blocked``,
which is an outcome and not an error to paper over.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

from ..engine.storage import read, write
from .server import ARMS, DEFAULT_ARM, RUN_DIRECTORY, SPECIALIST_TRACKS, TOOLS

SERVER = 'peerreview'
PLUGIN = 'pra'
TOOL_PREFIX = f'mcp__plugin_{PLUGIN}_{SERVER}__'
HARNESS_TOOLS = ('Workflow', 'Skill', 'TodoWrite')
PER_AGENT_TOKENS = 500_000
PER_RUN_TOKENS = 6_000_000
WALL_TIMEOUT = 5400
PROBE_TIMEOUT = 180
RATE_LIMIT_CEILING = 0.99


def tool_names():
    return tuple(TOOL_PREFIX + tool.__name__ for tool in TOOLS)


def allow_list():
    return ','.join((*HARNESS_TOOLS, *tool_names()))


def plugin_dir():
    return Path(__file__).resolve().parent.parent.parent / 'plugins' / PLUGIN


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def launch(prompt, directory, timeout_at, on_event, cwd):
    """Run one headless session, handing every stream event to ``on_event``.

    ``on_event`` returns a reason to stop, or ``None`` to carry on; a reason
    terminates the session. Returns ``(events_seen, stop_reason, exit_code)``.
    """
    command = [
        'claude', '-p', prompt,
        '--plugin-dir', str(directory),
        '--permission-mode', 'dontAsk',
        '--allowed-tools', allow_list(),
        '--output-format', 'stream-json',
        '--verbose',
        '--forward-subagent-text',
    ]
    environment = {**os.environ, 'PEERREVIEW_PLUGIN_RUN': '1'}
    environment.pop('ANTHROPIC_API_KEY', None)  # subscription login only
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, bufsize=1, cwd=str(cwd), env=environment)
    except (FileNotFoundError, PermissionError) as exc:
        # No Claude Code on this machine is a blocked run, not a traceback.
        return 0, 'claude could not be started: ' + str(exc), 127
    seen, stop = 0, None
    try:
        while True:
            if time.monotonic() > timeout_at:
                stop = 'the session passed its wall-clock limit'
                break
            ready = select.select([process.stdout], [], [], 5)[0]
            if not ready:
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            seen += 1
            stop = on_event(event)
            if stop:
                break
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
        rest = process.stderr.read() if process.stderr else ''
        if rest.strip():
            sys.stderr.write(rest[-4000:])
    return seen, stop, process.returncode


def probe(directory, cwd):
    """Assert the plugin and the server are really there before spending a run."""
    found = {'plugin': False, 'server': False, 'tools': set(), 'agents': set(), 'raw': None}
    expected = set(tool_names())

    def on_event(event):
        if event.get('type') != 'system' or event.get('subtype') != 'init':
            return None
        found['raw'] = {key: event.get(key) for key in
                        ('plugins', 'mcp_servers', 'agents', 'slash_commands')}
        text = json.dumps(event)
        found['plugin'] = PLUGIN in text
        for server in event.get('mcp_servers') or []:
            name = str(server.get('name', ''))
            if SERVER in name and str(server.get('status', '')) == 'connected':
                found['server'] = True
        found['tools'] = {name for name in (event.get('tools') or []) if name in expected}
        found['agents'] = {name for name in (event.get('agents') or []) if PLUGIN in str(name)}
        return 'probe complete'

    deadline = time.monotonic() + PROBE_TIMEOUT
    launch('Reply with the single word ready.', directory, deadline, on_event, cwd)
    problems = []
    if not found['plugin']:
        problems.append(f'the {PLUGIN} plugin was not loaded')
    if not found['server']:
        problems.append(f'the {SERVER} MCP server did not connect')
    missing = expected - found['tools']
    if missing:
        problems.append(f'{len(missing)} server tool(s) absent: ' + ', '.join(sorted(missing)[:6]))
    return problems, found


def rate_limit_reason(event):
    """A rate limit, read from whatever shape the event arrives in."""
    text = json.dumps(event)
    if 'unifiedWindows' not in text and 'rate_limit' not in text:
        return None
    for marker in ('limit_reached', 'rate_limit_exceeded', 'exceeded', 'upgrade'):
        if marker in text:
            return 'the subscription rate limit was reached: ' + marker
    windows = None
    for holder in (event, event.get('rate_limit') or {}, event.get('data') or {}):
        if isinstance(holder, dict) and isinstance(holder.get('unifiedWindows'), dict):
            windows = holder['unifiedWindows']
    for name, window in (windows or {}).items():
        used = (window or {}).get('utilization')
        if isinstance(used, (int, float)) and used >= RATE_LIMIT_CEILING:
            return f'the {name} rate-limit window is at {used:.0%}'
    return None


class Accounting:
    """Per-agent tokens, tool calls and models, taken from the progress stream."""

    def __init__(self, per_agent, per_run):
        self.per_agent = per_agent
        self.per_run = per_run
        self.agents = {}
        self.results = []
        self.notified = False
        self.rate_limits = []

    def on_event(self, event):
        kind, subtype = event.get('type'), event.get('subtype')
        limit = rate_limit_reason(event)
        if limit:
            self.rate_limits.append({'at': now(), 'reason': limit})
            return limit
        if kind == 'system' and subtype == 'task_progress':
            return self.progress(event)
        if kind == 'system' and subtype == 'task_notification':
            self.notified = True
            return None
        if kind == 'result':
            self.results.append(event)
            if self.notified:
                return 'the workflow task finished'
        return None

    def progress(self, event):
        rows = event.get('workflow_progress')
        if rows is None:
            rows = ((event.get('task') or {}).get('workflow_progress')
                    or (event.get('data') or {}).get('workflow_progress') or [])
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            key = str(row.get('label') or row.get('agentType') or f'agent-{index}')
            if row.get('attempt') and int(row.get('attempt') or 1) > 1:
                key = f"{key}#{row['attempt']}"
            record = self.agents.setdefault(key, {'label': key})
            for field in ('tokens', 'toolCalls', 'durationMs', 'state', 'attempt', 'agentType',
                          'model'):
                if row.get(field) is not None:
                    record[field] = row[field]
        over = [key for key, row in self.agents.items()
                if (row.get('tokens') or 0) > self.per_agent]
        if over:
            return (f'{over[0]} passed the per-agent token ceiling of {self.per_agent:,}')
        if self.total_tokens() > self.per_run:
            return f'the run passed its token ceiling of {self.per_run:,}'
        return None

    def total_tokens(self):
        return sum(row.get('tokens') or 0 for row in self.agents.values())

    def last_result(self):
        return self.results[-1] if self.results else None


def write_usage(review, accounting):
    """Usage the engine's own report reader can find, plus the full stream record."""
    for key, row in accounting.agents.items():
        slot = key.split('#')[0].replace(' ', '-').replace('/', '-')
        folder = review / 'usage' / slot
        folder.mkdir(parents=True, exist_ok=True)
        attempt = int(row.get('attempt') or 1)
        write(folder / f'{slot}-{attempt}.started.json',
              {'label': key, 'attempt': attempt, 'agent': row.get('agentType')})
        with (folder / 'usage.jsonl').open('a') as stream:
            stream.write(json.dumps({'label': key, 'attempt': attempt,
                                     'observed_tokens': row.get('tokens') or 0,
                                     'tool_calls': row.get('toolCalls') or 0,
                                     'model': row.get('model'),
                                     'state': row.get('state')}) + '\n')


def finish(review, status, reason, accounting, wall, result):
    write(review / 'runtime.json', {
        'status': status,
        'blocked_reason': reason,
        'wall_seconds': round(wall, 1),
        'tokens_total': accounting.total_tokens(),
        'agents': sorted(accounting.agents.values(), key=lambda row: row['label']),
        'rate_limits': accounting.rate_limits,
        'per_agent_token_ceiling': accounting.per_agent,
        'per_run_token_ceiling': accounting.per_run,
        'claude_code_version': os.environ.get('PEERREVIEW_CLAUDE_VERSION'),
        'result': {key: result.get(key) for key in ('subtype', 'is_error', 'num_turns',
                                                    'duration_ms', 'session_id')}
        if result else None,
        'workflow_result': (result or {}).get('result') if result else None,
        'recorded': now(),
    })
    write_usage(review, accounting)
    progress = review / 'progress.json'
    if status == 'blocked' and progress.is_file():
        current = read(progress)
        if current.get('status') not in ('complete', 'complete-without-findings'):
            write(progress, {**current, 'status': 'blocked',
                             'error': reason or current.get('error'), 'updated': now()})


def prompt_for(pdf, out, reviewers, arm, cutoff, tracks):
    text = (f'Use the peer-review-manuscript skill from the {PLUGIN} plugin. Review the '
            f'manuscript at {pdf} and write the run folder to {out}. Use {reviewers} core '
            f'reviewer(s) and the {arm} arm.')
    if arm == 'draft-check':
        chosen = ', '.join(tracks or ('stats', 'consistency', 'methods-reporting'))
        text = (f'Use the check-draft skill from the {PLUGIN} plugin. Check the draft at {pdf}, '
                f'write the run folder to {out}, and run these tracks: {chosen}.')
    if cutoff:
        text += f' The literature cutoff is {cutoff}.'
    return text + ' Report exactly what the workflow returns.'


def review(pdf, out, reviewers=3, arm=DEFAULT_ARM, literature_cutoff=None, tracks=None,
           per_agent_tokens=PER_AGENT_TOKENS, per_run_tokens=PER_RUN_TOKENS,
           wall_timeout=WALL_TIMEOUT, directory=None, cwd=None):
    if arm not in ARMS:
        raise SystemExit(f'Unknown arm {arm}. Arms: ' + ', '.join(sorted(ARMS)))
    source = Path(pdf).resolve()
    if not source.is_file():
        raise SystemExit(f'No manuscript at {source}')
    root = Path(out).resolve()
    review_dir = root / RUN_DIRECTORY
    review_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(directory) if directory else plugin_dir()
    cwd = Path(cwd) if cwd else Path(__file__).resolve().parent.parent.parent
    if not (directory / '.claude-plugin' / 'plugin.json').is_file():
        raise SystemExit(f'No plugin at {directory}')

    problems, found = probe(directory, cwd)
    write(review_dir / 'startup.json', {'probe': found['raw'], 'problems': problems,
                                        'tools_expected': list(tool_names()), 'at': now()})
    if problems:
        finish(review_dir, 'blocked', 'the start-up probe failed: ' + '; '.join(problems),
               Accounting(per_agent_tokens, per_run_tokens), 0.0, None)
        print(json.dumps({'status': 'blocked', 'reason': problems}, indent=2))
        return 2

    accounting = Accounting(per_agent_tokens, per_run_tokens)
    started = time.monotonic()
    prompt = prompt_for(source, root, reviewers, arm, literature_cutoff, tracks)
    seen, stop, code = launch(prompt, directory, started + wall_timeout,
                              accounting.on_event, cwd)
    wall = time.monotonic() - started
    result = accounting.last_result()
    blocked = None
    if accounting.rate_limits:
        blocked = accounting.rate_limits[-1]['reason']
    elif stop and stop != 'the workflow task finished':
        blocked = stop
    elif not accounting.notified:
        blocked = 'the workflow task never reported finishing'
    elif result and result.get('is_error'):
        blocked = 'the session ended in an error: ' + str(result.get('subtype'))

    status = 'blocked' if blocked else None
    progress = review_dir / 'progress.json'
    if status is None:
        status = read(progress).get('status') if progress.is_file() else 'blocked'
        if status not in ('complete', 'complete-without-findings'):
            blocked = (read(progress).get('error') if progress.is_file()
                       else 'the server wrote no progress file')
            status = status or 'blocked'
    finish(review_dir, status, blocked, accounting, wall, result)
    summary = {
        'status': status,
        'blocked_reason': blocked,
        'run': str(root),
        'review': str(review_dir),
        'arm': arm,
        'reviewers': reviewers,
        'wall_seconds': round(wall, 1),
        'tokens_total': accounting.total_tokens(),
        'tokens_by_agent': {row['label']: row.get('tokens') for row in
                            sorted(accounting.agents.values(), key=lambda r: r['label'])},
        'models': sorted({str(row.get('model')) for row in accounting.agents.values()
                          if row.get('model')}),
        'stream_events': seen,
        'exit_code': code,
    }
    if progress.is_file():
        done = read(progress)
        summary['findings_delivered'] = done.get('findings_delivered')
        summary['quarantined'] = done.get('quarantined_findings_count')
        summary['stages'] = done.get('stages')
    print(json.dumps(summary, indent=2))
    return 0 if status in ('complete', 'complete-without-findings') else 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog='peerreview review',
        description='Review one manuscript with the pra plugin in a headless Claude Code session.')
    parser.add_argument('workspace', help='the manuscript PDF')
    parser.add_argument('output', help='the run folder to write')
    parser.add_argument('--arm', default=DEFAULT_ARM, choices=sorted(ARMS),
                        help='which study arm to run (default: %(default)s)')
    parser.add_argument('--reviewers', type=int, default=3,
                        help='core reviewers, where the arm does not fix it (default: 3)')
    parser.add_argument('--track', action='append', choices=list(SPECIALIST_TRACKS),
                        help='for the draft-check arm: a specialist track, repeatable')
    parser.add_argument('--literature-cutoff', default=None,
                        help='no literature after this date counts against the paper')
    parser.add_argument('--per-agent-tokens', type=int, default=PER_AGENT_TOKENS)
    parser.add_argument('--per-run-tokens', type=int, default=PER_RUN_TOKENS)
    parser.add_argument('--timeout', type=int, default=WALL_TIMEOUT,
                        help='wall-clock seconds before the run is blocked')
    parser.add_argument('--plugin-dir', default=None, help='override the plugin folder')
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'review':
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    return review(args.workspace, args.output, reviewers=args.reviewers, arm=args.arm,
                  literature_cutoff=args.literature_cutoff, tracks=args.track,
                  per_agent_tokens=args.per_agent_tokens, per_run_tokens=args.per_run_tokens,
                  wall_timeout=args.timeout, directory=args.plugin_dir)


def handles(argv):
    """True when ``peerreview review`` was asked for the plugin route, by ``--arm``."""
    if not argv or argv[0] != 'review':
        return False
    return any(flag == '--arm' or flag.startswith('--arm=') for flag in argv)


if __name__ == '__main__':
    raise SystemExit(main())
