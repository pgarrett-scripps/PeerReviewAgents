"""The citation ledger as the server runs it: four stages, one run, no model.

Every lookup here is answered from a table in this file and nothing opens a
socket. Nothing starts Claude Code or Codex either: the rules this file pins
are the server's, not a prompt's — who may resolve the references, who may read
the table, who may write a verdict into it, and the one that matters most,

    a worker's verdict is not a finding.

The ingestion fixture has a reference list and no in-text markers, so
:func:`citing_workspace` imports it once, marks three sentences, and writes the
result back as the run's manuscript — the same workspace an attach reads.
"""

from __future__ import annotations

import datetime
import functools
import inspect
import io
import json
import re
import shutil
import threading
import time
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anyio
import mcp.types as mcp_types
import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from peerreviewagents import names
from peerreviewagents.cli.main import report_main
from peerreviewagents.engine import citation_ledger as cl
from peerreviewagents.engine import paper_base, paper_text
from peerreviewagents.engine.ingest import import_paper
from peerreviewagents.engine.run_report import report_blocks
from peerreviewagents.plugin import server as srv
from peerreviewagents.runs import read_run

FIXTURE = Path(__file__).parent / 'fixtures' / 'single-column.pdf'
#: The abstract every resolved reference comes back with. It is what the
#: workers judge the citing sentences against, and the passage they quote has
#: to be inside it.
ABSTRACT = ('We placed widgets at a fixed position on a level bench and read the width of '
            'each one. Fixed placement gave repeatable readings across days. The green '
            'widgets drifted between the first and the final reading.')
#: Which line of the fixture cites which reference, once the markers are added.
#: The introduction, the results and the discussion, each a paragraph of its
#: own on one line of ``paper.md``.
MARKERS = {11: '[1]', 19: '[2]', 23: '[3]'}
CODES = {}
TOKENS = {}


def resolving(*args, **kwargs):
    """``resolve_citations`` as a sync test calls it.

    The tool is a coroutine, because the waiting half of the stage runs in a
    worker thread so that it cannot hold the server's event loop. ``anyio.run``
    here is what FastMCP does around it in the real server.
    """
    return anyio.run(functools.partial(srv.resolve_citations, *args, **kwargs))


@pytest.fixture(autouse=True)
def fresh_state():
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    srv.STATE['opener'] = None
    CODES.clear()
    TOKENS.clear()
    yield
    srv.STATE['run'] = None
    srv.STATE['opener'] = None


class Index:
    """Crossref and Europe PMC, answered offline from this class.

    A bibliographic search is answered with the very title it asked for, so
    every reference in the fixture resolves and carries :data:`ABSTRACT` as its
    source text. Anything else is a 404, and a URL holding a fragment in
    ``fail`` raises :class:`OSError` — the transport failure the ledger has to
    read as "could not check" rather than as a paper that does not exist.
    """

    def __init__(self, abstract=ABSTRACT, fail=(), year_shift=0, missing=()):
        self.abstract = abstract
        self.fail = list(fail)
        self.year_shift = year_shift
        self.missing = list(missing)
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        assert 'mailto' not in url.lower(), 'a lookup must not carry an address'
        for fragment in self.fail:
            if fragment in url:
                raise OSError('temporary failure in name resolution')
        if url.startswith(cl.CROSSREF_SEARCH):
            return io.BytesIO(json.dumps(self.search(url)).encode('utf-8'))
        if url.startswith(cl.EUROPEPMC_SEARCH):
            # Europe PMC answers, and holds nothing: an index that answered
            # with no hits is the only thing that can earn 'does not exist'.
            return io.BytesIO(json.dumps({'hitCount': 0,
                                          'resultList': {'result': []}}).encode('utf-8'))
        raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)

    def search(self, url):
        """One Crossref search answer: the queried title, as a real record."""
        query = parse_qs(urlsplit(url).query)
        title = (query.get('query.bibliographic') or [''])[0]
        author = (query.get('query.author') or ['Ada Example'])[0]
        if any(one in title for one in self.missing):
            # An index that answered and held nothing like it. This is the only
            # shape that earns 'does not exist'.
            return {'message': {'items': []}}
        # The printed year is the middle of the window the ledger filtered on.
        window = (query.get('filter') or [''])[0]
        year = 2020
        for part in window.split(','):
            if part.startswith('from-pub-date:'):
                year = int(part.split(':', 1)[1][:4]) + 1
        item = {'DOI': '10.5555/' + ''.join(ch for ch in title.casefold() if ch.isalnum())[:24],
                'title': [title],
                'author': [{'given': author.split()[0], 'family': author.split()[-1]}],
                'issued': {'date-parts': [[year + self.year_shift]]},
                'container-title': ['Synthetic Journal'],
                'type': 'journal-article',
                'abstract': self.abstract}
        return {'message': {'items': [item]}}


class FakeClock:
    """A clock that only moves when something sleeps on it.

    Every retry gap in these tests is five minutes long and none of them takes
    any time. There is no real sleep anywhere in this suite on purpose: a test
    that waits out a real retry gap is a five-minute test, which is a test
    nobody runs.

    It starts at the real present rather than at zero, because the rows it
    decides the due times of carry real ``checked_at`` stamps.
    """

    def __init__(self, start=None):
        self.now = float(start if start is not None else time.time())
        self.slept: list[float] = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def cite_in(text, marker):
    """``text`` with ``marker`` added to the end of its first sentence."""
    first, rest = text.split('. ', 1)
    return first + ' ' + marker + '. ' + rest


def citing_workspace(tmp_path):
    """The fixture manuscript, with three of its sentences citing its references.

    ``ingest`` keeps a manuscript it finds in the run folder, so writing the
    marked-up paper there is how a test hands the server a draft that cites
    something. The marker goes into ``paper.md``, which is the manuscript the
    reviewers read, the ledger reads and the workers quote; ``map.json`` is
    re-fingerprinted over the lines as they now read, and the manifest
    re-stamped over every file touched, because the manuscript is immutable
    once a run starts and every tool call checks that.
    """
    root = tmp_path / 'run'
    workspace = root / names.MANUSCRIPT_DIRECTORY
    import_paper(str(FIXTURE), workspace)
    name = paper_base.MAIN_DOCUMENT
    path = workspace / name
    lines = path.read_text(encoding='utf-8').split('\n')
    for number, marker in MARKERS.items():
        lines[number - 1] = cite_in(lines[number - 1], marker)
    path.write_text('\n'.join(lines), encoding='utf-8', newline='\n')
    rows = srv.read(workspace / 'map.json')
    for row in rows:
        start, end = row['lines']
        if row['document'] == name and start in MARKERS:
            row['fingerprint'] = paper_text.fingerprint('\n'.join(lines[start - 1:end]))
    srv.write(workspace / 'map.json', rows)
    manifest = srv.read(workspace / 'manifest.json')
    for touched in (name, 'map.json'):
        manifest['files'][touched] = srv.digest((workspace / touched).read_bytes())
    srv.write(workspace / 'manifest.json', manifest)
    return root


def open_run(tmp_path, **kwargs):
    """A run over the citing manuscript, with every claim code in hand."""
    root = citing_workspace(tmp_path)
    opened = srv.ingest(str(FIXTURE), str(root), **kwargs)
    assert 'request_rejected' not in opened, opened
    CODES.update({row['slot']: row['claim_code'] for row in opened.get('claim_codes', [])})
    CODES['recorder_key'] = opened.get('recorder_key', '')
    return opened


def read_whole_paper(token):
    """Read every part of the manuscript, as the brief tells every agent to.

    The server refuses a finding from a slot it has not served the whole paper
    to, and reading spends nothing from the allowance.
    """
    run = srv.current()
    if 'read_paper' in run.agent_for(token).allowed:
        for part in range(1, srv.paper_view.served(run.base) + 1):
            assert isinstance(srv.read_paper(token, part=part), str)
    return token


def token_for(slot):
    """The token for one slot, claimed once with the code minted for it."""
    if slot in TOKENS:
        return TOKENS[slot]
    taken = srv.claim_agent(slot, CODES.get(slot, ''))
    assert 'agent_token' in taken, taken
    CODES.pop(slot, None)
    TOKENS[slot] = read_whole_paper(taken['agent_token'])
    return TOKENS[slot]


def resolved_run(tmp_path, index=None, **kwargs):
    """A run whose references are all looked up, and the index that answered."""
    opened = open_run(tmp_path, **kwargs)
    srv.STATE['opener'] = index = index if index is not None else Index()
    answer = resolving(CODES['recorder_key'], limit=0)
    assert 'request_rejected' not in answer, answer
    return opened, answer, index


def judged(tmp_path, verdict='supports', index=None):
    """A run resolved and judged by one worker, with the rows it was handed."""
    resolved_run(tmp_path, index=index)
    token = token_for('cite-1')
    claimed = srv.claim_citation_batch(token, size=10, model='claude-haiku-5-5')
    assert claimed['rows'], claimed
    stored = []
    for row in claimed['rows']:
        passage = row['source_text'].split('\n', 1)[1].split('. ')[0] + '.'
        stored.append(srv.submit_citation_verdict(token, row['row_id'], verdict,
                                                  passage=passage))
        assert stored[-1].get('stored'), stored[-1]
    return token, claimed, stored


# -- stage 1: the table exists before anything is looked up -----------------


