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


def jsonl(path, on_problem=None):
    """Every JSON object in a line-delimited file, or nothing if it is absent.

    Strict by default: the server's own budget reads these receipts, and a
    receipt it cannot parse must stop a call rather than be counted as zero.
    Pass ``on_problem`` — a callable taking one sentence — to read as far as the
    file can be read instead, which is what a reader of a killed run wants: the
    bad line is skipped, the undecodable bytes are replaced, and each is
    reported.
    """
    path = Path(path)
    if not path.exists(): return []
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        if on_problem is None: raise
        text = raw.decode('utf-8', errors='replace')
        on_problem(f'{path.name} is not valid UTF-8; the bytes that are not were replaced')
    rows, skipped = [], 0
    for line in text.splitlines():
        if not line.strip(): continue
        try:
            row = json.loads(line)
        except ValueError:
            if on_problem is None: raise
            skipped += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            skipped += 1
    if skipped and on_problem is not None:
        on_problem(f'{skipped} unreadable line(s) in {path.name} were skipped, so the usage '
                   'counted here is a lower bound')
    return rows


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


def observed_usage(out, on_problem=None):
    """Account for every attempt, but judge resolution on the surviving ones.

    A retried call leaves a failed receipt behind on purpose. Its tokens still
    count against the budget, and it is still listed, but a superseded attempt
    with unknown usage must not make the whole run unresolvable: the retry that
    replaced it reports its own usage.

    A call that timed out spent an unknown amount, so that attempt is counted in
    ``unknown_attempts`` and described in ``usage_limitation`` rather than
    silently treated as zero. The count is what the budget bounds.
    """
    unread = []

    def note(message):
        unread.append(message)
        on_problem(message)

    reader = note if on_problem is not None else None
    rows = [r for p in sorted(Path(out).rglob('usage.jsonl')) for r in jsonl(p, reader)]
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
    if unread:
        # A receipt that could not be read is spend nobody counted, so the
        # whole reading is unresolved rather than a tidy total.
        value['unknown'] = True
        value['usage_limitation'] = ' '.join(filter(None, [
            value.get('usage_limitation'),
            f'{len(unread)} usage receipt(s) could not be read in full, so observed_tokens is '
            'a lower bound for this run.']))
    return value


def runtime_usage(run, on_problem=None):
    """The starter's own accounting of a run, or ``None`` before it was written.

    ``runtime.json`` is written once the headless session has ended, so it is
    the only record that can carry a wall time, a rate limit and the model each
    agent really ran as. A report rendered from inside the session has no such
    file yet, which is why the starter renders the reports again afterwards.
    """
    path = Path(run) / 'runtime.json'
    if not path.is_file():
        return None
    try:
        value = read(path)
    except (ValueError, UnicodeDecodeError) as exc:
        # The starter writes this file last, so a run killed while writing it
        # leaves exactly this, and it is the file naming the reason it stopped.
        if on_problem is None: raise
        on_problem(f'runtime.json could not be read ({exc.__class__.__name__}); it is read as '
                   'absent, so the tokens and the wall time of this run are lost')
        return None
    return value if isinstance(value, dict) else None


#: The fields a stream row carries when it says anything at all about an agent.
#: A row with a label and none of these is the placeholder the harness emits for
#: an agent it has announced and not yet run.
SPENT_FIELDS = ('tokens', 'toolCalls', 'state', 'model', 'agentType', 'durationMs')


def _said_something(row):
    return any(row.get(field) is not None for field in SPENT_FIELDS)


def _role_of(row):
    """The agent file a stream row names, without its plugin prefix.

    The harness labels a task by what the task is — ``ingest``, ``write
    review`` — while the protocol records the slot that did it, ``recorder``.
    The ``agentType`` is the one field that holds the same word as the slot.
    """
    kind = row.get('agentType')
    return str(kind).split(':')[-1].strip() if kind else None


def _total(rows, field):
    """The sum over the rows that report ``field``, or ``None`` if none does."""
    values = [row[field] for row in rows if isinstance(row.get(field), (int, float))
              and not isinstance(row.get(field), bool)]
    return sum(values) if values else None


