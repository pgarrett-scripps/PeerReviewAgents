"""The ``peerreview`` MCP server: the run's owner of identity, guard, ledger and files.

Claude Code runs the review; this server makes no model calls. One process serves
every agent in one run, so the things a model must not be trusted with live here:

* **Identity.** An agent claims a slot once and gets a token. A finding's track
  comes from the token, never from what the agent says it is.
* **The ledger.** Every finding passes the engine's anchoring guard before it is
  stored, and ``write_review`` refuses while a required stage has not reported.
* **Status.** ``running``, ``blocked``, ``complete``, ``complete-without-findings``.
* **Budgets.** A hard cap on tool calls per agent and per run, and the engine's
  own cap on outside lookups.
* **Run files and provenance.** ``protocol.json``, ``progress.json``,
  ``manifest.json``, ``editor-package.json``, ``audit_<track>.md`` and
  ``REVIEW.md``, with the paper, plugin, workflow and engine hashes beside them.

The paper tools are the engine's own: every one of them is delegated to a
per-agent :class:`peerreviewagents.engine.investigation.Investigation`, which is
where the twelve-block read limit, the literal-phrase search, the arithmetic
receipts and the source allowance already live.
"""

from __future__ import annotations

import base64
import datetime
import json
import math
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from ..engine.annotations import normalize_review, quarantine_findings
from ..engine.domain_registry import context as domain_context
from ..engine.domain_registry import route as domain_route
from ..engine.ingest import import_paper
from ..engine.investigation import Investigation
from ..engine.storage import digest, read, write
from ..names import (
    COMPLETE_STATUSES,
    FINDING_SCHEMA_VERSION,
    MANUSCRIPT_DIRECTORY,
    RUN_DIRECTORY,
)
from . import models as model_plan

# The folder names, the statuses and the finding schema version are imported
# from `peerreviewagents.names` because this module writes them; that module is
# where they are defined and where an integration reads them from. Importing
# them from here instead is not supported and nothing in the repository does.
WORKFLOW_VERSION = 'plugin-two-track-v1'
SPECIALIST_TRACKS = ('stats', 'figures-tables', 'methods-reporting', 'citations',
                     'consistency', 'field')
MAX_REVIEWERS = 8
BODY_LIMIT = 8
SEVERITIES = ('major', 'moderate', 'minor')
KINDS = ('question', 'edit', 'literature')
PLACEMENTS = ('body', 'appendix', 'duplicate')
NUMERIC = r'(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])'
# The same number without its sign. An expression is tokenised with this one:
# `b - a` and `b * -a` are arithmetic on the two printed values, not on a
# negative number nobody printed.
NUMERIC_UNSIGNED = r'(?<![\w.])\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])'
# A finding may point at a handful of blocks, not at the whole manuscript. With
# no cap, citing every block makes every number in the paper available to a
# receipt, which turns `computed` into a label an agent can arrange.
MAX_EVIDENCE_BLOCKS = 6

OFFLINE_TOOLS = ('read_blocks', 'search', 'calculate', 'save_note', 'inspect_results',
                 'record_comparison')
IMAGE_TOOLS = ('view_page', 'view_supplement')
NETWORK_TOOLS = ('check_citation', 'search_literature', 'resolve_identifier', 'lookup_source')
PAPER_TOOLS = (*OFFLINE_TOOLS, *IMAGE_TOOLS, *NETWORK_TOOLS, 'validate_synthesis')

TRACK_TOOLS = {
    'core': (*OFFLINE_TOOLS, *IMAGE_TOOLS, *NETWORK_TOOLS),
    'stats': (*OFFLINE_TOOLS, 'view_page'),
    'figures-tables': (*OFFLINE_TOOLS, *IMAGE_TOOLS),
    'methods-reporting': (*OFFLINE_TOOLS, 'view_page', 'resolve_identifier', 'lookup_source'),
    'citations': (*OFFLINE_TOOLS, *NETWORK_TOOLS),
    'consistency': (*OFFLINE_TOOLS, 'view_page'),
    'field': (*OFFLINE_TOOLS, *IMAGE_TOOLS, *NETWORK_TOOLS),
    'editor': ('read_blocks', 'search', 'calculate', 'inspect_results'),
    'recorder': (),
}

CALL_BUDGET = {'core': 30, 'specialist': 20, 'editor': 12, 'recorder': 2}
EXTERNAL_BUDGET = {'core': 24, 'specialist': 16, 'editor': 0, 'recorder': 0}
RUN_CALL_BUDGET = 600

# A study arm is a setting of this one workflow, not a separate program. Each arm
# records itself in protocol.json beside the workflow hash, so two runs of the
# same arm are comparable and two runs of different arms are never pooled.
ARMS = {
    'full-two-track': {'reviewers': None, 'specialists': True, 'editor': True,
                       'revision': False, 'tools': PAPER_TOOLS,
                       'summary': 'N core reviewers plus all six specialists, then the editor'},
    'reviewers-only': {'reviewers': None, 'specialists': False, 'editor': True,
                       'revision': False, 'tools': PAPER_TOOLS,
                       'summary': 'N core reviewers, no specialist track, then the editor'},
    'single': {'reviewers': 1, 'specialists': False, 'editor': False,
               'revision': False, 'tools': PAPER_TOOLS,
               'summary': 'One core reviewer, one pass, no editor: the baseline'},
    'single-revision': {'reviewers': 1, 'specialists': False, 'editor': False,
                        'revision': True, 'tools': PAPER_TOOLS,
                        'summary': 'One core reviewer, then the same brief revising its own findings'},
    'manuscript-only': {'reviewers': 1, 'specialists': False, 'editor': False,
                        'revision': False, 'tools': OFFLINE_TOOLS,
                        'summary': 'One core reviewer with the manuscript text only: no pages, no lookups'},
    # Not a study arm: what the check-draft skill runs on a draft of your own.
    'draft-check': {'reviewers': 0, 'specialists': True, 'editor': False,
                    'revision': False, 'tools': PAPER_TOOLS,
                    'summary': 'Chosen specialists on a draft: no core track and no editor'},
}
DEFAULT_ARM = 'full-two-track'


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def rejection(message, **extra):
    """A refusal an agent can act on, not an exception: the agent keeps its turn."""
    return {'request_rejected': message, **extra}