def test_the_ledger_is_filled_at_ingest_without_a_model_or_a_lookup(tmp_path):
    """One row per reference and one per in-text use, written by ingest itself."""
    opened = open_run(tmp_path)
    run = srv.current()
    assert run.citation_fill['references'] == 3
    assert run.citation_fill['uses'] == 3
    assert (run.review / cl.LEDGER_PATH).is_file()
    counts = opened['citations']
    assert (counts['references'], counts['uses']) == (3, 3)
    # Stage 1 reads the manuscript. Nothing was resolved and nothing was judged.
    assert counts['lookups'] == 0
    assert counts['unresolved'] == 3
    assert counts['uses_unjudged'] == 3
    assert counts['complete'] is False
    status = srv.run_status(CODES['recorder_key'])
    assert status['citations']['references'] == 3
    # What the lane can cover is the paper's two numbers and the server's own
    # allowance, which is why ingest hands all three to the workflow: a stage
    # sized from a constant stops part-way through a long reference list.
    assert opened['citation_worker_allowance'] == srv.CALL_BUDGET['citation-worker']
    # The workers are planned, and they are not stages of the review.
    assert opened['citation_workers'] == ['cite-1', 'cite-2', 'cite-3', 'cite-4']
    assert not set(opened['citation_workers']) & set(opened['slots'])


def test_an_arm_without_the_citations_specialist_keeps_no_ledger(tmp_path):
    """The ledger belongs to the citations lane. No lane, no table and no workers.

    Two arms are compared on what they found, so an arm that runs no citations
    specialist must not quietly acquire citation counts the arm beside it has.
    """
    opened = open_run(tmp_path, tracks=['stats', 'figures-tables'])
    run = srv.current()
    assert opened['citation_workers'] == []
    assert run.citations_enabled() is False
    assert opened['citations'] is None
    # No lane, so there is no judging allowance to size a stage from either:
    # a number here would let the workflow plan work nobody is doing.
    assert opened['citation_worker_allowance'] is None
    assert not (run.review / cl.LEDGER_PATH).exists()
    assert 'no citation ledger' in opened['citations_next']
    refused = resolving(CODES['recorder_key'])
    assert 'keeps no citation ledger' in refused['request_rejected']


# -- stage 2: the server resolves every reference, and only the server ------


def test_the_references_are_resolved_by_the_recorder_and_no_model(tmp_path):
    """Every reference, not a sample: resolve until ``remaining`` is zero."""
    open_run(tmp_path)
    index = srv.STATE['opener'] = Index()
    assert resolving('not-the-run-key')['request_rejected']
    first = resolving(CODES['recorder_key'], limit=2)
    assert (first['resolved_now'], first['remaining']) == (2, 1)
    assert 'resolve_citations again' in first['next']
    assert first['lookups_now'] > 0
    rest = resolving(CODES['recorder_key'], limit=0)
    assert (rest['resolved_now'], rest['remaining']) == (1, 0)
    assert 'may claim their batches' in rest['next']
    counts = rest['counts']
    assert counts['resolved'] == 3
    assert counts['not_found'] == 0 and counts['could_not_check'] == 0
    assert counts['with_source_text'] == 3
    assert counts['lookups'] == rest['lookups_total'] == len(index.urls)
    # A third call has nothing left to do and makes no further request.
    again = resolving(CODES['recorder_key'])
    assert again['resolved_now'] == 0
    assert again['lookups_now'] == 0
    assert rest['ledger'] == cl.LEDGER_PATH
    # Nothing in a lookup says who is running it.
    assert all('mailto' not in url for url in index.urls)


def test_a_reference_neither_index_lists_is_not_located_and_still_a_row_to_re_check(tmp_path):
    """The whole lane, end to end: not located, re-checked, published as a finding.

    Two indexes answered and neither held the work. That is ``not located`` and
    not ``no``: this reference prints no DOI, and most of the literature —
    books, theses, standards, anything in a journal neither index covers — is
    in neither. The ledger hands the row to the specialist with that caveat
    attached, and the specialist decides what it means.
    """
    missing = 'Reading a measurement scale'
    _, answer, _ = resolved_run(tmp_path, index=Index(missing=(missing,)))
    counts = answer['counts']
    assert (counts['not_located'], counts['not_found'], counts['could_not_check']) == (1, 0, 0)

    token = token_for('spec-citations')
    rows = srv.citation_rows(token, kind='problems')
    assert len(rows['references']) == 1
    row = rows['references'][0]
    assert row['printed']['title'] == missing
    assert row['exists'] == 'not located'
    assert row['method'] == 'title-search'
    assert 'lists a work with this title' in row['note']
    assert 'not evidence that the work does not exist' in row['note']
    assert 'weak evidence' in row['problem']

    run = srv.current()
    sentence = next(one['sentence'] for one in srv.citation_rows(token, kind='unverified')['uses']
                    if one['reference_id'] == row['reference_id'])
    stored = srv.submit_finding(token, title='Reference 2 is in neither index',
                                quote=sentence,
                                comment='Neither Crossref nor Europe PMC lists a work with '
                                        'this title, and the reference prints no DOI, so I '
                                        'could not verify it either way.',
                                severity='minor')
    assert 'finding_uid' in stored, stored
    assert srv.submit_report(token, 'One reference could not be located.',
                             model='claude-sonnet-5-5',
                             report_markdown='# citations audit')['closed']
    finish(run)
    reading = read_run(run.review)
    assert [finding['title'] for finding in reading['findings']] == [
        'Reference 2 is in neither index']
    assert reading['citations']['counts']['not_located'] == 1
    assert reading['citations']['counts']['not_found'] == 0
    assert any('much of the literature is in neither' in line.lower()
               for line in reading['citations']['problems'])


def test_a_reference_whose_year_disagrees_is_resolved_and_mismatched(tmp_path):
    """Both facts on the row: the work exists, and the paper printed it wrong."""
    _, answer, _ = resolved_run(tmp_path, index=Index(year_shift=3))
    counts = answer['counts']
    assert counts['resolved'] == 3
    assert counts['mismatched'] == 3
    assert counts['not_found'] == 0
    rows = srv.citation_rows(token_for('spec-citations'), kind='problems')
    assert len(rows['references']) == 3
    assert all(row['mismatched'] == ['year'] for row in rows['references'])
    assert all('year' in row['problem'] for row in rows['references'])


def test_a_year_one_apart_is_the_same_publication_year_and_not_a_finding(tmp_path):
    """Online-first and print years differ by one constantly. That is not a mismatch.

    Every journal that publishes ahead of print has two years for one paper,
    and a reference list prints whichever its style asks for. Reporting that
    gap put a metadata mismatch on a correct reference in half the list, which
    is a finding about the comparison and not about the manuscript.
    """
    _, answer, _ = resolved_run(tmp_path, index=Index(year_shift=1))
    counts = answer['counts']
    assert (counts['resolved'], counts['mismatched']) == (3, 0)
    rows = srv.citation_rows(token_for('spec-citations'), kind='problems')
    assert rows['references'] == []


def test_a_lookup_that_never_answers_is_could_not_check(tmp_path):
    """A rate limit is not evidence about a paper. 'could not check', never 'no'."""
    open_run(tmp_path)
    srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
    answer = resolving(CODES['recorder_key'], limit=0)
    counts = answer['counts']
    assert counts['could_not_check'] == 3
    assert counts['not_found'] == 0
    problems = cl.problem_rows(srv.current().citations)
    assert len(problems['references']) == 3
    assert all('could not be checked' in row['problem'] for row in problems['references'])
    assert any('says nothing about whether they exist' in line
               for line in cl.problems(counts))


def test_the_recorder_is_told_to_stop_while_the_owed_retries_are_waiting(tmp_path):
    """Three rows rate-limited is three rows owed a lookup, and the loop sees it.

    ``remaining`` used to count what one call did not reach, so a pass in which
    every row failed answered ``remaining: 0`` and the recorder stopped: the
    loop in the workflow is the only thing that calls this tool, so the two
    further attempts the ledger promises never happened in a real run. Now the
    rows are still owed, and the tool says to wait rather than spend a turn.
    """
    open_run(tmp_path)
    srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
    answer = resolving(CODES['recorder_key'], limit=0)
    assert answer['remaining'] == 3
    assert 0 < answer['retry_after'] <= answer['retry_wait']
    assert answer['retry_wait'] == cl.RETRY_WAIT_SECONDS >= 60
    assert 'waiting out the retry wait' in answer['next']
    assert 'stop' in answer['next']
    assert answer['counts']['stages']['resolve']['done'] is False
    # And a second call now does nothing, which is what the recorder is told.
    again = resolving(CODES['recorder_key'], limit=0)
    assert again['resolved_now'] == 0 and again['remaining'] == 3

    # A run can end inside that wait: the retry gap is five minutes and a short
    # review has less than that left. The report has to say the rows were still
    # owed an attempt, because "could not be checked" on its own reads as the
    # indexes having been asked and failed.
    lines = cl.problems(again['counts'])
    owed = [line for line in lines if 'still owed an attempt' in line]
    assert len(owed) == 1, lines
    assert '3 row(s)' in owed[0]
    # No PEERREVIEW_RUN_DEADLINE here, so the stage had no run clock to measure
    # the gap against and says that, rather than implying it chose not to wait.
    assert 'not told how much of the run was left' in owed[0]
    assert 'counted as unchecked rather than answered' in owed[0]
    assert all(';' not in line and 'do not exist' not in line for line in lines)


