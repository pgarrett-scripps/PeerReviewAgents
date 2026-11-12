"""What a run actually spent, read back from the files the run itself wrote.

Two readings live here because a run records itself twice. Each agent writes a
usage receipt as it goes — ``usage/<slot>/usage.jsonl`` beside a
``*.started.json`` marker — and the starter writes one ``runtime.json`` at the
end with the stream's own per-agent accounting. :func:`observed_usage` reads the
receipts, which is the accounting that bounds a budget mid-run.
:func:`runtime_usage` reads ``runtime.json``, which is the accounting a report
can name a model and a wall time from.

Neither function makes a model call or trusts an agent's account of itself.
"""
import json
from pathlib import Path

from .storage import read


def jsonl(path):
    """Every JSON object in a line-delimited file, or nothing if it is absent."""
    path = Path(path)
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip()]


def attempt_key(record):
    """Identity of one attempt. A retry of the same request is a separate row."""
    identity = record.get('request_sha256')
    return f"{identity}#{record.get('attempt') or 1}" if identity else None


def effective_rows(rows, pending=None):
    """The usage rows that still bind the call guards.

    A retry replaces the attempt before it, so only the latest attempt of each
    request answers for that request. The superseded row keeps its receipt and
    its tokens, and no longer blocks any later call in the same directory.
    ``pending`` names the ``(request, attempt)`` about to launch, whose own
    predecessors are superseded even though that attempt has no row yet.
    """
    latest = {}
    for row in rows:
        identity = row.get('request_sha256')
        latest[identity] = max(latest.get(identity, 0), row.get('attempt') or 1)
    if pending:
        identity, attempt = pending
        latest[identity] = max(latest.get(identity, 0), attempt)
    return [row for row in rows if (row.get('attempt') or 1) >= latest.get(row.get('request_sha256'), 0)]


def unaccounted_attempts(rows):
    """Superseded attempts whose spend never became a token count.

    A call that timed out or crashed may have spent usage the transport never
    saw. The retry that replaced it answers for the request, so these rows are
    reported rather than enforced, and a budget may allow a bounded number.
    """
    accounted = {attempt_key(row) for row in effective_rows(rows)}
    return [row for row in rows if attempt_key(row) not in accounted
            and (row.get('usage_unknown') or row.get('observed_tokens') is None)]


def observed_usage(out):
    """Account for every attempt, but judge resolution on the surviving ones.

    A retried call leaves a failed receipt behind on purpose. Its tokens still
    count against the budget, and it is still listed, but a superseded attempt
    with unknown usage must not make the whole run unresolvable: the retry that
    replaced it reports its own usage.

    A call that timed out spent an unknown amount, so that attempt is counted in
    ``unknown_attempts`` and described in ``usage_limitation`` rather than
    silently treated as zero. The count is what the budget bounds.
    """
    rows = [r for p in sorted(Path(out).rglob('usage.jsonl')) for r in jsonl(p)]
    attempts = list(Path(out).rglob('*.started.json'))
    effective = effective_rows(rows)
    unaccounted = unaccounted_attempts(rows)
    value = {'calls': len(attempts), 'receipts': len(rows),
             'observed_tokens': sum(r.get('observed_tokens') or 0 for r in rows),
             'retried_attempts': len(rows) - len(effective),
             'unknown_attempts': len(unaccounted),
             'unknown': len(attempts) != len(rows) or any(
                 r.get('usage_unknown') or r.get('observed_tokens') is None for r in effective)}
    if unaccounted:
        value['usage_limitation'] = (f'{len(unaccounted)} superseded attempt(s) spent subscription usage that no '
                                     'token count covers, so observed_tokens is a lower bound for this run.')
        value['unaccounted_attempts'] = [{'label': r.get('label'), 'request_sha256': r.get('request_sha256'),
                                          'attempt': r.get('attempt') or 1, 'error': r.get('error'),
                                          'elapsed_seconds': r.get('elapsed_seconds')} for r in unaccounted]
    return value


def runtime_usage(run):
    """The starter's own accounting of a run, or ``None`` before it was written.

    ``runtime.json`` is written once the headless session has ended, so it is
    the only record that can carry a wall time, a rate limit and the model each
    agent really ran as. A report rendered from inside the session has no such
    file yet, which is why the starter renders the reports again afterwards.
    """
    path = Path(run) / 'runtime.json'
    if not path.is_file():
        return None
    value = read(path)
    return value if isinstance(value, dict) else None


def agent_rows(run, protocol=None):
    """One row per agent slot: role, track, model, tool calls and tokens.

    The protocol says which slots the arm asked for and what each was allowed to
    do; ``runtime.json`` says what each really spent. A slot that never reported
    is still listed, because a missing agent is the thing worth seeing.

    Two call counts are kept apart on purpose. ``tool_calls`` is what the
    server itself served that slot, which is the number the ledger bounds;
    ``session_calls`` is every tool call the session saw the agent make, which
    is larger and is Claude Code's count rather than the server's.
    """
    runtime = runtime_usage(run) or {}
    spent = {}
    for row in runtime.get('agents') or []:
        if isinstance(row, dict) and row.get('label'):
            spent[str(row['label']).split('#')[0]] = row
    rows = []
    for slot, record in sorted(((protocol or {}).get('agents') or {}).items()):
        record = record if isinstance(record, dict) else {}
        observed = spent.pop(slot, {})
        rows.append({'slot': slot, 'role': record.get('role'), 'track': record.get('track'),
                     'model': observed.get('model') or record.get('model'),
                     'attempt': observed.get('attempt') or record.get('attempt') or 1,
                     'tool_calls': record.get('tool_calls'),
                     'session_calls': observed.get('toolCalls'),
                     'tokens': observed.get('tokens'),
                     'state': observed.get('state')})
    # An agent the stream reported but the protocol never recorded is a real
    # discrepancy, so it is shown rather than dropped to make the table tidy.
    for slot, observed in sorted(spent.items()):
        rows.append({'slot': slot, 'role': None, 'track': None, 'model': observed.get('model'),
                     'attempt': observed.get('attempt') or 1, 'tool_calls': None,
                     'session_calls': observed.get('toolCalls'),
                     'tokens': observed.get('tokens'), 'state': observed.get('state')})
    return rows