def as_number(text):
    """One token as a float, or ``None``. ``0.50`` and ``0.5`` are the same number."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def same_number(one, other):
    """Numeric equality, so a receipt is checked on values and not on spelling."""
    return abs(one - other) <= 1e-9 * max(1.0, abs(one), abs(other))


def is_power_of_ten(value):
    """``1``, ``100``, ``0.01``: a unit or percentage conversion, which claims nothing.

    The only constants a receipt may carry that the manuscript does not print.
    Everything else — an effect size, a sample size, a threshold — has to come
    from a block the finding cites, or the finding is judgement.
    """
    if value == 0:
        return False
    exponent = round(math.log10(abs(value)))
    return same_number(abs(value), 10.0 ** exponent)


@dataclass
class Agent:
    slot: str
    role: str
    track: str
    token: str
    attempt: int
    out: Path
    allowed: tuple
    call_budget: int
    sees: tuple
    investigation: object = None
    calls: int = 0
    receipts: dict = field(default_factory=dict)
    report: dict = None
    editor_package: dict = None


def plan_slots(arm, reviewers, tracks=None):
    """The slots one arm asks for, without a run: ``core-1``, ``spec-stats``, …

    :class:`Run` answers this about itself, but the launcher has to answer it
    before a run exists — it settles which model each slot would run, and
    refuses an unavailable one, before the manuscript is imported or a session
    is started.
    """
    plan = ARMS[arm]
    fixed = plan['reviewers']
    count = reviewers if fixed is None else fixed
    chosen = tuple(t for t in (tracks or SPECIALIST_TRACKS) if t in SPECIALIST_TRACKS)
    slots = [f'core-{index}' for index in range(1, (count or 0) + 1)]
    if plan['revision']:
        slots.append('revision-1')
    if plan['specialists']:
        slots.extend(f'spec-{track}' for track in chosen)
    if plan['editor']:
        slots.append('editor')
    return tuple(slots)


class Run:
    """One review in one folder. Created by ``ingest``, finished by ``write_review``."""

    def __init__(self, pdf, root, reviewers, arm, literature_cutoff, plugin_root, title=None,
                 tracks=None, reviewer_models=None, specialist_model=None, editor_model=None,
                 recorder_model=None):
        if arm not in ARMS:
            raise ValueError('Unknown arm: ' + arm + '. Arms: ' + ', '.join(sorted(ARMS)))
        self.arm = arm
        self.plan = ARMS[arm]
        fixed = self.plan['reviewers']
        wanted = reviewers if fixed is None else fixed
        floor = 0 if fixed == 0 else 1
        if type(wanted) is not int or not floor <= wanted <= MAX_REVIEWERS:
            raise ValueError(f'Review with {floor} to {MAX_REVIEWERS} core reviewers')
        self.reviewers = wanted
        chosen = tuple(t for t in (tracks or SPECIALIST_TRACKS) if t in SPECIALIST_TRACKS)
        if tracks and not chosen:
            raise ValueError('tracks must name specialists: ' + ', '.join(SPECIALIST_TRACKS))
        self.tracks = chosen
        # Which model runs in which slot, settled before anything is imported
        # or written: a model this login cannot run is refused here, rather
        # than quietly substituted halfway through a run and recorded as the
        # arm it was not. The reviewers are deliberately different models —
        # see `models.py` for the ordering and the defaults.
        self.reviewer_models = (tuple(model_plan.parse_list(reviewer_models))
                                or tuple(model_plan.DEFAULT_REVIEWER_MODELS))
        # What this run chose, role by role, so a second `ingest` in the same
        # process can be told whether it is asking for the same run or a
        # different one. The per-slot mapping cannot answer that on its own: it
        # does not say which role put a model where.
        self.model_choice = {
            'reviewers': list(self.reviewer_models),
            'specialist': model_plan.choice(specialist_model,
                                            model_plan.DEFAULT_SPECIALIST_MODEL),
            'editor': model_plan.choice(editor_model, model_plan.DEFAULT_EDITOR_MODEL),
            'recorder': model_plan.choice(recorder_model, model_plan.DEFAULT_RECORDER_MODEL),
        }
        self.models = model_plan.refuse(model_plan.assign(
            (*self.required_slots(), 'recorder'),
            reviewers=self.reviewer_models, specialist=specialist_model,
            editor=editor_model, recorder=recorder_model))
        self.root = Path(root).resolve()
        self.workspace = self.root / MANUSCRIPT_DIRECTORY
        self.review = self.root / RUN_DIRECTORY
        self.cutoff = literature_cutoff
        self.plugin_root = Path(plugin_root).resolve() if plugin_root else None
        self.refuse_foreign_folder(pdf)
        self.review.mkdir(parents=True, exist_ok=True)
        if (self.workspace / 'paper.json').is_file():
            self.paper = read(self.workspace / 'paper.json')
        else:
            self.paper = import_paper(pdf, self.workspace, title=title)
        self.pdf = str(Path(pdf).resolve())
        self.blocks = {b['id']: b for b in self.paper['blocks']}
        self.salt = secrets.token_hex(16)
        self.agents = {}
        self.tokens = {}
        self.slot_calls = {}
        self.findings = []
        self.quarantined = []
        self.calls = 0
        self.status = 'running'
        self.error = None
        self.started = now()
        self.required = self.required_slots()
        # Identity. A slot is claimed with the one code minted for it here, and
        # the codes go back to `ingest`'s caller — the recorder — and nowhere
        # else. Without this a reviewer that knows a slot name is that slot.
        self.recorder_key = secrets.token_hex(16)
        self.claim_codes = {slot: secrets.token_hex(8)
                            for slot in (*self.required, 'recorder')}
        self.write_progress()
        # Identity at ingest, not only at the end. A run that is killed — a
        # token ceiling, the wall clock, a rate limit, a session error — used to
        # leave a folder that could not say which paper it had been reading or
        # which arm it was running, because `protocol.json` and `manifest.json`
        # were written by `write_review` and `mark_blocked` alone. Both are
        # rewritten at every later status change, so the end state still wins.
        self.write_protocol()
        self.write_manifest()

    def refuse_foreign_folder(self, pdf):
        """An out folder holds one paper under one plan, or the run is not this run.

        A folder reused for a different PDF keeps the first import — the review
        would quote a manuscript nobody passed in — and a folder reused under a
        different arm, or under different models, pools two arms the study
        design keeps apart. A folder written before models were recorded says
        nothing about them, and is left alone rather than guessed at.
        """
        kept = self.workspace / 'paper.json'
        if kept.is_file():
            try:
                before = read(kept)
            except (ValueError, OSError):
                before = {}
            source = Path(pdf)
            incoming = digest(source.read_bytes()) if source.is_file() else None
            if incoming and before.get('source_sha256') and incoming != before['source_sha256']:
                raise ValueError(
                    'The folder ' + str(self.root) + ' already holds a different manuscript ('
                    + str(before.get('title'))[:60] + '). Review this PDF in a fresh folder.')
        progress = self.review / 'progress.json'
        if progress.is_file():
            try:
                before = read(progress)
            except (ValueError, OSError):
                before = {}
            if before.get('arm') and (before['arm'] != self.arm
                                      or before.get('reviewers') != self.reviewers):
                raise ValueError(
                    'The folder ' + str(self.root) + ' already holds a ' + str(before['arm'])
                    + ' run with ' + str(before.get('reviewers')) + ' core reviewer(s). Two arms '
                    'are never pooled in one folder: use a fresh one for ' + self.arm + '.')
        protocol = self.review / 'protocol.json'
        if protocol.is_file():
            try:
                before = read(protocol)
            except (ValueError, OSError):
                before = {}
            kept_models = before.get('models')
            if isinstance(kept_models, dict) and kept_models and kept_models != self.models:
                differs = sorted(slot for slot in set(kept_models) | set(self.models)
                                 if kept_models.get(slot) != self.models.get(slot))
                raise ValueError(
                    'The folder ' + str(self.root) + ' already holds a run under different '
                    'models (' + ', '.join(
                        f'{slot}: {kept_models.get(slot) or "no model"} there, '
                        f'{self.models.get(slot) or "no model"} here' for slot in differs)
                    + '). Two model configurations are never pooled in one folder: use a '
                    'fresh one, or change them back with '
                    # The flag for the slots that actually differ. Naming the
                    # reviewer flag for an editor that changed sends a reader
                    # to a setting that is not the one in the way.
                    + ', '.join(sorted({model_plan.FLAG[model_plan.role_of(slot)]
                                        for slot in differs})) + '.')

    # -- plan ---------------------------------------------------------------

    def required_slots(self):
        return plan_slots(self.arm, self.reviewers, self.tracks)

    def resolve_slot(self, slot):
        """Role, track and what a slot may see. Unknown slots are refused, not invented.

        The refusal names no other slot. Listing them tells a core reviewer that
        the specialists exist, and reviewer isolation is one of the things the
        study arms compare.
        """
        if slot not in (*self.required, 'recorder'):
            raise ValueError('Slot ' + slot + ' is not part of this run.')
        if slot == 'recorder':
            return 'recorder', 'recorder', tuple(self.required)
        if slot == 'editor':
            return 'editor', 'editor', tuple(self.required)
        if slot.startswith('spec-'):
            return 'specialist', slot[len('spec-'):], (slot,)
        if slot == 'revision-1':
            return 'core', 'core', ('core-1', 'revision-1')
        return 'core', 'core', (slot,)

    def claim(self, slot, claim_code):
        """Bind one holder to one slot, against the code minted for it at ingest.

        The code works once. A slot already claimed is not re-claimed by a
        second holder: a retry goes through the recorder, which reissues the
        code and so drops the stuck holder's token.
        """
        role, track, sees = self.resolve_slot(slot)
        expected = self.claim_codes.get(slot)
        if expected is None:
            raise ValueError(
                'Slot ' + slot + ' has already been claimed in this run. A slot is claimed once; '
                'a retry needs a fresh code, which only the recorder can reissue.')
        if not secrets.compare_digest(str(claim_code or ''), expected):
            raise ValueError(
                'That is not the claim code for ' + slot + '. Your code is in your own prompt, it '
                'belongs to your slot alone, and it works once.')
        self.claim_codes.pop(slot, None)
        previous = self.agents.get(slot)
        attempt = (previous.attempt + 1) if previous else 1
        if previous is not None:
            self.tokens.pop(previous.token, None)
        allowed = tuple(name for name in TRACK_TOOLS[track if track in TRACK_TOOLS else 'core']
                        if name in self.plan['tools'])
        budget = CALL_BUDGET['specialist' if role == 'specialist' else role]
        # The allowance belongs to the slot, not to the claim. A second attempt
        # continues the first one's count, so a re-claim cannot buy more calls.
        used = self.slot_calls.get(slot, 0)
        token = digest(f'{self.salt}:{slot}:{attempt}')[:32]
        out = self.review / slot
        out.mkdir(parents=True, exist_ok=True)
        agent = Agent(slot=slot, role=role, track=track, token=token, attempt=attempt,
                      out=out, allowed=allowed, call_budget=budget, sees=sees, calls=used)
        if allowed:
            agent.investigation = Investigation({
                'paper': str(self.workspace),
                'output': str(out),
                'cache': str(self.root / 'cache'),
                'tool_limit': max(0, budget - used),
                'literature_cutoff': self.cutoff,
                'external_request_limit': EXTERNAL_BUDGET[
                    'specialist' if role == 'specialist' else role],
            })
        self.agents[slot] = agent
        self.tokens[token] = agent
        return agent

    def reissue(self, slot):
        """A fresh claim code for one slot, which also unbinds whoever holds it."""
        self.resolve_slot(slot)
        previous = self.agents.get(slot)
        if previous is not None:
            self.tokens.pop(previous.token, None)
        self.claim_codes[slot] = secrets.token_hex(8)
        return self.claim_codes[slot]

    def agent_for(self, token):
        agent = self.tokens.get(token)
        if agent is None:
            raise ValueError('Unknown or superseded agent token. Claim your slot first.')
        return agent

    # -- guard --------------------------------------------------------------

    def finding_uid(self, agent, raw):
        """One issue's identity: who raised it, and the evidence it cites.

        The model-written title is deliberately not in it. With the title in the
        key, the same reviewer restating the same issue in better words filed a
        second finding, while two reviewers who happened to agree on a title
        filed one — the count said four and the ledger held three. The slot is
        in the key so that agreement between reviewers stays visible as two
        rows for the editor to merge, rather than being merged here.
        """
        handles = sorted({str(handle) for handle
                          in [raw.get('block_id'), *(raw.get('evidence_blocks') or [])] if handle})
        key = ':'.join([self.paper['id'], agent.slot,
                        ' '.join(str(raw.get('quote', '')).split()).casefold(),
                        '|'.join(handles)])
        return digest(key)[:16]

    def cited_blocks(self, stored, corroborating):
        """Every block a finding points at: its anchor, its evidence, its corroboration."""
        ids = [stored.get('block_id'), *(stored.get('evidence_blocks') or [])]
        if isinstance(corroborating, dict):
            ids.append(corroborating.get('block_id'))
        elif corroborating:
            ids.append(corroborating)
        return [block_id for block_id in ids if block_id]

    def printed_numbers(self, stored, corroborating):
        """Every number the manuscript prints in the blocks this finding cites.

        The quoted sentence is one sentence of its block, and a reviewer
        quoting a claim rarely quotes every number the claim combines: the
        other half is usually two rows up in the same table. What a receipt has
        to answer for is that its numbers come from the paper, not that they sit
        inside the quotation marks, so the whole text of every cited block
        counts — and no text that the finding does not cite.
        """
        texts = [stored.get('quote', '')]
        for block_id in self.cited_blocks(stored, corroborating):
            texts.append(self.blocks.get(block_id, {}).get('text', ''))
        printed = set()
        for text in texts:
            for token in re.findall(NUMERIC, text or ''):
                value = as_number(token)
                if value is not None:
                    # Both signs: a receipt is tokenised unsigned, so a printed
                    # −0.5 has to match the 0.5 in `a - 0.5` as well.
                    printed |= {value, abs(value)}
        return printed

    def basis(self, agent, stored, receipt_id, corroborating):
        """``computed``, ``two-quotes`` or ``judgement``, decided here from receipts.

        A receipt is ``computed`` when every number it works on is printed in
        the blocks the finding cites. A number that appears in none of them came
        from somewhere the server cannot check — the agent's own prose, or a
        figure read by eye — and that is judgement however the arithmetic looks.

        A figure reading is judgement whatever else it carries: a number read off
        pixels is not a guard-checked number, so it can never be ``computed``.
        """
        block = self.blocks.get(stored.get('block_id'), {})
        if str(block.get('kind', '')).startswith('figure'):
            return 'judgement', 'A reading taken from a figure is judgement.'
        receipt = agent.receipts.get(receipt_id) if receipt_id else None
        if receipt:
            printed = self.printed_numbers(stored, corroborating)
            inputs = {}
            for token in re.findall(NUMERIC_UNSIGNED, str(receipt.get('expression') or '')):
                value = as_number(token)
                if value is not None:
                    inputs[token] = value
            unmatched = sorted(token for token, value in inputs.items()
                               if not is_power_of_ten(value)
                               and not any(same_number(value, other) for other in printed))
            if inputs and not unmatched:
                return 'computed', ('Arithmetic receipt over numbers printed in the blocks this '
                                    'finding cites, matched by value.')
            if inputs:
                return ('judgement',
                        'The receipt uses numbers that none of the cited blocks print: '
                        + ', '.join(unmatched))
            return 'judgement', 'The receipt works on no numbers at all.'
        if corroborating:
            return 'two-quotes', ('Two guard-checked passages. This shows both exist; it does not '
                                  'prove they conflict.')
        return 'judgement', 'No arithmetic receipt and no second anchored passage.'

    def anchor(self, block_id, quote):
        """One exact, unique position for a quote, or ``None``. The editor's rebuttal test."""
        block = self.blocks.get(block_id)
        if not block or not quote:
            return None
        found = [m.start() for m in re.finditer('(?=' + re.escape(quote) + ')', block['text'])]
        return found[0] if len(found) == 1 else None

    def store_finding(self, agent, raw, escalate, receipt_id, corroborating):
        response = {'summary': '', 'findings': [raw], 'limitations': []}
        normalized = normalize_review(self.paper, response, f'{agent.slot}-{agent.attempt}')
        kept, quarantined = quarantine_findings(normalized['findings'])
        if quarantined:
            record = {**quarantined[0], 'track': agent.track, 'slot': agent.slot,
                      'schema_version': FINDING_SCHEMA_VERSION}
            self.quarantined.append(record)
            self.append(self.review / 'quarantined.jsonl', record)
            return None, quarantined[0]['quarantine_reason']
        stored = kept[0]
        stored['schema_version'] = FINDING_SCHEMA_VERSION
        stored['track'] = agent.track
        stored['slot'] = agent.slot
        stored['role'] = agent.role
        stored['escalate'] = bool(escalate) and agent.role == 'specialist'
        stored['finding_uid'] = self.finding_uid(agent, raw)
        stored['basis'], stored['basis_reason'] = self.basis(agent, stored, receipt_id,
                                                             corroborating)
        if corroborating:
            stored['corroborating'] = corroborating
        if receipt_id and receipt_id in agent.receipts:
            stored['calculate_receipt'] = {'receipt': receipt_id, **agent.receipts[receipt_id]}
        stored['recorded'] = now()
        # Nothing is merged or dropped here. Every submission is a row, and the
        # editor rules on one row per `finding_uid` — the latest. Storage that
        # silently replaced a slot's earlier findings lost them from the ledger
        # while the counts still reported them.
        self.findings.append(stored)
        self.append(self.review / 'findings.jsonl', stored)
        return stored, None

    def pooled(self):
        """One row per ``finding_uid``: the latest submission of each, in first-seen order."""
        order, latest = [], {}
        for finding in self.findings:
            uid = finding['finding_uid']
            if uid not in latest:
                order.append(uid)
            latest[uid] = finding
        return [latest[uid] for uid in order]

    def guard_status(self, slot=None):
        """``quarantined`` when the guard threw out one of *this* reviewer's findings."""
        rows = [q for q in self.quarantined if slot is None or q.get('slot') == slot]
        return 'quarantined' if rows else 'clean'

    # -- disk ---------------------------------------------------------------

    @staticmethod
    def append(path, record):
        with Path(path).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def outline(self, limit=400):
        return [{'id': b['id'], 'kind': b.get('kind'), 'section': b.get('section'),
                 'characters': len(b.get('text', ''))} for b in self.paper['blocks'][:limit]]

    def ledger(self):
        reported = {slot: self.agents[slot].report for slot in self.required
                    if slot in self.agents and self.agents[slot].report}
        return {
            'required': list(self.required),
            'reported': sorted(reported),
            'missing': [slot for slot in self.required if slot not in reported],
            'none_found': sorted(slot for slot, report in reported.items()
                                 if report.get('none_found')),
            'attempts': {slot: agent.attempt for slot, agent in self.agents.items()},
            'editor_accepted': bool(self.agents.get('editor')
                                    and self.agents['editor'].editor_package),
        }

    def visible_ledger(self, agent):
        """The ledger as one caller may read it.

        A core reviewer or a specialist is shown its own stage and no other. The
        full ledger names every slot in the run, and a reviewer that can read
        ``spec-stats`` in it knows the specialist track exists.
        """
        if agent.role in ('editor', 'recorder'):
            return self.ledger()
        slots = [slot for slot in agent.sees if slot in self.required]
        reported = [slot for slot in slots
                    if slot in self.agents and self.agents[slot].report]
        return {
            'required': slots,
            'reported': sorted(reported),
            'missing': [slot for slot in slots if slot not in reported],
            'none_found': sorted(slot for slot in reported
                                 if self.agents[slot].report.get('none_found')),
            'attempts': {slot: self.agents[slot].attempt for slot in slots
                         if slot in self.agents},
            'scope': 'your own stage only',
        }

    def write_progress(self):
        write(self.review / 'progress.json', {
            'status': self.status,
            'error': self.error,
            'reviewers': self.reviewers,
            'arm': self.arm,
            'stages': self.ledger(),
            'findings_delivered': len(self.pooled()),
            'findings_submitted': len(self.findings),
            'quarantined_findings_count': len(self.quarantined),
            'review_guard_status': self.guard_status(),
            'tool_calls': {slot: agent.calls for slot, agent in self.agents.items()},
            'tool_calls_total': self.calls,
            'started': self.started,
            'updated': now(),
        })

    def provenance(self):
        files = {}
        if self.plugin_root and self.plugin_root.is_dir():
            for path in sorted(self.plugin_root.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:
                    files['plugin/' + path.relative_to(self.plugin_root).as_posix()] = digest(
                        path.read_bytes())
        engine = Path(__file__).resolve().parent.parent / 'engine'
        for path in sorted(engine.glob('*.py')):
            files['engine/' + path.name] = digest(path.read_bytes())
        here = Path(__file__).resolve().parent
        for path in sorted(here.glob('*.py')):
            files['plugin-python/' + path.name] = digest(path.read_bytes())
        return {
            'paper_id': self.paper['id'],
            'source_sha256': self.paper.get('source_sha256'),
            'pdf': self.pdf,
            'code': files,
            'workflow_version': WORKFLOW_VERSION,
            'finding_schema_version': FINDING_SCHEMA_VERSION,
            'claude_code_version': os.environ.get('PEERREVIEW_CLAUDE_VERSION'),
            'plugin_root': str(self.plugin_root) if self.plugin_root else None,
            'plan': {'arm': self.arm, 'reviewers': self.reviewers,
                     'specialists': list(self.tracks) if self.plan['specialists'] else [],
                     'editor': self.plan['editor'], 'revision': self.plan['revision'],
                     'summary': self.plan['summary'],
                     'models': dict(self.models),
                     'reviewer_models': list(self.reviewer_models),
                     'execution': 'Claude Code plugin workflow; agents in parallel, '
                                  'server-owned ledger.'},
        }

    def write_protocol(self):
        write(self.review / 'protocol.json', {
            'provider': 'claude-code',
            # `model` stays a string, because that is what every consumer of
            # this file already reads; `models` is the per-slot record a
            # published review needs to say which model wrote which report.
            'model': model_plan.describe(self.models),
            'effort': 'per-agent, pinned in the plugin agent files',
            'models': dict(self.models),
            'reviewer_models': list(self.reviewer_models),
            'mode': self.arm,
            'arm': self.arm,
            'reviewers': self.reviewers,
            'paper_id': self.paper['id'],
            'literature_cutoff': self.cutoff,
            'version': WORKFLOW_VERSION,
            'finding_schema_version': FINDING_SCHEMA_VERSION,
            'independent_tool_allowance': CALL_BUDGET['core'],
            'specialist_tool_allowance': CALL_BUDGET['specialist'],
            'run_tool_allowance': RUN_CALL_BUDGET,
            'body_finding_limit': BODY_LIMIT,
            'agents': {slot: {'role': agent.role, 'track': agent.track,
                              'attempt': agent.attempt, 'tool_calls': agent.calls,
                              'model': (agent.report or {}).get('model'),
                              'assigned_model': self.models.get(slot),
                              'allowed_tools': list(agent.allowed)}
                       for slot, agent in sorted(self.agents.items())},
            'plan': self.provenance()['plan'],
        })

    def write_manifest(self):
        write(self.review / 'manifest.json', self.provenance())


STATE = {'run': None, 'plugin_root': None}


def current():
    run = STATE['run']
    if run is None:
        raise ValueError('No run yet. Call ingest first.')
    return run


# -- tools ------------------------------------------------------------------


def _model_disagreement(run, settled):
    """What an attaching call asks for that the live run does not do, in words.

    Only what the caller actually named is compared. A second ``ingest`` that
    passes no models at all is attaching to the run as it stands, and reading
    its silence as a request for the defaults would refuse the ordinary attach
    a retry makes.
    """
    said = []
    for role, wanted in (settled or {}).items():
        if role == 'reviewers':
            listed = tuple(model_plan.parse_list(wanted))
            if listed and listed != tuple(run.reviewer_models):
                said.append(', '.join(run.reviewer_models) + ' as its core reviewers, not '
                            + ', '.join(listed) + ' (' + model_plan.FLAG['core'] + ')')
            continue
        mine = run.model_choice.get(role)
        if model_plan.normalize(wanted) != mine:
            said.append(str(mine) + ' as its ' + role + ', not ' + model_plan.normalize(wanted)
                        + ' (' + model_plan.FLAG[role] + ')')
    return '; '.join(said)


def ingest(pdf: str, out: str, reviewers: int = 3, arm: str = DEFAULT_ARM,
           literature_cutoff: str = '', title: str = '', tracks: list = None,
           reviewer_models: list = None, specialist_model: str = '',
           editor_model: str = '', recorder_model: str = '') -> dict:
    """Open a run: import the manuscript PDF and fix the plan. Safe to call twice.

    ``tracks`` narrows the specialist track to the ones named; the default is
    all six. ``arm`` is the study arm, and it decides which stages must report.

    ``reviewer_models`` is the ordered list the core reviewers take their model
    from — reviewer *i* runs ``list[(i - 1) % len(list)]``, so the default
    ``claude-sonnet-5-5, claude-opus-5-5, claude-haiku-5-5`` makes three
    reviewers three different models. ``specialist_model``, ``editor_model``
    and ``recorder_model`` are one model each, so every slot is settable the
    same way. Anything omitted here falls back to what the launcher asked for
    in the environment, then to the defaults in ``models.py`` — one merge,
    ``models.settle``, the same one the launcher plans from. The answer's
    ``models`` says which model each slot is to run, and ``spawn`` is the
    override to pass that agent.
    """
    try:
        # The arguments over the environment, once, in the module that owns the
        # rule. The launcher plans from this same merge, so a session started
        # with PEERREVIEW_* variables set and a workflow that passes no model
        # arguments reach the same answer instead of two.
        settled = model_plan.settle(reviewer_models, specialist_model, editor_model,
                                    recorder_model)
    except ValueError as exc:
        return rejection(str(exc))
    run = STATE['run']
    if run is not None and str(Path(out).resolve()) == str(run.root):
        wanted = ARMS[arm]['reviewers'] if arm in ARMS else reviewers
        if arm != run.arm or (wanted if wanted is not None else reviewers) != run.reviewers:
            return rejection('This run is the ' + run.arm + ' arm with ' + str(run.reviewers)
                             + ' core reviewer(s). An attach does not change the plan: review '
                             'the ' + str(arm) + ' arm in a fresh out folder.')
        # The models are as much the plan as the arm is. A second caller asking
        # for different ones is not attaching to this run, and answering
        # `attached: true` would hand it a run whose reports will be recorded
        # under models it did not ask for.
        differs = _model_disagreement(run, settled)
        if differs:
            return rejection('This run already runs ' + differs + '. An attach does not change '
                             'the models: review the models you want in a fresh out folder.')
        # The claim codes are minted once and go back once. A second caller
        # attaching to a live run is not the recorder: no identities, and not
        # the slot list either — nor the per-slot models, which name the slots.
        hidden = ('slots', 'models')
        return {k: v for k, v in summary_of(run).items()
                if k not in hidden} | {'attached': True}
    if run is not None:
        return rejection('This server process already serves the run at ' + str(run.root))
    try:
        run = Run(pdf, out, reviewers, arm, literature_cutoff or None,
                  STATE['plugin_root'], title or None, list(tracks) if tracks else None,
                  reviewer_models=settled.get('reviewers'),
                  specialist_model=settled.get('specialist'),
                  editor_model=settled.get('editor'),
                  recorder_model=settled.get('recorder'))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return rejection(str(exc))
    STATE['run'] = run
    return {**summary_of(run), 'attached': False, 'recorder_key': run.recorder_key,
            'claim_codes': [{'slot': slot, 'claim_code': code}
                            for slot, code in run.claim_codes.items()],
            'identity': ('Give each agent only its own claim_code, in its own prompt. A code '
                         'works once. recorder_key is yours: it reissues a code for a retry and '
                         'marks the run blocked.')}


def summary_of(run):
    return {
        'run': str(run.root),
        'paper_id': run.paper['id'],
        'title': run.paper.get('title'),
        'arm': run.arm,
        'arm_summary': run.plan['summary'],
        'reviewers': run.reviewers,
        'status': run.status,
        'statistics': run.paper.get('statistics'),
        'slots': list(run.required) + ['recorder'],
        # The model each slot is to run, and `spawn` — the value to pass as
        # that agent's model override. They differ only in form: the override
        # is the family alias, and `model` is the version written down.
        'models': [{'slot': slot, 'model': model,
                    'spawn': model_plan.spawn_name(model)}
                   for slot, model in run.models.items()],
        'outline': run.outline(),
        'literature_cutoff': run.cutoff,
        'finding_schema_version': FINDING_SCHEMA_VERSION,
    }


def claim_agent(slot: str, claim_code: str = '') -> dict:
    """Take your slot with the claim code in your prompt, and get your agent token.

    One code per slot, minted at ingest and good for one claim. A slot someone
    else already holds is not claimable, and a code for another slot is not
    yours: both are refused, so a track is an identity and not a string.
    """
    run = current()
    try:
        agent = run.claim(slot, claim_code)
    except (ValueError, KeyError) as exc:
        return rejection(str(exc))
    run.write_progress()
    guidance = ''
    if agent.track in ('core', 'field'):
        try:
            profile = domain_route(run.paper, limit=2 if agent.track == 'core' else 1)
            guidance = domain_context(profile) if profile['selected'] else ''
        except (ValueError, KeyError) as exc:  # a pack that does not fit this paper
            guidance = 'Domain routing unavailable: ' + str(exc)
    return {
        'agent_token': agent.token,
        'slot': agent.slot,
        'role': agent.role,
        'track': agent.track,
        'attempt': agent.attempt,
        'tool_call_budget': agent.call_budget,
        'allowed_tools': list(agent.allowed),
        'paper_id': run.paper['id'],
        'title': run.paper.get('title'),
        'statistics': run.paper.get('statistics'),
        'outline': run.outline(),
        'domain_guidance': guidance,
        'finding_schema_version': FINDING_SCHEMA_VERSION,
        'severities': list(SEVERITIES),
        'kinds': list(KINDS),
        'reminder': ('Your track is set by this token. Quote exactly from the block you cite: an '
                     'unanchored quote is quarantined and the finding is lost.'),
    }


def reissue_claim_code(recorder_key: str, slot: str) -> dict:
    """Recorder only: a fresh claim code for one slot, to retry a stage that failed.

    Reissuing drops the token of whoever holds the slot, so the stuck attempt
    cannot keep filing findings beside its replacement.
    """
    run = current()
    if not secrets.compare_digest(str(recorder_key or ''), run.recorder_key):
        return rejection('A claim code is reissued by the recorder, with the run key ingest '
                         'returned.')
    try:
        code = run.reissue(slot)
    except (ValueError, KeyError) as exc:
        return rejection(str(exc))
    run.write_progress()
    return {'slot': slot, 'claim_code': code, 'attempt_next': run.agents[slot].attempt + 1
            if slot in run.agents else 1}


def _call(token, name, args):
    run = current()
    try:
        agent = run.agent_for(token)
    except ValueError as exc:
        return rejection(str(exc))
    if name not in agent.allowed:
        return rejection(f'The {agent.slot} agent may not call {name}. Allowed: '
                         + ', '.join(agent.allowed))
    if run.calls >= RUN_CALL_BUDGET:
        return rejection('This run has used its whole tool allowance. Finish from existing '
                         'evidence.', budget_exhausted=True)
    if agent.calls >= agent.call_budget:
        return rejection('Tool allowance exhausted. Finish from existing evidence.',
                         budget_exhausted=True)
    agent.calls += 1
    run.slot_calls[agent.slot] = agent.calls
    run.calls += 1
    result = agent.investigation.call(name, args)
    if name == 'calculate' and 'receipt' in result:
        agent.receipts[result['receipt']] = {'expression': result.get('expression'),
                                             'result': result.get('result')}
    result['calls_left'] = agent.call_budget - agent.calls
    if 'image' in result:
        return _image(result, agent, name)
    return result


def _image(result, agent, name):
    data = result.pop('image')
    views = agent.out / 'views'
    views.mkdir(parents=True, exist_ok=True)
    path = views / (digest(data)[:12] + '.png')
    path.write_bytes(base64.b64decode(data))
    try:
        from mcp.server.fastmcp import Image
    except ImportError:
        return {**result, 'image_unavailable': 'This server has no image transport.'}
    return [Image(path=str(path)), json.dumps({**result, 'tool': name}, ensure_ascii=False)]


def read_blocks(agent_token: str, ids: list) -> dict:
    """Read source blocks by stable ID. At most twelve in one call."""
    return _call(agent_token, 'read_blocks', {'ids': list(ids)})


def search(agent_token: str, query: str) -> dict:
    """Search one short exact contiguous phrase in the manuscript and supplements."""
    return _call(agent_token, 'search', {'query': query})


def calculate(agent_token: str, expression: str) -> dict:
    """Bounded arithmetic on reported values. The receipt it returns is what makes a
    finding ``computed``."""
    return _call(agent_token, 'calculate', {'expression': expression})


def save_note(agent_token: str, text: str, evidence_blocks: list) -> dict:
    """Save a source-linked hypothesis or a check you discarded."""
    return _call(agent_token, 'save_note',
                 {'text': text, 'evidence_blocks': list(evidence_blocks)})


def view_page(agent_token: str, page: int) -> dict:
    """Inspect one original PDF page, including its figures."""
    return _call(agent_token, 'view_page', {'page': page})


def view_supplement(agent_token: str, id: str, page: int = 1) -> dict:
    """Inspect one supplement page or image preview. IDs are in the asset inventory."""
    return _call(agent_token, 'view_supplement', {'id': id, 'page': page})


def check_citation(agent_token: str, reference_id: str) -> dict:
    """Resolve an extracted reference DOI through Crossref. Metadata only."""
    return _call(agent_token, 'check_citation', {'reference_id': reference_id})


def search_literature(agent_token: str, query: str, block_id: str) -> dict:
    """Search one Europe PMC page for a manuscript question. Not an exhaustive review."""
    return _call(agent_token, 'search_literature', {'query': query, 'block_id': block_id})


def resolve_identifier(agent_token: str, kind: str, identifier: str, block_id: str) -> dict:
    """Resolve one exact DOI, PMID, PMCID, NCT or OSF identifier."""
    return _call(agent_token, 'resolve_identifier',
                 {'kind': kind, 'identifier': identifier, 'block_id': block_id})


def lookup_source(agent_token: str, provider: str, identifier: str, block_id: str) -> dict:
    """Retrieve one exact public record linked to a manuscript block."""
    return _call(agent_token, 'lookup_source',
                 {'provider': provider, 'identifier': identifier, 'block_id': block_id})


def inspect_results(agent_token: str, block_id: str = '') -> dict:
    """Inspect linked claims, tables and figure captions. Empty block_id is an overview."""
    return _call(agent_token, 'inspect_results', {'block_id': block_id})


def record_comparison(agent_token: str, observations: list) -> dict:
    """Record two exact textual quantities with their model, population, metric and units."""
    return _call(agent_token, 'record_comparison', {'observations': list(observations)})


def validate_synthesis(agent_token: str, draft_json: str) -> dict:
    """Local checks on a synthesis draft before submission. No scientific endorsement."""
    return rejection('validate_synthesis is not part of a plugin run: submit_editor runs the '
                     'editor checks and returns what to fix.')


def submit_finding(agent_token: str, title: str, block_id: str, quote: str, comment: str,
                   severity: str, kind: str = 'question', replacement: str = '',
                   evidence_blocks: list = None, counterevidence: str = '',
                   source_urls: list = None, escalate: bool = False,
                   calculate_receipt: str = '', corroborating_block_id: str = '',
                   corroborating_quote: str = '') -> dict:
    """Store one finding. The guard anchors your quote; the server sets track and basis.

    A quote that is absent or appears twice in the block you name is quarantined
    with its reason and nothing is stored, so fix the quote and submit again.
    """
    run = current()
    try:
        agent = run.agent_for(agent_token)
    except ValueError as exc:
        return rejection(str(exc))
    if agent.role not in ('core', 'specialist'):
        return rejection('Only a reviewer or a specialist submits findings.')
    if severity not in SEVERITIES:
        return rejection('severity is one of ' + ', '.join(SEVERITIES))
    # Nothing arrives after the ledger has moved on. A finding stored after the
    # stage closed, or after the editor ruled, was written to findings.jsonl and
    # then silently left out of the package: refusing it says so to the agent.
    if run.status != 'running':
        return rejection('This run is ' + run.status + '. Nothing was stored.')
    if agent.report is not None:
        return rejection('You closed this stage with submit_report. Nothing was stored: a finding '
                         'after the stage is not part of the review.')
    editor = run.agents.get('editor')
    if editor is not None and editor.editor_package:
        return rejection('The pooled review has already been ruled on. Nothing was stored.')
    if len(list(evidence_blocks or [])) > MAX_EVIDENCE_BLOCKS:
        return rejection(f'Cite at most {MAX_EVIDENCE_BLOCKS} evidence blocks: name the blocks the '
                         'finding rests on, not every block that holds a number.')
    corroborating = None
    if corroborating_block_id and corroborating_quote:
        if run.anchor(corroborating_block_id, corroborating_quote) is None:
            return rejection('The corroborating quote is absent or ambiguous in '
                             + corroborating_block_id + '. Nothing was stored.')
        corroborating = {'block_id': corroborating_block_id, 'quote': corroborating_quote}
    raw = {'title': title, 'block_id': block_id, 'quote': quote, 'comment': comment,
           'severity': severity, 'kind': kind if kind in KINDS else 'question',
           'replacement': replacement, 'evidence_blocks': list(evidence_blocks or []),
           'counterevidence': counterevidence, 'source_urls': list(source_urls or [])}
    stored, reason = run.store_finding(agent, raw, escalate, calculate_receipt, corroborating)
    run.write_progress()
    if stored is None:
        return rejection(reason, quarantined=True)
    return {'stored': True, 'finding_uid': stored['finding_uid'], 'id': stored['id'],
            'track': stored['track'], 'basis': stored['basis'],
            'basis_reason': stored['basis_reason'], 'escalate': stored['escalate'],
            'kind': stored['kind'], 'edit_withheld': stored.get('edit_withheld'),
            'receipt': digest(json.dumps(stored, sort_keys=True))[:16],
            'findings_stored_by_you': sum(f['slot'] == agent.slot for f in run.pooled()),
            'submissions_by_you': sum(f['slot'] == agent.slot for f in run.findings)}


def submit_report(agent_token: str, summary: str, limitations: list = None,
                  investigation_summary: str = '', discarded_hypotheses: list = None,
                  none_found: bool = False, model: str = '',
                  report_markdown: str = '') -> dict:
    """Close your stage. Required: the ledger counts a stage as done only after this.

    Pass ``none_found`` with an empty-handed summary rather than inventing a
    finding. A specialist's ``report_markdown`` is written to ``audit_<track>.md``.
    """
    run = current()
    try:
        agent = run.agent_for(agent_token)
    except ValueError as exc:
        return rejection(str(exc))
    mine = [f for f in run.pooled() if f['slot'] == agent.slot]
    if none_found and mine:
        return rejection(f'You stored {len(mine)} finding(s); none_found contradicts the ledger.')
    agent.report = {'slot': agent.slot, 'role': agent.role, 'track': agent.track,
                    'attempt': agent.attempt, 'summary': summary,
                    'limitations': list(limitations or []),
                    'investigation_summary': investigation_summary,
                    'discarded_hypotheses': list(discarded_hypotheses or []),
                    'none_found': bool(none_found and not mine), 'model': model or None,
                    'tool_calls': agent.calls, 'findings': len(mine), 'closed': now()}
    write(agent.out / 'report.json', {**agent.report, 'finding_uids': [f['finding_uid']
                                                                       for f in mine]})
    if agent.role == 'specialist':
        body = report_markdown.strip() or _audit_markdown(agent, mine)
        (run.review / f'audit_{agent.track}.md').write_text(
            f'# {agent.track} audit\n\n{body}\n', encoding='utf-8')
    run.write_progress()
    return {'closed': True, 'slot': agent.slot, 'findings': len(mine),
            'none_found': agent.report['none_found'], 'ledger': run.visible_ledger(agent)}


def _audit_markdown(agent, findings):
    lines = [agent.report['summary'] or '(no summary)', '']
    for finding in findings:
        lines.append(f"## {finding['title']} ({finding['severity']}, {finding['basis']}"
                     + (', escalated' if finding['escalate'] else '') + ')')
        lines.append(f"Block {finding['block_id']}: “{finding['quote']}”")
        lines.append('')
        lines.append(finding['comment'])
        lines.append('')
    return '\n'.join(lines)


def list_findings(agent_token: str, track: str = '', include_comment: bool = True) -> dict:
    """List the findings you are allowed to see. A reviewer sees only its own."""
    run = current()
    try:
        agent = run.agent_for(agent_token)
    except ValueError as exc:
        return rejection(str(exc))
    rows = [f for f in run.pooled() if f['slot'] in agent.sees or agent.role in ('editor',
                                                                                 'recorder')]
    if track:
        rows = [f for f in rows if f['track'] == track]
    if agent.role in ('editor', 'recorder'):
        # Randomised once per run, so no reviewer or track is systematically read first.
        rows = sorted(rows, key=lambda f: digest(run.salt + f['finding_uid']))
    fields = ('finding_uid', 'id', 'track', 'slot', 'title', 'severity', 'kind', 'basis',
              'basis_reason', 'escalate', 'block_id', 'quote', 'evidence_blocks',
              'counterevidence', 'corroborating', 'calculate_receipt')
    out = []
    for finding in rows:
        row = {key: finding.get(key) for key in fields}
        if include_comment:
            row['comment'] = finding.get('comment')
        out.append(row)
    return {'count': len(out), 'findings': out, 'order': 'randomised per run',
            'reports': [a.report for a in run.agents.values() if a.report
                        and (a.slot in agent.sees or agent.role in ('editor', 'recorder'))],
            'ledger': run.visible_ledger(agent)}


def submit_editor(agent_token: str, summary: str, decisions: list,
                  significance: str, strength_of_evidence: str, verdict_reasoning: str,
                  escalation_rulings: list = None, limitations: list = None) -> dict:
    """Submit the pooled review. Every finding in the ledger needs exactly one decision.

    ``decisions`` is a list of ``{finding_uid, placement, reason}`` where placement
    is ``body``, ``appendix`` or ``duplicate`` (then ``duplicate_of`` names the
    finding it repeats). A ``computed`` finding kept out of the body needs a
    ``rebuttal_quote`` that anchors exactly in the block it cites. Nothing is
    deleted: the appendix carries everything the body does not.
    """
    run = current()
    try:
        agent = run.agent_for(agent_token)
    except ValueError as exc:
        return rejection(str(exc))
    if agent.role != 'editor':
        return rejection('Only the editor submits the pooled review.')
    # One row per uid, the latest submission of each: what the editor was shown.
    by_uid = {f['finding_uid']: f for f in run.pooled()}
    problems, seen, body = [], {}, []
    for index, decision in enumerate(decisions or [], 1):
        uid = str(decision.get('finding_uid', ''))
        placement = decision.get('placement')
        if uid not in by_uid:
            problems.append(f'decision {index}: unknown finding_uid {uid or "(missing)"}')
            continue
        if uid in seen:
            problems.append(f'decision {index}: {uid} already decided')
            continue
        if placement not in PLACEMENTS:
            problems.append(f'decision {index}: placement is one of ' + ', '.join(PLACEMENTS))
            continue
        if not str(decision.get('reason', '')).strip():
            problems.append(f'decision {index}: every placement needs a logged reason')
            continue
        if placement == 'duplicate':
            other = str(decision.get('duplicate_of', ''))
            if other not in by_uid or other == uid:
                problems.append(f'decision {index}: duplicate_of must name another finding')
                continue
        finding = by_uid[uid]
        if placement != 'body' and finding['basis'] == 'computed':
            quote = str(decision.get('rebuttal_quote', ''))
            block = str(decision.get('rebuttal_block_id', finding['block_id']))
            if run.anchor(block, quote) is None:
                problems.append(f'decision {index}: a computed finding leaves the body only with '
                                'a rebuttal_quote that anchors exactly in the block it cites')
                continue
        seen[uid] = {**decision, 'finding_uid': uid, 'placement': placement}
        if placement == 'body':
            body.append(uid)
    missing = [uid for uid in by_uid if uid not in seen]
    if missing:
        problems.append(f'{len(missing)} finding(s) have no decision: ' + ', '.join(missing[:10]))
    if len(body) > BODY_LIMIT:
        problems.append(f'the body holds at most about {BODY_LIMIT} findings; you placed '
                        f'{len(body)}')
    escalated = [uid for uid, f in by_uid.items() if f['escalate']]
    rulings = {str(r.get('finding_uid', '')): r for r in (escalation_rulings or [])}
    for uid in escalated:
        ruling = rulings.get(uid)
        if not ruling or not str(ruling.get('ruling', '')).strip():
            problems.append('escalated finding ' + uid + ' needs a ruling by name: '
                            + by_uid[uid]['title'][:60])
    for axis, value in (('significance', significance),
                        ('strength_of_evidence', strength_of_evidence)):
        if not str(value).strip():
            problems.append(axis + ' is required')
        elif re.fullmatch(r'[\d.\s/]+', str(value)):
            problems.append(axis + ' is a judgement in words, not a score')
    if problems:
        return rejection('The pooled review was not accepted. Fix these and submit again.',
                         problems=problems, findings_in_ledger=len(by_uid),
                         escalated=escalated)
    package = {
        'summary': summary,
        'limitations': list(limitations or []),
        'decisions': list(seen.values()),
        'escalation_rulings': [rulings[uid] for uid in escalated],
        'verdict': {'significance': significance, 'strength_of_evidence': strength_of_evidence,
                    'reasoning': verdict_reasoning, 'basis': 'judgement',
                    'limitation': 'Advisory two-axis judgement, not a score and not a decision.'},
        'body': body,
        'accepted': now(),
    }
    agent.editor_package = package
    write(agent.out / 'editor-decisions.json', package)
    run.write_progress()
    return {'accepted': True, 'body': len(body), 'appendix': len(by_uid) - len(body),
            'escalated_ruled': len(escalated), 'next': 'The recorder calls write_review.'}


def mark_blocked(recorder_key: str, reason: str) -> dict:
    """Recorder only: mark the run blocked — a rate limit, or a stage that failed twice.

    A finished review is never flipped to blocked, and the run key keeps the
    decision with the recorder: a reviewer that could call this could end a run
    whose findings it did not like.
    """
    run = current()
    if not secrets.compare_digest(str(recorder_key or ''), run.recorder_key):
        return rejection('The run is marked blocked by the recorder, with the run key ingest '
                         'returned.')
    if run.status in COMPLETE_STATUSES:
        return rejection('This run is ' + run.status + ' and its review is written. A finished '
                         'run is not marked blocked.', status=run.status)
    run.status = 'blocked'
    run.error = reason
    run.write_progress()
    run.write_protocol()
    run.write_manifest()
    return {'status': run.status, 'error': reason}


def run_status(recorder_key: str = '') -> dict:
    """How the run stands, without a token. The stage ledger needs the run key.

    The counts are safe for anyone in the run to read; the ledger names every
    slot, so it goes only to the holder of the recorder's key.
    """
    run = current()
    ledger = run.ledger()
    status = {'status': run.status, 'error': run.error, 'arm': run.arm,
              'reviewers': run.reviewers, 'stages_required': len(ledger['required']),
              'stages_reported': len(ledger['reported']),
              'findings': len(run.pooled()), 'quarantined': len(run.quarantined),
              'tool_calls': run.calls, 'run': str(run.root)}
    if recorder_key and secrets.compare_digest(str(recorder_key), run.recorder_key):
        status['ledger'] = ledger
    return status


def write_review(agent_token: str) -> dict:
    """Write the run files. Refuses while a required stage has not reported."""
    run = current()
    try:
        agent = run.agent_for(agent_token)
    except ValueError as exc:
        return rejection(str(exc))
    if agent.role != 'recorder':
        return rejection('The recorder writes the review.')
    ledger = run.ledger()
    if ledger['missing']:
        run.status = 'blocked'
        run.error = 'Stages that never reported: ' + ', '.join(ledger['missing'])
        run.write_progress()
        run.write_protocol()
        run.write_manifest()
        return rejection(run.error, status=run.status, ledger=ledger)
    if run.plan['editor'] and not ledger['editor_accepted']:
        run.status = 'blocked'
        run.error = 'The editor never submitted an accepted pooled review.'
        run.write_progress()
        run.write_protocol()
        run.write_manifest()
        return rejection(run.error, status=run.status, ledger=ledger)
    package = _editor_package(run)
    write(run.review / 'editor-package.json', package)
    for index in range(1, run.reviewers + 1):
        slot = f'core-{index}'
        reviewer = run.agents.get(slot)
        if reviewer is None:
            continue
        write(run.review / f'reviewer-{index}' / 'editor-package.json', {
            'summary': (reviewer.report or {}).get('summary', ''),
            'findings': [_public(f) for f in run.pooled() if f['slot'] == slot],
            'limitations': (reviewer.report or {}).get('limitations', []),
            'reviewer_count': 1,
            'workflow_version': WORKFLOW_VERSION,
            'review_guard_status': run.guard_status(slot),
        })
    # The appendix is part of the review: a run that delivered one appendix
    # finding did not complete without findings.
    delivered = len(package['findings']) + len(package['appendix'])
    run.status = 'complete' if delivered else 'complete-without-findings'
    run.error = None
    run.write_progress()
    run.write_protocol()
    run.write_manifest()
    report = _write_reports(run)
    run.write_progress()
    return {'status': run.status, 'run': str(run.root),
            'review': str(run.review / 'REVIEW.md'), 'report': report,
            'body_findings': len(package['findings']),
            'appendix_findings': len(package['appendix']),
            'findings_by_track': package['findings_by_track'],
            'escalated': package['escalated_count'], 'ledger': ledger}


def _public(finding):
    """One finding as a consumer reads it: the 0.8 fields plus the new ones."""
    keys = ('id', 'finding_uid', 'review_id', 'slot', 'track', 'role', 'title', 'block_id',
            'quote', 'kind', 'category', 'severity', 'comment', 'replacement',
            'evidence_blocks', 'counterevidence', 'source_urls', 'basis', 'basis_reason',
            'escalate', 'corroborating', 'calculate_receipt', 'start', 'end', 'block_sha256',
            'placement', 'issue_status', 'edit_decision', 'edit_withheld', 'schema_version')
    return {key: finding.get(key) for key in keys if key in finding}


def _editor_package(run):
    editor = run.agents.get('editor')
    package = editor.editor_package if editor and editor.editor_package else None
    pooled = run.pooled()
    by_uid = {f['finding_uid']: f for f in pooled}
    decisions = package['decisions'] if package else [
        {'finding_uid': uid, 'placement': 'body', 'reason': 'No editor stage in this arm.'}
        for uid in by_uid]
    placement = {d['finding_uid']: d for d in decisions}
    body_uids = [d['finding_uid'] for d in decisions if d['placement'] == 'body']
    counts = {}
    for finding in pooled:
        counts[finding['track']] = counts.get(finding['track'], 0) + 1
    raised_by = {}
    for decision in decisions:
        target = decision.get('duplicate_of') or decision['finding_uid']
        raised_by.setdefault(target, []).append(decision['finding_uid'])
    reports = [a.report for a in run.agents.values() if a.report]
    return {
        'summary': (package or {}).get('summary') or _fallback_summary(run),
        'findings': [{**_public(by_uid[uid]),
                      'editor_reason': placement[uid].get('reason'),
                      'reviewers_raising': len(raised_by.get(uid, [uid])),
                      'duplicate_uids': [u for u in raised_by.get(uid, []) if u != uid]}
                     for uid in body_uids],
        'appendix': [{**_public(by_uid[d['finding_uid']]),
                      'editor_reason': d.get('reason'), 'editor_placement': d['placement'],
                      'duplicate_of': d.get('duplicate_of'),
                      'rebuttal_quote': d.get('rebuttal_quote')}
                     for d in decisions if d['placement'] != 'body'],
        'limitations': (package or {}).get('limitations', []),
        'decisions': decisions,
        'verdict': (package or {}).get('verdict'),
        'escalation_rulings': (package or {}).get('escalation_rulings', []),
        'escalated_count': sum(1 for f in pooled if f['escalate']),
        'findings_by_track': counts,
        'reviewer_count': run.reviewers,
        # `model` is what the agent reported running as; `assigned_model` is
        # what the run asked that slot to run. A published review names the
        # second and can show the first disagreeing with it.
        'reviewer_provenance': [{'slot': r['slot'], 'track': r['track'], 'model': r['model'],
                                 'assigned_model': run.models.get(r['slot']),
                                 'attempt': r['attempt'], 'tool_calls': r['tool_calls'],
                                 'findings': r['findings'], 'none_found': r['none_found']}
                                for r in reports],
        'arm': run.arm,
        'synthesis_policy': 'editor' if package else 'none',
        'workflow_version': WORKFLOW_VERSION,
        'finding_schema_version': FINDING_SCHEMA_VERSION,
        'review_guard_status': run.guard_status(),
        'findings_delivered': len(pooled),
        'findings_submitted': len(run.findings),
        'quarantined_findings_count': len(run.quarantined),
        'quarantined_findings': run.quarantined,
        'stages': run.ledger(),
    }


def _fallback_summary(run):
    reports = [a.report for a in run.agents.values() if a.report and a.role == 'core']
    return ' '.join(r['summary'] for r in reports if r.get('summary')) or 'No summary submitted.'


def _write_reports(run):
    """REVIEW.md through the engine's own renderer, with a plain fallback if it refuses."""
    try:
        from ..engine.run_report import write_reports
        write_reports(run.review, run.workspace, run.review)
        return {'renderer': 'engine.run_report'}
    except Exception as exc:  # noqa: BLE001 - a report must never lose a finished review
        package = read(run.review / 'editor-package.json')
        lines = [f"# Review of {run.paper.get('title')}", '',
                 f"Run {run.root.name}, arm {run.arm}, {run.reviewers} core reviewer(s), "
                 f"status {run.status}.", '', '## Summary', '', package['summary'], '',
                 '## Findings', '']
        for finding in package['findings']:
            lines += [f"### {finding['title']} ({finding['severity']}, {finding['track']}, "
                      f"{finding['basis']})", '',
                      f"Block {finding['block_id']}: “{finding['quote']}”", '',
                      finding.get('comment') or '', '']
        if package['appendix']:
            lines += ['## Appendix', '']
            for finding in package['appendix']:
                lines += [f"### {finding['title']} ({finding['severity']}, {finding['track']})",
                          '', f"Placed in the appendix: {finding.get('editor_reason')}", '',
                          finding.get('comment') or '', '']
        (run.review / 'REVIEW.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return {'renderer': 'plugin fallback', 'engine_error': str(exc)}


TOOLS = (ingest, claim_agent, reissue_claim_code, read_blocks, search, calculate, save_note,
         view_page, view_supplement, check_citation, search_literature, resolve_identifier,
         lookup_source, inspect_results, record_comparison, validate_synthesis, submit_finding,
         submit_report, list_findings, submit_editor, mark_blocked, run_status, write_review)


def create_server(plugin_root=None):
    from mcp.server.fastmcp import FastMCP

    STATE['plugin_root'] = plugin_root or os.environ.get('CLAUDE_PLUGIN_ROOT')
    server = FastMCP('peerreview')
    for tool in TOOLS:
        server.tool()(tool)
    return server


def main(argv=None):
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    # One `args` array in .mcp.json serves both launchers: the console script
    # `peerreview-plugin-server`, which an installed wheel puts on PATH, and
    # `<python> -m peerreviewagents.plugin.server`, which is what `peerreview
    # review` points the session at so the run uses its own interpreter. The
    # console script is handed the interpreter switch as an argument; drop it
    # rather than make the plugin config pick one launcher.
    if argv[:1] == ['-m'] and len(argv) > 1 and argv[1].startswith('peerreviewagents.'):
        argv = argv[2:]
    plugin_root = None
    if '--plugin-root' in argv:
        index = argv.index('--plugin-root')
        plugin_root = argv[index + 1] if index + 1 < len(argv) else None
    if plugin_root in ('', '${CLAUDE_PLUGIN_ROOT}'):
        plugin_root = None
    create_server(plugin_root).run()


if __name__ == '__main__':
    main()