def test_a_retry_gap_that_fits_inside_the_run_is_waited_out_and_the_rows_are_checked(tmp_path):
    """The whole point of the retry: a rate limit passes, and the stage is still there.

    Three rows rate-limited, a retry gap of five minutes, and an hour of run
    left. The stage waits the gap out and takes the rows up itself, so the
    reference list is checked instead of being reported as unchecked by a run
    that had the time and spent it ending.
    """
    open_run(tmp_path)
    index = Index(fail=('crossref.org', 'ebi.ac.uk'))
    srv.STATE['opener'] = index
    clock = FakeClock()

    def wait_the_rate_limit_out(seconds):
        clock.sleep(seconds)
        index.fail = []  # which is the state the retry exists for

    run = srv.current()
    patience = cl.Patience(deadline=clock.time() + 3600, clock=clock.time,
                           sleep=wait_the_rate_limit_out,
                           slice_seconds=cl.RETRY_WAIT_SECONDS)
    progress = cl.resolve(run.citations, run.citation_lookups(), limit=0, patience=patience)
    assert progress['remaining'] == 0
    assert progress['counts']['resolved'] == 3
    assert progress['counts']['could_not_check'] == 0
    assert progress['unchecked_reason'] is None
    # About one gap waited, and no real time spent on it.
    assert clock.slept
    assert progress['waited'] == pytest.approx(sum(clock.slept))
    assert cl.RETRY_WAIT_SECONDS <= progress['waited'] <= cl.RETRY_WAIT_SECONDS + 10
    stage = progress['counts']['stages']['resolve']
    assert stage['done'] is True and stage['waited'] == progress['waited']
    assert stage['unchecked_reason'] is None
    assert not [line for line in cl.problems(progress['counts'])
                if 'still owed an attempt' in line]


def test_a_retry_gap_longer_than_the_run_has_left_is_not_waited_out_and_says_so(tmp_path):
    """Two minutes of run and a five-minute gap: report it, do not sleep through it.

    This is the pilot's seven-minute run. Sleeping would hold the tool call
    open until the driver ended the run, and the rows would be unchecked at the
    end of it either way — so the stage comes straight back and the record says
    which rows were left and why.
    """
    open_run(tmp_path)
    srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
    clock = FakeClock()
    run = srv.current()
    patience = cl.Patience(deadline=clock.time() + 120, clock=clock.time, sleep=clock.sleep)
    progress = cl.resolve(run.citations, run.citation_lookups(), limit=0, patience=patience)
    assert progress['remaining'] == 3
    assert clock.slept == [] and progress['waited'] == 0.0
    reason = progress['unchecked_reason']
    # 120 seconds of run less the margin the stage keeps for the pass that
    # would have followed the wait.
    left = 120 - cl.RESOLVE_MARGIN_SECONDS
    assert f'{left} second(s) of the run were left to wait in' in reason, reason
    assert progress['counts']['stages']['resolve']['unchecked_reason'] == reason
    lines = cl.problems(progress['counts'])
    owed = [line for line in lines if 'still owed an attempt' in line]
    assert len(owed) == 1 and reason in owed[0]
    assert 'counted as unchecked rather than answered' in owed[0]
    assert all(';' not in line and 'do not exist' not in line for line in lines)


def test_the_recorder_is_handed_the_runs_clock_and_not_a_guess(tmp_path, monkeypatch, subtests):
    """The tool reads the driver's deadline out of the environment, or waits for nothing.

    This is the wiring the fake-clock tests above cannot see: that the server
    asks the environment the launcher set, that an unusable answer is no
    deadline rather than a wrong one, and that a run with less left than the
    gap comes straight back with the reason instead of holding the call open.
    """
    with subtests.test('an unusable deadline is no deadline'):
        for value in ('', 'soon', '0', '-1'):
            monkeypatch.setenv('PEERREVIEW_RUN_DEADLINE', value)
            assert srv.run_deadline() is None, value
        monkeypatch.delenv('PEERREVIEW_RUN_DEADLINE')
        assert srv.run_deadline() is None

    with subtests.test('a deadline the launcher set is read'):
        monkeypatch.setenv('PEERREVIEW_RUN_DEADLINE', '1700000124')
        assert srv.run_deadline() == 1_700_000_124.0

    with subtests.test('a run with less left than the gap does not wait'):
        open_run(tmp_path)
        srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
        monkeypatch.setenv('PEERREVIEW_RUN_DEADLINE', str(int(time.time()) + 90))
        started = time.monotonic()
        answer = resolving(CODES['recorder_key'], limit=0)
        # Held open for no time at all: the point is that it did not sleep.
        assert time.monotonic() - started < 5
        assert answer['remaining'] == 3 and answer['waited'] == 0.0
        assert 'of the run were left to wait in' in answer['unchecked_reason']
        assert answer['unchecked_reason'] in answer['next']
        assert 'unchecked rather than answered' in answer['next']


def test_the_rest_of_the_session_keeps_working_while_the_resolve_stage_waits(tmp_path,
                                                                            monkeypatch):
    """The wait may not hold the server's event loop. Three agents, one loop.

    FastMCP runs a *sync* tool inline on the event loop that serves every agent
    in the run, so a ``time.sleep`` inside one would stop the reviewers, the
    workers, the editor and the recorder for the length of the gap — and would
    stop the one call that could end the wait, which is the recorder marking the
    run blocked. So the tool is a coroutine and the pass runs in a worker
    thread. This test holds a resolve in its wait and asks the server for three
    things while it is there: another agent's tool call, a heartbeat in
    ``progress.json``, and the wait's own end.
    """
    open_run(tmp_path)
    srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
    run = srv.current()
    spec = token_for('spec-citations')
    clock = FakeClock()
    nap = time.sleep

    class Napping:
        """``time`` as the ledger sees it: a fake clock that yields for real.

        A slice is twenty fake seconds and two real milliseconds. The fake part
        is why a five-minute gap costs nothing; the real part is only there so
        the worker thread lets go of the interpreter, which is the thing being
        measured.
        """

        @staticmethod
        def time():
            return clock.time()

        @staticmethod
        def sleep(seconds):
            clock.sleep(seconds)
            nap(0.002)

    # A sync tool would have been called inline on the loop, so this is the
    # whole fix in one assertion: FastMCP awaits a coroutine function instead.
    assert inspect.iscoroutinefunction(srv.resolve_citations)
    monkeypatch.setattr(cl, 'time', Napping)
    monkeypatch.setattr(cl, 'Patience', functools.partial(cl.Patience, slice_seconds=20))
    monkeypatch.setenv('PEERREVIEW_RUN_DEADLINE', str(int(clock.time()) + 3600))
    progress_file = run.review / 'progress.json'
    run.write_progress()
    before = json.loads(progress_file.read_text(encoding='utf-8'))['updated']
    seen = {}

    async def the_recorder_resolves():
        seen['resolve'] = await srv.resolve_citations(CODES['recorder_key'], limit=0)

    async def drive():
        async with anyio.create_task_group() as group:
            group.start_soon(the_recorder_resolves)
            for _ in range(2000):
                if clock.slept:
                    break
                await anyio.sleep(0.001)
            assert clock.slept, 'the stage never got as far as waiting'
            # The citations specialist, mid-wait, on the same loop.
            seen['rows'] = srv.citation_rows(spec, kind='counts')
            seen['beat'] = json.loads(progress_file.read_text(encoding='utf-8'))
            # And the call that a blocking wait made impossible: it is what
            # `stop` reads, so it ends the wait as well as the run.
            seen['blocked'] = srv.mark_blocked(CODES['recorder_key'], reason='rate limited')

    started = time.monotonic()
    anyio.run(drive)
    # The whole exchange, including two agents and an interrupted five-minute
    # gap, inside a second or two of real time.
    assert time.monotonic() - started < 10

    # A tool call from another agent was served while the stage was waiting.
    assert 'request_rejected' not in seen['rows'], seen['rows']
    assert seen['rows']['counts']['references'] == 3
    # Progress moved while the stage waited, which is what the driver watches.
    assert seen['beat']['updated'] != before
    # And the wait ended with the run rather than with the gap.
    assert 'request_rejected' not in seen['blocked'], seen['blocked']
    answer = seen['resolve']
    assert answer['remaining'] == 3
    assert 0 < answer['waited'] < cl.RETRY_WAIT_SECONDS
    assert 'the run stopped while the stage was waiting' in answer['unchecked_reason']
    assert run.status == 'blocked'