def _merge(rows):
    """Several stream rows for one slot, as one row's worth of accounting.

    A slot can appear in the stream more than once because one agent ran more
    than one task: the recorder ingests the manuscript and later writes the
    review. Both are that slot's spend, so they are added rather than listed as
    two agents, and ``labels`` keeps the task names the stream used.
    """
    state = [row.get('state') for row in rows if row.get('state')]
    return {
        'model': next((row['model'] for row in rows if row.get('model')), None),
        'attempt': max((int(row.get('attempt') or 1) for row in rows), default=1),
        'session_calls': _total(rows, 'toolCalls'),
        'tokens': _total(rows, 'tokens'),
        # Every task done is the slot done; anything else is the state that is
        # not, because that is the one worth seeing.
        'state': next((value for value in state if value != 'done'),
                      state[0] if state else None),
        'labels': [str(row.get('label')) for row in rows if row.get('label')],
    }


def agent_rows(run, protocol=None, runtime=None, on_problem=None):
    """One row per agent slot: role, track, model, tool calls and tokens.

    The protocol says which slots the arm asked for and what each was allowed to
    do; ``runtime.json`` says what each really spent. A slot that never reported
    is still listed, because a missing agent is the thing worth seeing.

    ``model`` and ``assigned_model`` are two different facts since 0.9.0, when
    the slots stopped all running one model, and neither is ever shown as the
    other. ``assigned_model`` is the model the run asked that slot to run, from
    the protocol: the plan. ``model`` is what was observed to run — the
    stream's word for it, then the agent's own report of it — and nothing else.
    A slot that reported no model has ``model`` of ``None``, which means *not
    recorded*, never *the model it was assigned*: filling the gap from the plan
    would make a substituted model impossible to see, which is the one thing
    these two fields exist to show.

    Two call counts are kept apart on purpose. ``tool_calls`` is what the
    server itself served that slot, which is the number the ledger bounds;
    ``session_calls`` is every tool call the session saw the agent make, which
    is larger and is Claude Code's count rather than the server's.

    **One agent is one row.** The stream names a task and the protocol names a
    slot, and for the recorder those are different words: ``ingest`` and ``write
    review`` in ``runtime.json``, ``recorder`` in ``protocol.json``. A stream
    row whose ``agentType`` is the slot's own agent is that slot's, so its
    tokens and calls are added to it and ``labels`` says which tasks they were,
    rather than the same agent appearing three times with the spend under the
    two names the protocol does not use. A stream row that carries nothing but a
    label is the harness's placeholder for an agent it has not run, and it is
    left out: it is not a slot, and listing it puts an agent that spent nothing
    and did nothing in the run's own record.
    """
    if runtime is None:
        runtime = runtime_usage(run, on_problem) or {}
    spent = {}
    rows_recorded = runtime.get('agents')
    for row in rows_recorded if isinstance(rows_recorded, list) else []:
        if isinstance(row, dict) and row.get('label') and _said_something(row):
            spent[str(row['label']).split('#')[0]] = row
    rows = []
    planned = (protocol or {}).get('agents')
    for slot, record in sorted((planned if isinstance(planned, dict) else {}).items()):
        record = record if isinstance(record, dict) else {}
        if slot in spent:
            observed = _merge([spent.pop(slot)])
        else:
            # No row under the slot's own name: the tasks this slot's agent ran.
            theirs = [label for label, row in spent.items() if _role_of(row) == slot]
            observed = _merge([spent.pop(label) for label in theirs]) if theirs else {}
        rows.append({'slot': slot, 'role': record.get('role'), 'track': record.get('track'),
                     'model': observed.get('model') or record.get('model'),
                     'assigned_model': record.get('assigned_model'),
                     'attempt': observed.get('attempt') or record.get('attempt') or 1,
                     'tool_calls': record.get('tool_calls'),
                     'session_calls': observed.get('session_calls'),
                     'tokens': observed.get('tokens'),
                     'state': observed.get('state'),
                     'labels': observed.get('labels') or []})
    # An agent the stream reported but the protocol never recorded is a real
    # discrepancy, so it is shown rather than dropped to make the table tidy.
    for slot, observed in sorted(spent.items()):
        merged = _merge([observed])
        rows.append({'slot': slot, 'role': None, 'track': None, 'model': merged.get('model'),
                     'assigned_model': None,
                     'attempt': merged.get('attempt') or 1, 'tool_calls': None,
                     'session_calls': merged.get('session_calls'),
                     'tokens': merged.get('tokens'), 'state': merged.get('state'),
                     'labels': merged.get('labels') or []})
    return rows
