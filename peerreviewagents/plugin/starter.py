"""Start one plugin review in a headless Claude Code session and account for it.

The server owns the review; this starter owns the launch and the accounting.
It does five things a workflow script cannot:

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
* **Renders the reports last.** The recorder writes ``REVIEW.md`` from inside
  the session, before any of that accounting exists on disk. The starter writes
  the reports again afterwards, so the header can state real totals.

A rate limit, a breached budget or a missing stage leaves the run ``blocked``,
which is an outcome and not an error to paper over.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..engine.storage import digest, read, write
from ..names import COMPLETE_STATUSES, FINDING_SCHEMA_VERSION
from .server import (
    ARMS,
    DEFAULT_ARM,
    MANUSCRIPT_DIRECTORY,
    RUN_DIRECTORY,
    SPECIALIST_TRACKS,
    TOOLS,
    WORKFLOW_VERSION,
)

SERVER = 'peerreview'
PLUGIN = 'pra'
TOOL_PREFIX = f'mcp__plugin_{PLUGIN}_{SERVER}__'
HARNESS_TOOLS = ('Workflow', 'Skill', 'TodoWrite')
PER_AGENT_TOKENS = 500_000
PER_RUN_TOKENS = 6_000_000
WALL_TIMEOUT = 5400
PROBE_TIMEOUT = 180
RATE_LIMIT_CEILING = 0.99
MISSING_CLAUDE = ('claude is not installed or not on this PATH, so no session can be started: '
                  'install Claude Code and check that `claude --version` runs in this shell')
VERSIONS = {}


def tool_names():
    return tuple(TOOL_PREFIX + tool.__name__ for tool in TOOLS)


def allow_list():
    return ','.join((*HARNESS_TOOLS, *tool_names()))


def plugin_dir():
    """The plugin that ships inside this package.

    It is package data, not a checkout: an installed wheel has no ``plugins/``
    folder beside it, and a review that cannot find its own plugin is the one
    failure the starter must never have.
    """
    return Path(__file__).resolve().parent / PLUGIN


def claude_binary():
    """Claude Code, resolved the way a shell resolves it.

    ``shutil.which`` honours PATHEXT, so it finds the ``claude.cmd`` shim a
    Windows install puts on PATH, where naming the bare word would not.
    """
    return shutil.which('claude')


def claude_version(binary=None):
    """What ``claude --version`` prints, for provenance. ``None`` if it will not say."""
    binary = binary or claude_binary()
    if not binary:
        return None
    if binary not in VERSIONS:
        VERSIONS[binary] = None
        try:
            done = subprocess.run([binary, '--version'], capture_output=True, text=True,
                                  timeout=60, encoding='utf-8', errors='replace')
            first = ((done.stdout or '') + (done.stderr or '')).strip().splitlines()
            VERSIONS[binary] = first[0][:120] if first else None
        except (OSError, subprocess.SubprocessError):
            VERSIONS[binary] = None
    return VERSIONS[binary]


def child_environment(binary):
    """The environment a session runs in: subscription login, own interpreter."""
    environment = {**os.environ, 'PEERREVIEW_PLUGIN_RUN': '1',
                   # The plugin's `.mcp.json` launches its MCP server through
                   # this, so the run is served by the very interpreter that
                   # started it even when that virtualenv is not on PATH.
                   'PEERREVIEW_MCP_COMMAND': sys.executable}
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
        environment.pop(key, None)  # subscription login only, never a key
    version = claude_version(binary)
    if version:
        # Provenance records the harness that ran the review. Nothing else sets
        # this, so the starter is the one place that can know it.
        environment['PEERREVIEW_CLAUDE_VERSION'] = version
    return environment


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def pump(stream, lines):
    """Read one pipe to its end, in its own thread."""
    try:
        for line in stream:
            lines.put(line)
    except (OSError, ValueError):  # the pipe closed under us; EOF either way
        pass
    finally:
        lines.put(None)


def drain(stream, into):
    """Keep a pipe empty and remember its tail.

    Claude Code writes plugin and MCP diagnostics to stderr. Left unread, that
    pipe fills and the session stops writing events, which looked like a hung
    review; the tail is what gets shown if the run then fails.
    """
    try:
        for line in stream:
            into.append(line)
    except (OSError, ValueError):
        pass


def launch(prompt, directory, timeout_at, on_event, cwd):
    """Run one headless session, handing every stream event to ``on_event``.

    ``on_event`` returns a reason to stop, or ``None`` to carry on; a reason
    terminates the session. Returns ``(events_seen, stop_reason, exit_code)``.

    Both pipes are read by threads rather than polled: ``select`` cannot wait on
    a pipe on Windows, and stderr has to be drained for the whole run and not
    only at the end.
    """
    binary = claude_binary()
    if not binary:
        return 0, MISSING_CLAUDE, 127
    command = [
        binary, '-p', prompt,
        '--plugin-dir', str(directory),
        '--permission-mode', 'dontAsk',
        '--allowed-tools', allow_list(),
        '--output-format', 'stream-json',
        '--verbose',
        '--forward-subagent-text',
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, bufsize=1, cwd=str(cwd),
                                   env=child_environment(binary),
                                   encoding='utf-8', errors='replace')
    except (OSError, ValueError) as exc:
        # A claude that will not start is a blocked run, not a traceback.
        return 0, 'claude could not be started: ' + str(exc), 127
    lines = queue.Queue()
    errors = collections.deque(maxlen=400)
    readers = (threading.Thread(target=pump, args=(process.stdout, lines), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, errors), daemon=True))
    for reader in readers:
        reader.start()
    seen, stop = 0, None
    try:
        while True:
            if time.monotonic() > timeout_at:
                stop = 'the session passed its wall-clock limit'
                break
            try:
                line = lines.get(timeout=5)
            except queue.Empty:
                continue
            if line is None:  # the session closed its stream
                break
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
                process.wait(timeout=20)
        for reader in readers:
            reader.join(timeout=5)
        rest = ''.join(errors)
        if rest.strip():
            sys.stderr.write(rest[-4000:])
    return seen, stop, process.returncode


def probe(directory, cwd):
    """Assert the plugin and the server are really there before spending a run."""
    found = {'plugin': False, 'server': False, 'tools': set(), 'agents': set(), 'raw': None}
    expected = set(tool_names())
    if not claude_binary():
        # Without a harness there is nothing to say about the plugin, and
        # "the plugin was not loaded" would send the fix to the wrong place.
        return [MISSING_CLAUDE], found

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
    _, stop, _ = launch('Reply with the single word ready.', directory, deadline, on_event, cwd)
    problems = []
    if stop and stop != 'probe complete':
        # The session never reached its init event, so say what actually went
        # wrong instead of reporting every tool as absent.
        return [stop], found
    if not found['plugin']:
        problems.append(f'the {PLUGIN} plugin was not loaded')
    if not found['server']:
        problems.append(f'the {SERVER} MCP server did not connect')
    missing = expected - found['tools']
    if missing:
        problems.append(f'{len(missing)} server tool(s) absent: ' + ', '.join(sorted(missing)[:6]))
    return problems, found


ALLOWED_LIMIT_STATUS = ('allowed', 'allowed_warning', 'warning', 'ok')


def rate_limit_reason(event):
    """A rate limit, read from the shape the real stream sends.

    The event is ``{'type': 'rate_limit_event', 'rate_limit_info': {...}}``, and
    a healthy one says ``status: 'allowed'`` — a subscription with no overage
    credit reports ``overageStatus: 'rejected'`` on every call it serves
    perfectly well, so only ``status`` itself blocks a run. Any nested object
    that reports a numeric ``utilization`` is also honoured, without this code
    asserting what those windows are called.
    """
    if not isinstance(event, dict):
        return None
    info = event.get('rate_limit_info')
    if not isinstance(info, dict):
        info = event.get('rate_limit')
    if not isinstance(info, dict):
        return None
    status = str(info.get('status') or '').strip().lower()
    if status and status not in ALLOWED_LIMIT_STATUS:
        detail = info.get('overageDisabledReason') or info.get('reason') or 'no reason given'
        return f'the subscription rate limit reported status {status} ({detail})'
    for name, window in info.items():
        levels = [(name, window)]
        if isinstance(window, dict):
            levels += list(window.items())
        for label, level in levels:
            used = level.get('utilization') if isinstance(level, dict) else None
            if isinstance(used, (int, float)) and not isinstance(used, bool):
                if used >= RATE_LIMIT_CEILING:
                    return f'the {label} rate-limit window is at {used:.0%}'
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
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get('label') or row.get('agentType')
            if not key:
                # The stream emits a placeholder row for an agent it has not
                # named yet. It carries no usage, so inventing an `agent-N`
                # label for it only puts an empty agent in the run record.
                continue
            key = str(key)
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
        with (folder / 'usage.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'label': key, 'attempt': attempt,
                                     'observed_tokens': row.get('tokens') or 0,
                                     'tool_calls': row.get('toolCalls') or 0,
                                     'model': row.get('model'),
                                     'state': row.get('state')}) + '\n')


def write_identity(review, source, arm, reviewers, cutoff=None, tracks=None):
    """Say which paper and which plan this folder was, when the server never did.

    The server writes ``protocol.json`` and ``manifest.json`` from the imported
    manuscript, which is the full record and is left alone wherever it exists.
    A run that never reached ``ingest`` — a start-up probe that refused to
    launch, a session that died before the workflow called the first tool — has
    neither, and a folder that cannot name its own paper or arm is not a result
    anybody can file. These two are the thin version: the source hash, the
    plan, the versions, and nothing the run did not actually learn.
    """
    source = Path(source)
    try:
        source_sha256 = digest(source.read_bytes()) if source.is_file() else None
    except OSError:
        source_sha256 = None
    plan = ARMS.get(arm) or {}
    wrote = []
    if not (review / 'protocol.json').is_file():
        write(review / 'protocol.json', {
            'provider': 'claude-code',
            'mode': arm,
            'arm': arm,
            'reviewers': reviewers,
            'paper_id': None,
            'literature_cutoff': cutoff,
            'version': WORKFLOW_VERSION,
            'finding_schema_version': FINDING_SCHEMA_VERSION,
            'pdf': str(source),
            'agents': {},
            'stage': 'startup',
            'written_by': 'starter: the run stopped before the server recorded its protocol',
            'plan': {'arm': arm, 'reviewers': reviewers,
                     'specialists': list(tracks or ()) if plan.get('specialists') else [],
                     'editor': plan.get('editor'), 'revision': plan.get('revision'),
                     'summary': plan.get('summary')},
        })
        wrote.append('protocol.json')
    if not (review / 'manifest.json').is_file():
        write(review / 'manifest.json', {
            'paper_id': None,
            'source_sha256': source_sha256,
            'pdf': str(source),
            'code': {},
            'workflow_version': WORKFLOW_VERSION,
            'finding_schema_version': FINDING_SCHEMA_VERSION,
            'claude_code_version': claude_version() or os.environ.get(
                'PEERREVIEW_CLAUDE_VERSION'),
            'written_by': 'starter: the run stopped before the manuscript was imported',
        })
        wrote.append('manifest.json')
    return wrote


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
        'claude_code_version': claude_version() or os.environ.get('PEERREVIEW_CLAUDE_VERSION'),
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
        if current.get('status') not in COMPLETE_STATUSES:
            write(progress, {**current, 'status': 'blocked',
                             'error': reason or current.get('error'), 'updated': now()})


def rerender(review, workspace=None):
    """Render REVIEW.md again, now that the run's own accounting is on disk.

    The recorder writes the reports from inside the session, which is before
    this starter has written ``runtime.json`` or a single usage receipt: that
    copy can only report no tokens and no models. This is the first moment the
    whole accounting exists, so the reports are written once more over the top.
    """
    if not (review / 'protocol.json').is_file():
        return None
    try:
        from ..engine.run_report import write_reports
        paths = write_reports(review, workspace, review)
        return {key: str(value) for key, value in paths.items()}
    except Exception as exc:  # noqa: BLE001 - a report must never lose a finished review
        return {'error': str(exc)}


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
    """Run one review and return its summary.

    The summary is a dict, not an exit code: ``status``, ``blocked_reason``,
    the run and review folders, the arm, the wall time, the tokens in total and
    per agent, the models each slot ran on, and the counts the server recorded.
    ``stage`` says how far the run got — ``startup`` means the probe refused to
    launch a session and no usage was spent. :func:`exit_code` turns it into a
    process status, and nothing here prints: the command prints, a library
    caller reads the dict and the run folder.
    """
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
        reason = 'the start-up probe failed: ' + '; '.join(problems)
        write_identity(review_dir, source, arm, reviewers, literature_cutoff, tracks)
        finish(review_dir, 'blocked', reason,
               Accounting(per_agent_tokens, per_run_tokens), 0.0, None)
        return {'status': 'blocked', 'blocked_reason': reason, 'stage': 'startup',
                'probe_problems': problems, 'run': str(root), 'review': str(review_dir),
                'arm': arm, 'reviewers': reviewers}

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
        recorded = read(progress) if progress.is_file() else {}
        status = str(recorded.get('status') or '') or None
        if status not in COMPLETE_STATUSES:
            # A workflow that ends without write_review or mark_blocked leaves
            # the server's own status at `running`, which would read as a review
            # still in flight long after the session is gone.
            blocked = recorded.get('error') or (
                f'the workflow ended without writing the review or marking the run blocked;'
                f' the server last recorded {status}' if status
                else 'the server wrote no progress file')
            status = 'blocked'
    # A session that ended before the workflow called `ingest` left no protocol
    # and no manifest, so the folder would not name its own paper or arm.
    write_identity(review_dir, source, arm, reviewers, literature_cutoff, tracks)
    finish(review_dir, status, blocked, accounting, wall, result)
    report = rerender(review_dir, root / MANUSCRIPT_DIRECTORY)
    summary = {
        'status': status,
        'blocked_reason': blocked,
        'stage': 'session',
        'run': str(root),
        'review': str(review_dir),
        'report': report,
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
    return summary


def exit_code(summary):
    """What the command exits with: 0 reviewed, 2 never started, 1 anything else.

    A start-up probe that refuses to launch is worth its own code, because no
    subscription usage was spent and the fix is on this machine.
    """
    if summary.get('status') in COMPLETE_STATUSES:
        return 0
    return 2 if summary.get('stage') == 'startup' else 1


def build_parser(prog='peerreview review'):
    parser = argparse.ArgumentParser(
        prog=prog,
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


def review_main(argv=None, prog='peerreview review'):
    """``peerreview review`` as a function: parse, run, print the summary, return a code.

    This is the supported entry point for an application that wraps the
    command. It takes the argument list the user typed — with or without a
    leading ``review`` — prints the run summary as JSON on stdout and returns
    the process exit code rather than raising it, so a caller decides what to
    do with a blocked run. ``prog`` is the program name its ``--help`` and its
    errors use, so a wrapper named something else says its own name.

    Nothing else here needs to be reached for: the summary dict comes from
    :func:`review` and the code from :func:`exit_code`.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'review':
        argv = argv[1:]
    args = build_parser(prog=prog).parse_args(argv)
    summary = review(args.workspace, args.output, reviewers=args.reviewers, arm=args.arm,
                     literature_cutoff=args.literature_cutoff, tracks=args.track,
                     per_agent_tokens=args.per_agent_tokens, per_run_tokens=args.per_run_tokens,
                     wall_timeout=args.timeout, directory=args.plugin_dir)
    print(json.dumps(summary, indent=2))
    return exit_code(summary)


def main(argv=None):
    """The console script's entry point. :func:`review_main` is the public name."""
    return review_main(argv)


if __name__ == '__main__':
    raise SystemExit(review_main())