def test_two_resolve_calls_at_once_do_not_both_fetch_the_same_rows(tmp_path, monkeypatch):
    """One pass at a time, now that the loop is free enough to start a second.

    While the tool was sync, a second call could not begin until the first
    returned. It is a coroutine with its pass in a worker thread now, so a
    recorder that issues the call twice — models do — or calls again while one
    is still waiting would get two passes over the same owed rows: every row
    fetched twice, two of a row's three attempts spent on one answer, and
    ``Lookups._record``'s read-modify-write of ``requests.json`` racing itself,
    which loses receipts and miscounts the allowance. The second call does
    nothing at all instead, and the recorder is told to wait for the first.
    """
    open_run(tmp_path)
    srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
    run = srv.current()
    clock = FakeClock()
    nap = time.sleep

    class Napping:
        """Twenty fake seconds and two real milliseconds per slice, as above."""

        @staticmethod
        def time():
            return clock.time()

        @staticmethod
        def sleep(seconds):
            clock.sleep(seconds)
            nap(0.002)

    def attempts():
        return {row['row_id']: (row.get('resolution') or {}).get('attempts')
                for row in run.citations.rows('reference')}

    monkeypatch.setattr(cl, 'time', Napping)
    monkeypatch.setattr(cl, 'Patience', functools.partial(cl.Patience, slice_seconds=20))
    monkeypatch.setenv('PEERREVIEW_RUN_DEADLINE', str(int(clock.time()) + 3600))
    seen = {}

    async def the_recorder_resolves():
        seen['first'] = await srv.resolve_citations(CODES['recorder_key'], limit=0)

    async def drive():
        async with anyio.create_task_group() as group:
            group.start_soon(the_recorder_resolves)
            for _ in range(2000):
                if clock.slept:
                    break
                await anyio.sleep(0.001)
            assert clock.slept, 'the stage never got as far as waiting'
            assert run.resolving is True
            spent, owed = run.citation_lookups().count(), attempts()
            started = time.monotonic()
            # The same call the first task is still inside, on the same loop.
            seen['second'] = await srv.resolve_citations(CODES['recorder_key'], limit=0)
            seen['elapsed'] = time.monotonic() - started
            seen['spent'] = (spent, run.citation_lookups().count())
            seen['owed'] = (owed, attempts())

    anyio.run(drive)

    second = seen['second']
    # Not one request, and not one attempt spent, which is the point: an
    # attempt spent twice for one answer is a row written off two attempts
    # early.
    assert seen['spent'][0] == seen['spent'][1], 'the second call made lookups of its own'
    assert seen['owed'][0] == seen['owed'][1], 'the second call spent the rows attempts'
    assert any(seen['owed'][0].values()), 'nothing had been attempted, so nothing was proved'
    # It answered instead of queueing behind the wait, and it did no work: no
    # pass ran, so there is nothing of a pass in the answer.
    assert seen['elapsed'] < 1
    assert not {'resolved_now', 'remaining', 'counts'} & set(second)
    assert 'already running' in second['request_rejected']
    assert second['already_resolving'] is True
    # The first call is the one that answers, and the stage is open again after
    # it, so the recorder that waited is not locked out.
    assert seen['first']['waited'] > 0
    assert run.resolving is False
    again = resolving(CODES['recorder_key'], limit=0)
    assert 'request_rejected' not in again, again
    assert 'already_resolving' not in again

    # And a pass that raises leaves the stage open rather than shut for the rest
    # of the run, which is what the `finally` is for.
    def broken(*args, **kwargs):
        raise ValueError('the ledger is unreadable')

    monkeypatch.setattr(cl, 'resolve', broken)
    assert 'could not be resolved' in resolving(CODES['recorder_key'], limit=0)['request_rejected']
    assert run.resolving is False


def test_a_cancelled_resolve_call_does_not_take_the_session_down(tmp_path):
    """A client that cancels a pass mid-lookup used to end the whole session.

    ``notifications/cancelled`` completes the request's responder and cancels
    its scope, but the pass is in a worker thread that
    ``anyio.to_thread.run_sync`` does not abandon, so the cancellation was held
    until the pass ended and then had nowhere to land: the handler returned
    normally, the lowlevel server answered a request the cancellation had
    already answered, and the assertion behind ``RequestResponder.respond``
    ("Request already responded to") came out of the task group that runs the
    session. One cancelled lookup therefore ended the session for every agent
    in the run and left the folder at ``running``.

    Driven through a real in-memory MCP session, because the fault was in what
    the server does with the handler's return value rather than in the tool.
    The pass keeps what it wrote, the stage is released, and the next call is
    served.
    """
    open_run(tmp_path)
    run = srv.current()
    index = Index()
    looking_up, release = threading.Event(), threading.Event()

    def gated(request, timeout=None):
        """The index, with the first lookup held open in the worker thread."""
        if not looking_up.is_set():
            looking_up.set()
            assert release.wait(30), 'the test never released the lookup'
        return index(request, timeout=timeout)

    srv.STATE['opener'] = gated
    seen = {}

    async def the_recorder_resolves(session):
        try:
            seen['first'] = await session.call_tool(
                'resolve_citations', {'recorder_key': CODES['recorder_key'], 'limit': 0})
        except McpError as exc:
            # What a cancelled request answers: `responder.cancel()` sends the
            # error itself, before the handler has anything to say.
            seen['first'] = exc

    async def drive():
        async with create_connected_server_and_client_session(srv.create_server()) as session:
            async with anyio.create_task_group() as group:
                group.start_soon(the_recorder_resolves, session)
                with anyio.fail_after(30):
                    while not looking_up.is_set():
                        await anyio.sleep(0.005)
                    # The id the client gave the call that is in flight: the
                    # only request it has open, and the one the notification
                    # has to name.
                    await session.send_notification(mcp_types.ClientNotification(
                        mcp_types.CancelledNotification(
                            method='notifications/cancelled',
                            params=mcp_types.CancelledNotificationParams(
                                requestId=session._request_id - 1))))
                    while 'first' not in seen:
                        await anyio.sleep(0.005)
                    # And now the pass runs to its end underneath the request
                    # that is already answered, which is the whole fault.
                    release.set()
                    while run.resolving:
                        await anyio.sleep(0.005)
                    # The session is still there to serve the call after it.
                    seen['second'] = await session.call_tool(
                        'resolve_citations',
                        {'recorder_key': CODES['recorder_key'], 'limit': 0})

    anyio.run(drive)

    assert isinstance(seen['first'], McpError), seen['first']
    assert 'cancelled' in str(seen['first']).lower()
    # The stage is open again and the run was never left mid-pass.
    assert run.resolving is False
    assert run.status == 'running'
    # The pass kept its work: the lookups it made are recorded, and so is the
    # progress written after it.
    assert run.citation_lookups().count() > 0
    standing = json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))
    assert standing['status'] == 'running'
    # And the next call is served by the same session and finishes the table.
    answer = seen['second']
    assert answer.isError is False, answer
    progress = (answer.structuredContent if answer.structuredContent is not None
                else json.loads(answer.content[0].text))
    assert progress['remaining'] == 0
    assert progress['counts']['resolved'] == 3


def test_a_cancelled_call_whose_pass_fails_still_answers_only_once(tmp_path, monkeypatch):
    """The checkpoint was below the rejection a failed pass answers with.

    A pass that raises is answered with a rejection, and that rejection was
    returned from the ``except`` clause — above the cancellation checkpoint. So
    the one call that both was cancelled *and* failed answered a request the
    cancellation had already answered, raised "Request already responded to"
    out of the session's task group, and ended the session for every agent in
    the run exactly as the uncancelled crash did, with the folder at
    ``running``. Every exit path of this call goes below that checkpoint now.
    """
    open_run(tmp_path)
    run = srv.current()
    passing, release = threading.Event(), threading.Event()

    def broken(*args, **kwargs):
        """A pass held open in the worker thread, which then fails."""
        passing.set()
        assert release.wait(30), 'the test never released the pass'
        raise OSError('the ledger went away under the pass')

    monkeypatch.setattr(cl, 'resolve', broken)
    seen = {}

    async def the_recorder_resolves(session):
        try:
            seen['first'] = await session.call_tool(
                'resolve_citations', {'recorder_key': CODES['recorder_key'], 'limit': 0})
        except McpError as exc:
            seen['first'] = exc

    async def drive():
        async with create_connected_server_and_client_session(srv.create_server()) as session:
            async with anyio.create_task_group() as group:
                group.start_soon(the_recorder_resolves, session)
                with anyio.fail_after(30):
                    while not passing.is_set():
                        await anyio.sleep(0.005)
                    await session.send_notification(mcp_types.ClientNotification(
                        mcp_types.CancelledNotification(
                            method='notifications/cancelled',
                            params=mcp_types.CancelledNotificationParams(
                                requestId=session._request_id - 1))))
                    while 'first' not in seen:
                        await anyio.sleep(0.005)
                    # And now the held pass fails, under a request that has
                    # already been answered.
                    release.set()
                    while run.resolving:
                        await anyio.sleep(0.005)
                    seen['second'] = await session.call_tool(
                        'resolve_citations',
                        {'recorder_key': CODES['recorder_key'], 'limit': 0})

    anyio.run(drive)

    assert isinstance(seen['first'], McpError), seen['first']
    assert 'cancelled' in str(seen['first']).lower()
    # A failed pass leaves nothing held and nothing final: the stage is open,
    # the run is still running, and the folder says so.
    assert run.resolving is False
    assert run.status == 'running'
    standing = json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))
    assert standing['status'] == 'running'
    # The session served the call after it, and that call — not cancelled, and
    # failing the same way — is answered with the rejection, once.
    answer = seen['second']
    rejection = (answer.structuredContent if answer.structuredContent is not None
                 else json.loads(answer.content[0].text))
    assert 'could not be resolved' in rejection['request_rejected'], rejection
    assert 'still running' in rejection['request_rejected'], rejection


def test_one_call_waits_only_so_long_and_tells_the_recorder_to_call_again(tmp_path, monkeypatch):
    """The run's deadline bounds the run; it does not bound one tool call.

    Rows fall behind at different moments, so a stage can be handed gap after
    gap and hold one call open for most of a ninety-minute run. The client has
    its own patience — a stdio MCP server gets 30 minutes of silence per call
    in Claude Code 2.1.325, and nothing this process writes to the run folder
    extends it — so the call comes back with its budget spent, and the rows it
    left are rows the next call takes up rather than rows nobody will.
    """
    open_run(tmp_path)
    srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
    clock = FakeClock()
    # Room for one gap and not two, against a day of run: the budget is what
    # bites, and the stage waited once before it did.
    budget = cl.RETRY_WAIT_SECONDS + 30
    monkeypatch.setattr(cl, 'time', clock)
    # The rows are stamped on the same clock the stage sleeps on. Two gaps go by
    # here, so rows stamped on the system clock would come out due the moment
    # the fake one moved, and the second gap — the one the budget refuses —
    # would never be asked for.
    monkeypatch.setattr(cl, 'now', lambda: datetime.datetime.fromtimestamp(
        clock.time(), datetime.timezone.utc).isoformat())
    monkeypatch.setattr(cl, 'Patience', functools.partial(
        cl.Patience, slice_seconds=cl.RETRY_WAIT_SECONDS, budget=budget))
    monkeypatch.setenv('PEERREVIEW_RUN_DEADLINE', str(int(clock.time()) + 86_400))
    answer = resolving(CODES['recorder_key'], limit=0)
    assert answer['remaining'] == 3
    assert answer['budget_spent'] is True
    assert cl.RETRY_WAIT_SECONDS <= answer['waited'] <= budget
    assert sum(clock.slept) == pytest.approx(answer['waited'])
    reason = answer['unchecked_reason']
    assert 'one call may wait' in reason and 'taken up' in reason
    assert reason in answer['next'] and 'Call again' in answer['next']
    # Not the other two cases: the run had the time, and nothing stopped it.
    assert 'of the run were left to wait in' not in reason
    assert 'the run stopped' not in reason


def test_the_stage_waits_only_when_it_knows_the_run_has_the_time(subtests):
    """:class:`Patience` on its own: every state it may and may not wait in."""
    with subtests.test('no deadline is no waiting'):
        clock = FakeClock()
        blind = cl.Patience(clock=clock.time, sleep=clock.sleep)
        assert blind.left() is None
        assert blind.fits(300) is False
        assert blind.wait(300) is False
        assert clock.slept == []
        assert 'not told how much of the run was left' in blind.why(300)
        assert blind.why(0) is None

    with subtests.test('a gap inside the run is waited out'):
        clock = FakeClock()
        roomy = cl.Patience(deadline=clock.time() + 3600, clock=clock.time, sleep=clock.sleep)
        assert roomy.fits(300) is True
        assert roomy.wait(300) is True
        assert roomy.waited == pytest.approx(300)
        assert roomy.interrupted is False

    with subtests.test('a gap the margin does not leave room for'):
        clock = FakeClock()
        tight = cl.Patience(deadline=clock.time() + 300, clock=clock.time, sleep=clock.sleep)
        assert tight.fits(300) is False
        assert tight.wait(300) is False
        assert clock.slept == []

    with subtests.test('a wait ends when the run does, not when the gap does'):
        clock = FakeClock()
        asked = []

        def the_run_is_over():
            asked.append(1)
            return len(asked) > 3

        stopping = cl.Patience(deadline=clock.time() + 3600, clock=clock.time,
                               sleep=clock.sleep, stop=the_run_is_over)
        assert stopping.wait(300) is False
        assert stopping.interrupted is True
        # Seconds, not minutes: the wait is slept in slices so that it can end.
        assert stopping.waited <= 5
        assert 'the run stopped while the stage was waiting' in stopping.why(300)

    with subtests.test('a sleep that does not move the clock is not a wait'):
        clock = FakeClock()
        frozen = cl.Patience(deadline=clock.time() + 3600, clock=clock.time,
                             sleep=lambda seconds: None)
        assert frozen.wait(300) is False
        assert frozen.interrupted is False

    with subtests.test('a day of run left is still not an unbounded call'):
        clock = FakeClock()
        patient = cl.Patience(deadline=clock.time() + 86_400, clock=clock.time,
                              sleep=clock.sleep, slice_seconds=cl.RETRY_WAIT_SECONDS)
        gaps = 0
        while patient.wait(cl.RETRY_WAIT_SECONDS):
            gaps += 1
        assert gaps == int(cl.RESOLVE_WAIT_BUDGET_SECONDS // cl.RETRY_WAIT_SECONDS) == 3
        assert patient.waited == pytest.approx(cl.RESOLVE_WAIT_BUDGET_SECONDS)
        assert patient.spare() == pytest.approx(0)
        assert patient.interrupted is False, 'a spent budget is not an interrupted run'
        assert 'one call may wait' in patient.why(cl.RETRY_WAIT_SECONDS)

    with subtests.test('the stage says it is alive while it waits'):
        clock, beats = FakeClock(), []
        alive = cl.Patience(deadline=clock.time() + 3600, clock=clock.time, sleep=clock.sleep,
                            tick=lambda: beats.append(clock.time()))
        assert alive.wait(cl.RETRY_WAIT_SECONDS) is True
        # One beat a tick interval, not one a slice: the folder is polled.
        assert len(beats) == pytest.approx(cl.RETRY_WAIT_SECONDS / cl.RESOLVE_TICK_SECONDS,
                                           abs=1)
        assert max(b - a for a, b in zip(beats, beats[1:])) <= cl.RESOLVE_TICK_SECONDS

    with subtests.test('a heartbeat that fails is not a failed stage'):
        clock = FakeClock()

        def the_loop_is_gone():
            raise RuntimeError('no running event loop')

        deaf = cl.Patience(deadline=clock.time() + 3600, clock=clock.time, sleep=clock.sleep,
                           tick=the_loop_is_gone)
        assert deaf.wait(cl.RETRY_WAIT_SECONDS) is True
        assert deaf.tick is None, 'and it is not tried again'


def test_a_ledger_that_cannot_be_read_is_reported_and_never_read_as_clean(tmp_path, monkeypatch):
    """The specialist's whole job is the table, so an unreadable one is an answer.

    Every kind of this read goes through ``counts`` first, and a ledger file
    that cannot be read at all raised out of the tool: the specialist was left
    with a traceback where its instrument should have been, and the one thing it
    must not do then is report a clean reference list.
    """
    resolved_run(tmp_path)
    token = token_for('spec-citations')

    def unreadable(_ledger):
        raise OSError('Input/output error')

    monkeypatch.setattr(cl, 'counts', unreadable)
    refused = srv.citation_rows(token, kind='counts')
    assert 'could not be read' in refused['request_rejected']
    assert 'rather than reporting that the citations are clean' in refused['request_rejected']
    assert 'calls_left' in refused


# -- stage 3: the workers fill the table in, and file nothing ---------------


def test_a_worker_claims_its_own_batch_and_says_what_model_it_ran(tmp_path):
    """A worker files no report, so the model it names here is the observation."""
    token, claimed, stored = judged(tmp_path)
    run = srv.current()
    assert claimed['slot'] == 'cite-1'
    assert len(claimed['rows']) == 3
    assert all(row['source_text'].startswith('ABSTRACT') for row in claimed['rows'])
    assert all(row['reference']['exists'] == 'yes' for row in claimed['rows'])
    assert set(claimed['verdicts']) == set(cl.VERDICTS)
    assert stored[-1]['uses_unjudged'] == 0
    assert srv.current().citation_counts()['supported'] == 3

    # The plan and the observation, as two facts and never as one.
    protocol = json.loads((run.review / 'protocol.json').read_text(encoding='utf-8'))
    row = protocol['agents']['cite-1']
    assert row['assigned_model'] == 'claude-haiku-5-5'
    assert row['model'] == 'claude-haiku-5-5'
    assert row['role'] == 'citation-worker'

    # A second worker is handed nothing: the first worker's claim is recorded.
    other = srv.claim_citation_batch(token_for('cite-2'))
    assert other['rows'] == []
    assert 'done' in other['next']


def test_a_worker_verdict_alone_never_becomes_a_finding(tmp_path):
    """The point of the whole lane: stage 3 fills a table, stage 4 decides.

    A worker is given two tools and neither of them writes a review. Its
    harshest verdict leaves the published run with no finding in it, and shows
    up as a row the citations specialist has to re-check for itself.
    """
    token, _, _ = judged(tmp_path, verdict='does not support')
    run = srv.current()
    agent = run.agent_for(token)
    assert set(agent.allowed) == {'claim_citation_batch', 'submit_citation_verdict'}
    assert 'submit_finding' not in agent.allowed
    refused = srv.submit_finding(token, title='The source says otherwise',
                                quote='unused', comment='...', severity='major')
    assert 'Only a reviewer or a specialist submits findings' in refused['request_rejected']

    assert run.citation_counts()['not_supported'] == 3
    assert run.findings == []

    # What the specialist is handed instead: the rows, with the passage.
    rows = srv.citation_rows(token_for('spec-citations'), kind='problems')
    assert len(rows['uses']) == 3
    assert all('does not support' in row['problem'] for row in rows['uses'])
    assert rows['counts']['not_supported'] == 3

    finish(run)
    reading = read_run(run.review)
    assert reading['findings'] == []
    assert reading['citations']['counts']['not_supported'] == 3


def test_a_worker_out_of_allowance_hands_its_unjudged_rows_back(tmp_path):
    """A worker that is finished must not keep the rows it never judged.

    Its claim expires on its own, which covers a worker that dies. It does not
    cover one that simply ran out of tool calls: that worker is alive, its name
    is still on the rows, and until the timeout nobody else can take them — so
    the next worker is handed nothing and the table reports uses as unjudged
    with no worker left to judge them.
    """
    resolved_run(tmp_path)
    token = token_for('cite-1')
    claimed = srv.claim_citation_batch(token, size=10, model='claude-haiku-5-5')
    assert len(claimed['rows']) == 3
    run = srv.current()
    agent = run.agent_for(token)
    agent.calls = agent.call_budget  # what spending the allowance looks like

    spent = srv.claim_citation_batch(token)
    assert spent['budget_exhausted'] is True
    assert spent['rows_released'] == 3

    # Another worker is handed them at once, without waiting out a timeout.
    second = srv.claim_citation_batch(token_for('cite-2'), size=10, model='claude-haiku-5-5')
    assert {row['row_id'] for row in second['rows']} == {row['row_id']
                                                         for row in claimed['rows']}
    assert srv.current().citation_counts()['uses_unjudged'] == 3
    # And the second worker's verdict is accepted, because the row is its own.
    passage = second['rows'][0]['source_text'].split('\n', 1)[1].split('. ')[0] + '.'
    stored = srv.submit_citation_verdict(token_for('cite-2'), second['rows'][0]['row_id'],
                                         'supports', passage=passage)
    assert stored.get('stored') is True


def test_a_worker_is_told_the_rows_are_quoted_material_not_instructions(tmp_path):
    """Prompt injection through the manuscript, or through a fetched abstract.

    A citing sentence is the author's text and an abstract is a stranger's;
    both arrive in the row a worker judges, and either can contain a sentence
    addressed to the worker. The batch says so where the worker will read it,
    so an instruction in the material is part of what is being checked.
    """
    resolved_run(tmp_path)
    claimed = srv.claim_citation_batch(token_for('cite-1'), size=10)
    assert claimed['untrusted_text'] == cl.UNTRUSTED_NOTE
    assert 'not instructions to you' in claimed['untrusted_text']
    rows = srv.citation_rows(token_for('spec-citations'), kind='unverified')
    assert rows['untrusted_text'] == cl.UNTRUSTED_NOTE


def test_the_lookup_allowance_is_the_paper_s_length_not_a_constant(tmp_path):
    """A 300-reference review needs more requests than a four-reference letter.

    A fixed allowance half-checks the long list and reports the counts as
    though the whole list had been checked, which is the one thing this table
    exists not to do.
    """
    resolved_run(tmp_path)
    run = srv.current()
    references = len(run.references)
    assert run.citation_lookups().limit == cl.lookup_budget(references)
    assert cl.lookup_budget(4) == cl.MIN_LOOKUPS, 'a short list still gets room to retry'
    assert cl.lookup_budget(300) == 300 * cl.LOOKUPS_PER_REFERENCE
    assert cl.lookup_budget(10_000) == cl.MAX_LOOKUPS
    # And the receipts say what was actually spent, against that allowance.
    receipts = cl.Lookups(run.citations.cache).receipts()
    assert receipts['limit'] == cl.lookup_budget(references)
    assert 0 < len(receipts['requests']) <= receipts['limit']


# -- stage 4: the specialist reads the table, and nobody else does ----------


def test_the_ledger_is_read_by_the_citations_specialist_alone(tmp_path):
    """One lane owns the instrument: a core reviewer cannot read the table."""
    resolved_run(tmp_path)
    core = srv.citation_rows(token_for('core-1'))
    assert 'may not call citation_rows' in core['request_rejected']

    token = token_for('spec-citations')
    totals = srv.citation_rows(token, kind='counts')
    assert totals['counts']['resolved'] == 3
    assert 'reference' in totals['summary']
    assert totals['unfinished']  # nothing is judged yet, and it says so
    unverified = srv.citation_rows(token, kind='unverified')
    assert len(unverified['uses']) == 3
    one = unverified['uses'][0]['row_id']
    detail = srv.citation_rows(token, kind='row', row_id=one)
    assert detail['row']['row_id'] == one
    assert detail['source_text'].startswith('ABSTRACT')
    assert detail['reference']['exists'] == 'yes'
    assert srv.citation_rows(token, kind='row', row_id='no-such-row')['request_rejected']
    assert srv.citation_rows(token, kind='everything')['request_rejected']


def test_the_clean_verdicts_are_readable_too_so_nobody_waves_a_list_through(tmp_path):
    """A worker that answers "supports" for everything looks like a good paper.

    The problem list is empty either way, so the specialist needs the other
    half of the table: the rows a worker read as supported, with the passage it
    quoted, to spot-check before the review says the citations check out.
    """
    judged(tmp_path, verdict='supports')
    token = token_for('spec-citations')
    assert srv.citation_rows(token, kind='problems')['uses'] == []
    supported = srv.citation_rows(token, kind='supported')
    assert len(supported['uses']) == 3
    assert all(row['verdict'] == 'supports' and row['passage'] for row in supported['uses'])
    assert 'how many of them you checked' in supported['instruction']
    assert supported['untrusted_text'] == cl.UNTRUSTED_NOTE
    assert supported['counts']['supported'] == 3
    # The row behind one of them, in full, is what re-checking it means.
    detail = srv.citation_rows(token, kind='row', row_id=supported['uses'][0]['row_id'])
    assert detail['row']['passage'] in detail['source_text']


# -- what the run reports, and what it costs --------------------------------


def close_every_stage(run, model=None):
    """Every stage reported and the editor closed: the run, up to the write.

    Apart from :func:`finish`, this is what a test that has something to do
    between the last stage and ``write_review`` calls — holding a resolve pass
    open across the write, for one.
    """
    for slot in run.required:
        agent = run.agents.get(slot)
        if slot == 'editor' or (agent is not None and agent.report):
            continue
        assert srv.submit_report(token_for(slot), 'Nothing further.', none_found=True,
                                 model=model or run.models.get(slot)).get('closed')
    if 'editor' in run.required:
        editor = token_for('editor')
        pooled = run.pooled()
        if pooled:
            # Every finding belongs to exactly one issue, so one issue holding
            # all of them is the smallest close this helper can make.
            stored = srv.submit_issues(editor, [{
                'name': 'The reference list does not hold up',
                'severity': 'major', 'category': 'literature-and-citations', 'rank': 1,
                'statement': 'The citation checks found rows nobody could confirm, and the '
                             'claims resting on them are unsupported as written.',
                'members': [row['finding_uid'] for row in pooled]}])
            assert stored.get('stored') == 1, stored
        accepted = srv.submit_editor(
            editor, summary='The reference list is the only weakness anyone confirmed.',
            significance='Modest either way.',
            strength_of_evidence='Thin, and not contradicted.',
            verdict_reasoning='No row in the citation table survived a second look.')
        assert accepted['accepted'] is True, accepted
        assert srv.submit_report(editor, 'Nothing further.',
                                 model=model or run.models.get('editor')).get('closed')


def finish(run, model=None):
    """Close every stage and publish: nobody confirmed anything, which is a run.

    ``model`` is what every slot reports running; ``None`` means each slot
    reports the model it was assigned, which is the run that ran its own arm
    and the only one whose ``publishable`` is about anything else.

    The citation workers are deliberately not in this loop. They are not stages
    of the review and none of them has to report for the review to be written —
    which is the whole reason they can be fanned out over a long reference list.
    """
    close_every_stage(run, model)
    written = srv.write_review(token_for('recorder'))
    assert written['status'] in names.COMPLETE_STATUSES, written
    return written


def test_the_run_files_and_the_report_say_what_the_citations_cost(tmp_path):
    """Counts, the ledger path and the per-stage lookups, in both readers."""
    token, _, _ = judged(tmp_path)
    run = srv.current()
    worker_calls = run.agent_for(token).calls
    finish(run)

    reading = read_run(run.review)
    citations = reading['citations']
    assert citations['enabled'] is True
    assert citations['complete'] is True
    assert citations['problems'] == []
    assert citations['ledger'] == str(run.review / cl.LEDGER_PATH)
    assert citations['counts']['supported'] == 3
    assert 'reference' in citations['summary']

    manifest = json.loads((run.review / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['citation_ledger'] == cl.LEDGER_PATH
    assert manifest['citations']['uses_judged'] == 3

    # Per stage: stage 1 cost no lookup, stage 2 is counted and recorded, and
    # stage 3 is tool calls on a worker that is allowed no network at all.
    stages = citations['counts']['stages']
    assert stages['fill']['references'] == 3
    assert stages['resolve']['done'] is True
    assert stages['resolve']['lookups'] == citations['counts']['lookups'] > 0
    assert stages['judge']['verdicts'] == 3
    assert worker_calls == 4  # one claim, three verdicts
    assert srv.EXTERNAL_BUDGET['citation-worker'] == 0
    progress = json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))
    assert progress['tool_calls']['cite-1'] == worker_calls

    blocks = report_blocks(run.review)
    headings = [block[1] for block in blocks if block[0] == 'heading2']
    assert 'Citations' in headings
    fields = [block for block in blocks if block[0] == 'fields']
    rendered = {label: value for block in fields for label, value in block[1]}
    assert str(rendered['References']) == '3'
    assert str(rendered['Supported']) == '3'
    # The published name, not the path. This row printed the ledger's absolute
    # location into REVIEW.md and REVIEW.txt, so the two documents the authors
    # are sent named the machine the review ran on.
    assert rendered['Ledger'] == names.RUN_DIRECTORY + '/' + cl.LEDGER_PATH


def test_a_worker_that_ran_another_model_family_is_reported_and_the_review_stands(tmp_path):
    """A substituted worker is named in `problems`, and withholds no review.

    Claude Code substitutes a model it may not run rather than refusing, and a
    reviewer, specialist or editor that happened to is a run nobody ran: its
    report would be attributed to a model that never wrote it, so it is not
    published. A citation worker is not that. It files no report and no
    finding, and every row of its table that becomes a finding is re-read by
    the citations specialist first — so the swap is reported by slot, loudly,
    and the editor's own review still publishes.
    """
    resolved_run(tmp_path)
    token = token_for('cite-1')
    claimed = srv.claim_citation_batch(token, size=10, model='claude-sonnet-5-5')
    assert claimed['rows'], claimed
    for row in claimed['rows']:
        passage = row['source_text'].split('\n', 1)[1].split('. ')[0] + '.'
        assert srv.submit_citation_verdict(token, row['row_id'], 'supports',
                                           passage=passage).get('stored')
    run = srv.current()
    finish(run)

    reading = read_run(run.review)
    assert reading['publishable'] is True, reading['not_publishable_reasons']
    assert all('cite-1' not in reason for reason in reading['not_publishable_reasons'])
    named = [line for line in reading['problems'] if line.startswith('cite-1 ')]
    assert len(named) == 1, reading['problems']
    assert 'claude-haiku-5-5' in named[0] and 'claude-sonnet-5-5' in named[0]
    assert 'different model family' in named[0]
    # Both facts on the row, and neither shown as the other.
    row = next(one for one in reading['agents'] if one['slot'] == 'cite-1')
    assert row['assigned_model'] == 'claude-haiku-5-5'
    assert row['model'] == 'claude-sonnet-5-5'


def test_a_planned_citations_lane_with_no_table_is_not_read_as_no_lane(tmp_path):
    """Two runs keep no ledger, and they do not mean the same thing.

    An arm without the citations lane keeps no table on purpose and has nothing
    to report. An arm that planned the lane and left no table behind checked
    not one citation, and reporting both as silence hid the second inside the
    first.
    """
    open_run(tmp_path)
    run = srv.current()
    finish(run)
    (run.review / cl.LEDGER_PATH).unlink()
    reading = read_run(run.review)
    assert reading['citations']['enabled'] is True
    assert reading['citations']['ledger'] is None and reading['citations']['counts'] == {}
    assert any('planned a citations lane and wrote no citation ledger' in line
               for line in reading['citations']['problems'])
    assert reading['citations']['problems'] == [line for line in reading['problems']
                                                if 'citations lane' in line]
    assert all('citation' not in reason for reason in reading['not_publishable_reasons'])

    # The arm that never ran the lane says nothing at all, which is why the
    # line above is worth printing. One server process serves one run, so the
    # second arm starts where the first one left off.
    srv.STATE['run'] = None
    TOKENS.clear()
    other = open_run(tmp_path / 'b', tracks=['stats', 'figures-tables'])
    assert other['citations'] is None
    quiet = srv.current()
    finish(quiet)
    silent = read_run(quiet.review)
    assert silent['citations']['enabled'] is False
    assert silent['citations']['problems'] == []
    assert all('citation' not in line for line in silent['problems'])


def folder_digests(root):
    """Every file under one run folder, by run-relative name and digest."""
    return {path.relative_to(root).as_posix(): srv.digest(path.read_bytes())
            for path in sorted(Path(root).rglob('*')) if path.is_file()}


def test_the_ledger_of_a_written_review_takes_no_claim_and_no_verdict(tmp_path):
    """A worker went on filling the table the published counts were taken from.

    The citation workers are not stages of the review: the recorder writes it
    without waiting for them, which is the whole point of fanning them out over
    a long reference list. So a worker still holding a token after
    ``write_review`` could claim another batch and store another verdict, into
    a ledger whose counts are already published in issues.json, progress.json
    and REVIEW.md — and the table no longer said what the review said it said.
    Both tools are guarded, and the folder does not change.
    """
    token, claimed, _ = judged(tmp_path)
    run = srv.current()
    finish(run)
    before = folder_digests(run.root)

    again = srv.claim_citation_batch(token, size=10, model='claude-haiku-5-5')
    stored = srv.submit_citation_verdict(token, claimed['rows'][0]['row_id'], 'contradicts',
                                         passage=ABSTRACT.split('. ')[0] + '.')

    for answer in (again, stored):
        assert 'request_rejected' in answer, answer
        assert 'complete' in answer['request_rejected'], answer
        assert 'stored nothing' in answer['request_rejected'], answer
    assert folder_digests(run.root) == before
    assert read_run(run.review)['status'] in names.COMPLETE_STATUSES


def test_the_references_of_a_written_review_are_not_looked_up_again(tmp_path):
    """``resolve_citations`` was the one way left into a finished folder.

    It had no completion guard at all, so a recorder that called it again over
    a written review — a retry, a workflow stage run twice — was accepted: it
    rewrote citations/stages.json and progress.json, and with rows still owed a
    lookup it appended to ledger.jsonl and left receipts beside it, under a
    REVIEW.md rendered from the table as it had been. The stage is closed with
    the review.
    """
    judged(tmp_path)
    run = srv.current()
    finish(run)
    before = folder_digests(run.root)

    refused = resolving(CODES['recorder_key'], limit=0)

    assert 'request_rejected' in refused, refused
    said = refused['request_rejected']
    assert 'complete' in said and 'ledger is closed' in said, said
    assert refused['status'] in names.COMPLETE_STATUSES
    assert 'remaining' not in refused, 'a pass ran'
    assert folder_digests(run.root) == before, 'the finished folder was written to'
    assert read_run(run.review)['status'] in names.COMPLETE_STATUSES


def test_the_review_is_not_written_from_under_a_resolve_pass(tmp_path, monkeypatch):
    """A pass in flight when the review was written went on writing after it.

    The lookups run in a worker thread, so the guard on the call cannot catch a
    pass that has already started: the recorder wrote the review while one was
    waiting out a retry gap, and the pass then appended to ledger.jsonl, left
    its receipts and rewrote progress.json with counts the REVIEW.md beside it
    was not rendered from — "Resolved 0" in the review, three in progress.json.
    The review is written from the table, so it is not written while the table
    is being filled: ``write_review`` refuses for as long as the pass is open,
    and ``mark_blocked`` still ends a run whose pass will not come back.
    """
    open_run(tmp_path)
    srv.STATE['opener'] = Index(fail=('crossref.org', 'ebi.ac.uk'))
    run = srv.current()
    close_every_stage(run)
    clock = FakeClock()
    nap = time.sleep

    class Napping:
        """Twenty fake seconds and two real milliseconds per slice, as above."""

        @staticmethod
        def time():
            return clock.time()

        @staticmethod
        def sleep(seconds):
            clock.sleep(seconds)
            nap(0.002)

    monkeypatch.setattr(cl, 'time', Napping)
    monkeypatch.setattr(cl, 'Patience', functools.partial(cl.Patience, slice_seconds=20))
    monkeypatch.setenv('PEERREVIEW_RUN_DEADLINE', str(int(clock.time()) + 3600))
    recorder = token_for('recorder')
    seen = {}

    async def the_recorder_resolves():
        seen['pass'] = await srv.resolve_citations(CODES['recorder_key'], limit=0)

    async def drive():
        async with anyio.create_task_group() as group:
            group.start_soon(the_recorder_resolves)
            for _ in range(2000):
                if clock.slept:
                    break
                await anyio.sleep(0.001)
            assert clock.slept, 'the stage never got as far as waiting'
            assert run.resolving is True
            # The write, on the same loop, while the pass holds the stage.
            seen['refused'] = srv.write_review(recorder)
            seen['during'] = folder_digests(run.root)

    anyio.run(drive)

    refused = seen['refused']
    assert 'request_rejected' in refused, refused
    assert 'resolve_citations pass is still looking references up' in refused['request_rejected']
    assert refused['resolving'] is True
    assert 'status' not in refused, 'nothing was published'
    assert not (run.review / 'REVIEW.md').exists(), 'the review was written mid-pass'
    assert run.status == 'running'
    assert 'request_rejected' not in seen['pass'], seen['pass']

    # The pass has answered, and the write is the recorder's again. The review
    # and the table agree, which is what the refusal is for.
    written = srv.write_review(recorder)
    assert written['status'] in names.COMPLETE_STATUSES, written
    totals = cl.counts(cl.Ledger(run.review))
    progress = json.loads((run.review / 'progress.json').read_text(encoding='utf-8'))
    review = (run.review / 'REVIEW.md').read_text(encoding='utf-8')
    assert progress['citations']['resolved'] == totals['resolved']
    assert re.search(r'Resolved[^\n\d]*' + str(totals['resolved']) + r'\b', review), review


def complete_two_track_run(tmp_path):
    """A written review of the arm that runs every stage, and a token for each.

    The citation worker's token and the row it judged come back with it: they
    are the only caller and the only argument the ledger tools take that no
    other stage can hand them.
    """
    worker, claimed, _ = judged(tmp_path)
    run = srv.current()
    finish(run)
    return run, worker, claimed['rows'][0]['row_id']


def test_no_registered_tool_writes_in_a_complete_folder(tmp_path):
    """Every tool the server registers, over a finished two-track run.

    The guards were added one path at a time, and ``resolve_citations`` was
    missed because nothing checked the whole surface: this loops over
    ``srv.TOOLS`` itself, so a tool added later is a failure here until it is
    called with arguments and the folder is still unchanged after it. The arm
    is the full one, because it is the only one with a ledger, citation workers
    and an editor — the stages whose tools the smaller arms never register.
    """
    run, worker, row_id = complete_two_track_run(tmp_path)
    reviewer, editor, recorder = token_for('core-1'), token_for('editor'), token_for('recorder')
    key = CODES['recorder_key']
    quote = run.base.line_text(1)
    calls = {
        'ingest': lambda: srv.ingest(str(FIXTURE), str(run.root)),
        'claim_agent': lambda: srv.claim_agent('core-2', 'whatever-code-it-holds'),
        'reissue_claim_code': lambda: srv.reissue_claim_code(key, 'editor'),
        'read_paper': lambda: srv.read_paper(reviewer),
        'search': lambda: srv.search(reviewer, 'widget'),
        'calculate': lambda: srv.calculate(reviewer, '2 + 2'),
        'save_note': lambda: srv.save_note(reviewer, 'A late thought.', [1]),
        'view_page': lambda: srv.view_page(reviewer, 1),
        'view_supplement': lambda: srv.view_supplement(reviewer, 'S1'),
        'check_citation': lambda: srv.check_citation(reviewer, 'b0006'),
        'search_literature': lambda: srv.search_literature(reviewer, 'widgets', 1),
        'resolve_identifier': lambda: srv.resolve_identifier(reviewer, 'doi', '10.5555/x', 1),
        'lookup_source': lambda: srv.lookup_source(reviewer, 'crossref', '10.5555/x', 1),
        'inspect_results': lambda: srv.inspect_results(reviewer, 1),
        'record_comparison': lambda: srv.record_comparison(reviewer, []),
        'validate_synthesis': lambda: srv.validate_synthesis(reviewer, '{}'),
        'submit_finding': lambda: srv.submit_finding(
            reviewer, title='A late reading', line=1, quote=quote,
            comment='Raised after the review was written.', severity='minor'),
        'submit_report': lambda: srv.submit_report(reviewer, 'A second report.',
                                                   model=run.models.get('core-1')),
        'list_findings': lambda: srv.list_findings(editor),
        'submit_issues': lambda: srv.submit_issues(
            editor, retire=['The reference list does not hold up']),
        'submit_editor': lambda: srv.submit_editor(
            editor, summary='On reflection the paper is fine.',
            significance='Nothing here matters after all.',
            strength_of_evidence='Fully supported.',
            verdict_reasoning='I have changed my mind about every issue of this review.'),
        # The Read stage's tools. This run was read by nobody and sealed long
        # ago, so every one of them is a call against a manuscript that is
        # already written: refused, and the folder untouched.
        'read_draft': lambda: srv.read_draft(reviewer),
        'replace_equation': lambda: srv.replace_equation(reviewer, 'p1', 1, 'E = mc^2'),
        'replace_table': lambda: srv.replace_table(reviewer, 'p1', 1, [['a', 'b']]),
        'describe_figure': lambda: srv.describe_figure(reviewer, 'p1', 1, 'A plot of a widget.'),
        'add_piece': lambda: srv.add_piece(reviewer, 'p1', 'equation', 1, latex='x = 1'),
        'merge_equations': lambda: srv.merge_equations(reviewer, ['p1', 'p2'], 1, 'x = 1'),
        'reread_piece': lambda: srv.reread_piece(reviewer, 'p1', 1,
                                                 [{'kind': 'equation', 'latex': 'x = 1'}]),
        'withdraw_piece': lambda: srv.withdraw_piece(reviewer, 'p1'),
        'confirm_piece': lambda: srv.confirm_piece(reviewer, 'p1', 1),
        'seal_manuscript': lambda: srv.seal_manuscript(reviewer),
        'seal_as_converted': lambda: srv.seal_as_converted(key, 'a late change of mind'),
        'resolve_citations': lambda: resolving(key, limit=0),
        'citation_rows': lambda: srv.citation_rows(token_for('spec-citations'), 'counts'),
        'claim_citation_batch': lambda: srv.claim_citation_batch(worker, size=10,
                                                                 model='claude-haiku-5-5'),
        'submit_citation_verdict': lambda: srv.submit_citation_verdict(
            worker, row_id, 'contradicts', passage=ABSTRACT.split('. ')[0] + '.'),
        'mark_blocked': lambda: srv.mark_blocked(key, 'Something went wrong after all.'),
        'run_status': lambda: srv.run_status(key),
        'write_review': lambda: srv.write_review(recorder),
    }
    # A tool nobody listed here is a tool nobody checked.
    assert set(calls) == {tool.__name__ for tool in srv.TOOLS}
    # The two that answer rather than refuse, and write nothing either way: one
    # reports how the run stands, which is how a lost answer is recovered, and
    # the other reads the pool the editor already ruled on.
    reads = {'run_status', 'list_findings'}
    # And the one tool that refuses every caller in every state of every run:
    # it belongs to the standalone engine, and there is nothing about this run
    # for its refusal to say.
    always_refuses = {'validate_synthesis'}
    # The Read stage's tools belong to the reader slot, and a finished run has
    # no reader to call them with: whichever of the two refusals comes first —
    # this run is finished, or this slot may not call that tool — the call is
    # refused and the folder is untouched, which is what this test is about.
    # The seal refuses for a reason of its own: the manuscript is written, and
    # a finding may already cite the lines it wrote, so nothing re-seals it
    # whatever state the rest of the run is in.
    reader_only = {*srv.READER_TOOLS, 'seal_as_converted'}
    before = folder_digests(run.root)

    for name, call in calls.items():
        answer = call()
        assert folder_digests(run.root) == before, f'{name} wrote in a finished folder'
        if name in reads:
            assert 'request_rejected' not in answer, (name, answer)
            continue
        assert 'request_rejected' in answer, (name, answer)
        if name in always_refuses:
            continue
        if name in reader_only:
            assert ('complete' in answer['request_rejected']
                    or 'may not' in answer['request_rejected']
                    or 'already sealed' in answer['request_rejected']), (name, answer)
            continue
        assert 'complete' in answer['request_rejected'], (name, answer)
    assert read_run(run.review)['status'] in names.COMPLETE_STATUSES
    assert run.status in names.COMPLETE_STATUSES, 'and the run still says what it says'


def test_no_file_of_a_two_track_run_names_the_machine_it_ran_on(tmp_path):
    """REVIEW.md and REVIEW.txt printed the absolute path of the ledger.

    The reviewers-only arm keeps no ledger, so the scan that checks every file
    for the run's own location passed over the one row that held one: the
    citations page's Ledger field, written straight from the run folder. A
    published name is run-relative POSIX, and the test runs on the arm that has
    every file a run can write.
    """
    run, _, _ = complete_two_track_run(tmp_path)

    for path in sorted(run.root.rglob('*')):
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='ignore')
        where = path.relative_to(run.root).as_posix()
        for machine in (str(run.root), str(tmp_path)):
            # Raw, and escaped as a JSON string writes a Windows path.
            assert machine not in text and json.dumps(machine)[1:-1] not in text, (
                f'{where} names {machine}')
    assert cl.LEDGER_PATH in (run.review / 'REVIEW.md').read_text(encoding='utf-8'), (
        'the ledger is still named, by the name it is published under')


def test_a_moved_complete_folder_re_renders_to_the_same_two_files(tmp_path):
    """``peerreview report`` on a copy of a finished run changes nothing.

    A report that names the folder it was rendered in cannot be re-rendered
    anywhere else: the same run, copied or moved, produced a different
    REVIEW.md. Byte-identical is the whole claim, and it is only true once no
    path from the machine is printed.
    """
    run, _, _ = complete_two_track_run(tmp_path)
    before = {name: (run.review / name).read_bytes() for name in ('REVIEW.md', 'REVIEW.txt')}
    moved = tmp_path / 'moved' / 'run'
    shutil.copytree(run.root, moved)

    assert report_main([str(moved)]) == 0
    assert report_main([str(run.root)]) == 0

    for name, raw in before.items():
        assert (moved / names.RUN_DIRECTORY / name).read_bytes() == raw, f'{name} moved'
        assert (run.review / name).read_bytes() == raw, f'{name} re-rendered in place'


def test_an_unfinished_ledger_is_reported_and_does_not_hold_the_review(tmp_path):
    """The reader is told which references nobody reached. It is not a veto."""
    open_run(tmp_path)
    run = srv.current()
    finish(run)
    reading = read_run(run.review)
    assert reading['citations']['complete'] is False
    assert reading['citations']['problems']
    assert any('citation' in line or 'reference' in line for line in reading['problems'])
    assert all('citation' not in reason and 'reference' not in reason
               for reason in reading['not_publishable_reasons'])
    blocks = report_blocks(run.review)
    labeled = [block for block in blocks if block[0] == 'labeled']
    assert any('Citation checks unfinished' in block[1] for block in labeled)
