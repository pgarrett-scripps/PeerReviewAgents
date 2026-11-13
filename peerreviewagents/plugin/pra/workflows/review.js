export const meta = {
  name: 'review',
  description: 'Two-track peer review: ingest, N core reviewers and six specialists in parallel, editor, write the run',
  whenToUse: 'Reviewing one manuscript PDF with PeerReviewAgents 0.9. Pass {pdf, out, reviewers, arm}, and {reviewer_models, specialist_model, editor_model, recorder_model, citation_worker_model} to choose the models.',
  phases: [
    { title: 'Ingest', detail: 'import the PDF and fix the plan for this arm' },
    { title: 'Citations', detail: 'resolve every reference, then judge each in-text use against the source' },
    { title: 'Review', detail: 'core reviewers and specialists, in parallel, each isolated' },
    { title: 'Revision', detail: 'single-revision arm only: the reviewer revises its own findings' },
    { title: 'Editor', detail: 'pool the findings, decide body against appendix, rule on escalations' },
    { title: 'Record', detail: 'write the run files, or mark the run blocked' },
  ],
}

// The stages are a script, not a prompt, so a stage that never ran is a
// failure rather than something the model forgot. The server owns identity,
// the guard, the ledger and the files; this script owns only the order, the
// fan-out and the one retry. It has no filesystem access, so every check here
// is made through what the server returns.

const run = args || {}
if (!run.pdf || !run.out) {
  throw new Error('review: args must include {"pdf": "<path to PDF>", "out": "<run folder>"}')
}
const REVIEWERS = run.reviewers || 3
const ARM = run.arm || 'full-two-track'

const OPENED = {
  type: 'object',
  properties: {
    paper_id: { type: 'string' },
    title: { type: 'string' },
    arm: { type: 'string' },
    block_count: { type: 'integer', description: 'how many blocks the outline holds' },
    slots: {
      type: 'array', items: { type: 'string' },
      description: 'the slot list ingest returned, verbatim',
    },
    recorder_key: { type: 'string', description: 'the recorder_key ingest returned, verbatim' },
    claim_codes: {
      type: 'array',
      items: {
        type: 'object',
        properties: { slot: { type: 'string' }, claim_code: { type: 'string' } },
        required: ['slot', 'claim_code'],
      },
      description: 'every {slot, claim_code} pair ingest returned, verbatim',
    },
    models: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          slot: { type: 'string' },
          model: { type: 'string' },
          spawn: { type: 'string' },
        },
        required: ['slot', 'spawn'],
      },
      description: 'every {slot, model, spawn} entry ingest returned, verbatim, one per slot',
    },
    citation_workers: {
      type: 'array', items: { type: 'string' },
      description: 'the citation_workers list ingest returned, verbatim; [] where the arm has none',
    },
    // How long the reference list is, which is what the lookup stage below is
    // sized from. Without it the stage would run on a constant and stop
    // part-way through a long list.
    citations: {
      type: 'object',
      properties: {
        references: { type: 'integer', description: "ingest's citations.references" },
        uses: { type: 'integer', description: "ingest's citations.uses" },
      },
      description: 'the reference and in-text-use counts from citations; omit it where the arm '
                   + 'returned citations: null',
    },
    citation_worker_allowance: {
      type: 'integer',
      description: 'the citation_worker_allowance ingest returned; omit it where it was null',
    },
  },
  // `models` is required because the plan has to arrive for the run to be the
  // run the server recorded. This script has no tool access and no filesystem
  // access, so the plan can only reach it as text an agent relays, and a relay
  // that drops it would otherwise complete: every slot would fall back to the
  // model its agent file pins while `protocol.json` recorded the plan. The
  // relay cannot be made trustworthy from in here, so it is made loud instead
  // — and `read_run` compares what ran against the plan afterwards, which is
  // the backstop for a relay that swaps two models rather than losing them.
  required: ['paper_id', 'arm', 'slots', 'recorder_key', 'claim_codes', 'models'],
}

// A retry needs a code the first attempt has not spent.
const REISSUED = {
  type: 'object',
  properties: {
    slot: { type: 'string' },
    claim_code: { type: 'string', description: 'the fresh code reissue_claim_code returned' },
  },
  required: ['claim_code'],
}

const STAGE = {
  type: 'object',
  properties: {
    closed: { type: 'boolean', description: 'true only if submit_report returned closed' },
    findings_stored: { type: 'integer' },
    none_found: { type: 'boolean' },
    model: { type: 'string', description: 'the model you are running as' },
    summary: { type: 'string', description: 'one or two sentences' },
  },
  required: ['closed', 'findings_stored', 'summary'],
}

const EDITED = {
  type: 'object',
  properties: {
    accepted: { type: 'boolean', description: 'true only if submit_editor returned accepted' },
    body: { type: 'integer', description: 'findings placed in the body' },
    escalated_ruled: { type: 'integer' },
    attempts: { type: 'integer', description: 'how many times you called submit_editor' },
    summary: { type: 'string' },
  },
  required: ['accepted', 'body', 'summary'],
}

// Stage 2 of the citation ledger: the server's own lookups, no model involved.
// The recorder calls the tool until nothing is owed a lookup, or until what is
// owed is waiting out the retry wait, so what comes back is how far it got, not
// what it intended.
const RESOLVED = {
  type: 'object',
  properties: {
    remaining: { type: 'integer', description: 'references still owed a lookup, from the last call' },
    retry_after: { type: 'integer', description: 'seconds until the earliest owed row is due again, from the last call' },
    references: { type: 'integer' },
    resolved: { type: 'integer' },
    lookups: { type: 'integer', description: 'lookups_total from the last call' },
    calls: { type: 'integer', description: 'how many times you called resolve_citations' },
    refusal: { type: 'string', description: 'the request_rejected text, if it refused' },
  },
  required: ['remaining'],
}

// Stage 3: one worker's share of the in-text uses.
const JUDGED = {
  type: 'object',
  properties: {
    judged: { type: 'integer', description: 'verdicts submit_citation_verdict stored for you' },
    batches: { type: 'integer', description: 'how many batches you claimed' },
    refused: { type: 'integer', description: 'verdicts the server refused' },
    left: { type: 'integer', description: 'uses_unjudged from your last call' },
    model: { type: 'string', description: 'the model you are running as' },
  },
  required: ['judged'],
}

const RECORDED = {
  type: 'object',
  properties: {
    status: { type: 'string', description: 'the status write_review returned' },
    body_findings: { type: 'integer' },
    appendix_findings: { type: 'integer' },
    escalated: { type: 'integer' },
    refusal: { type: 'string', description: 'the request_rejected text, if it refused' },
  },
  required: ['status'],
}

// One line per lane, and the lines are written to be disjoint: the brief a
// specialist is spawned with has to say what its lane is without describing
// another's. The agent files carry the boundaries in full.
const SPECIALIST_BRIEF = {
  'stats': 'the inference: whether the test matches the design, what n really is, multiplicity, effect size and precision, and what the result licenses the paper to say',
  'figures-tables': 'what each panel and table actually shows, against its caption and the sentences that cite it, and whether a reader can read it',
  'methods-reporting': 'what is absent: the parameter, rule, declaration or working link a reader would need to repeat this work',
  'citations': 'the citation ledger the server filled: every reference it looked up and every verdict a worker left, re-checking each problem row yourself before any of it becomes a finding',
  'numbers': 'every quantity the paper prints: whether the same value agrees wherever it appears, whether the n survives the paper, and whether the arithmetic between printed values holds',
  'field': 'what this kind of study is required to do that a generalist would not know to ask for, working from the domain pack the server routes to you',
}

function label(slot, attempt) {
  return attempt > 1 ? slot + ' (retry)' : slot
}

// The claim code is the agent's identity: it is minted per slot at ingest,
// returned only to the agent that opened the run, and spent on first use. The
// script is the only thing that knows which agent gets which code, so an agent
// cannot claim a slot that was not assigned to it even by guessing the name.
let codes = {}
let recorderKey = ''
const codeFor = slot => codes[slot] || ''

// Which model each slot runs is the server's decision, taken at ingest from
// the flags, the environment and its defaults, and recorded in the run folder.
// This script only carries it: `spawn` is the override the agent is started
// with, and a slot the server said nothing about keeps the model its own agent
// file pins. Three core reviewers are three different models on purpose.
let spawns = {}
let assigned = {}
function options(slot, rest) {
  const spawn = spawns[slot]
  if (!spawn) return rest
  return Object.assign({}, rest, { model: spawn })
}

// A spent code cannot be reused, so a retry has to be issued a fresh one, and
// only the holder of the recorder key can ask for that.
async function reissue(slot, phaseName) {
  const fresh = await agent(
    'A stage of this run has to be retried, so its slot needs a new claim code. Call'
    + ' reissue_claim_code with recorder_key="' + recorderKey + '" and slot="' + slot
    + '". Report the claim_code it returned, verbatim, and nothing else.',
    options('recorder', {
      label: 'reissue ' + slot, phase: phaseName, agentType: 'pra:recorder', schema: REISSUED,
    }),
  )
  if (fresh && fresh.claim_code) {
    codes[slot] = fresh.claim_code
    return fresh.claim_code
  }
  return ''
}

// One retry, then blocked: a stage that returns nothing, or returns without
// closing its slot in the ledger, is not a stage that ran. `promptFor` takes
// the claim code, because attempt 2 runs on a different one.
async function stage(slot, agentType, phaseName, promptFor, schema) {
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const code = attempt === 1 ? codeFor(slot) : await reissue(slot, phaseName)
    if (!code) {
      log(slot + ': no claim code, so the slot cannot be claimed; the run is blocked')
      return null
    }
    const result = await agent(promptFor(code), options(slot, {
      label: label(slot, attempt), phase: phaseName, agentType, schema,
    }))
    const ok = result && (schema === EDITED ? result.accepted : result.closed)
    if (ok) return Object.assign({ slot, attempts: attempt }, result)
    const what = result ? 'returned without closing its stage' : 'returned nothing'
    log(slot + ': ' + what + (attempt === 1 ? '; retrying once' : '; the run is blocked'))
  }
  return null
}

async function block(reason) {
  await agent(
    'A stage of this run failed twice. Call mark_blocked with recorder_key="' + recorderKey
    + '" and this reason, exactly as written: "' + reason + '".'
    + ' Then report the status it returned.',
    options('recorder', { label: 'mark blocked', phase: 'Record', agentType: 'pra:recorder' }),
  )
  throw new Error('review: ' + reason)
}

phase('Ingest')
const opened = await agent(
  'Open a review run. Call ingest exactly once with pdf="' + run.pdf + '", out="' + run.out
  + '", reviewers=' + REVIEWERS + ', arm="' + ARM + '"'
  + (run.literature_cutoff ? ', literature_cutoff="' + run.literature_cutoff + '"' : '')
  + (run.tracks ? ', tracks=' + JSON.stringify(run.tracks) : '')
  + (run.reviewer_models ? ', reviewer_models=' + JSON.stringify(run.reviewer_models) : '')
  + (run.specialist_model ? ', specialist_model="' + run.specialist_model + '"' : '')
  + (run.editor_model ? ', editor_model="' + run.editor_model + '"' : '')
  + (run.recorder_model ? ', recorder_model="' + run.recorder_model + '"' : '')
  + (run.citation_workers !== undefined && run.citation_workers !== null
    ? ', citation_workers=' + run.citation_workers : '')
  + (run.citation_worker_model ? ', citation_worker_model="' + run.citation_worker_model + '"' : '')
  + '. Report the paper id, the title, the block count, the slot list and the citation_workers'
  + ' list it returned, and — where the response holds them rather than null —'
  + ' citations.references, citations.uses and citation_worker_allowance, which are what the'
  + ' citation stages are sized from. Copy'
  + ' the recorder_key, every claim_code and every models entry out of the response verbatim:'
  + ' they are the only copies, and each stage is handed its own code and its own model'
  + ' from here.',
  { label: 'ingest', phase: 'Ingest', agentType: 'pra:recorder', schema: OPENED },
)
if (!opened || !opened.slots || opened.slots.length === 0) {
  throw new Error('review: ingest returned no plan, so no stage can be assigned')
}
if (!opened.recorder_key || !opened.claim_codes || opened.claim_codes.length === 0) {
  throw new Error('review: ingest returned no claim codes, so no agent can prove which slot it is')
}
recorderKey = opened.recorder_key
codes = {}
opened.claim_codes.forEach(entry => {
  if (entry && entry.slot) codes[entry.slot] = entry.claim_code
})
spawns = {}
assigned = {}
;(opened.models || []).forEach(entry => {
  if (!entry || !entry.slot) return
  if (entry.spawn) spawns[entry.slot] = entry.spawn
  if (entry.model) assigned[entry.slot] = entry.model
})

// The server's slot list is the plan. The arm decides it, so the script never
// has to hold a second copy of which stages a given arm requires.
const slots = opened.slots
// Every planned slot has to arrive with its model. A slot with no entry would
// run whatever its agent file pins while `protocol.json` records the plan, and
// the relay is the only way the plan reaches this script, so a relay that drops
// one stops the run rather than producing a record that disagrees with it.
// The citation workers are planned beside the stages and are not stages: they
// fill the ledger the citations specialist reads, and a worker that never runs
// leaves rows unjudged instead of holding the review open. The list arrives
// twice over — ingest returns it, and the model plan carries one entry per
// worker — so a relay that drops the list is caught by the plan.
const workerSlots = Object.keys(spawns).filter(slot => /^cite-[0-9]+$/.test(slot))
  .concat((opened.citation_workers || []).filter(slot => /^cite-[0-9]+$/.test(slot)))
  .filter((slot, index, all) => all.indexOf(slot) === index)
  .filter(slot => !!codes[slot])
const unplanned = slots.concat(workerSlots).filter(slot => !spawns[slot])
if (unplanned.length > 0) {
  await block('ingest returned no model for ' + unplanned.join(', ') + ', so '
    + (unplanned.length === 1 ? 'that slot' : 'those slots')
    + ' would run whatever the agent file pins while the run records the plan')
}
const coreSlots = slots.filter(s => /^core-[0-9]+$/.test(s))
const specSlots = slots.filter(s => s.indexOf('spec-') === 0)
const hasRevision = slots.indexOf('revision-1') >= 0
const hasEditor = slots.indexOf('editor') >= 0
log('arm ' + opened.arm + ': ' + coreSlots.length + ' core reviewer(s), '
  + specSlots.length + ' specialist(s), editor ' + (hasEditor ? 'yes' : 'no'))
if (coreSlots.length > 0) {
  log('models: ' + coreSlots.concat(hasEditor ? ['editor'] : [])
    // Every planned slot has a model by here, or the check above stopped the
    // run, so there is no agent-file case left to name: the spawn name is the
    // fallback only because it is the same entry's shorter spelling.
    .map(s => s + ' ' + (assigned[s] || spawns[s])).join(', '))
}

// -- the citation ledger ----------------------------------------------------
//
// Stage 1 happened inside ingest. Stage 2 is the server's own lookups, which
// cost no model call and are driven by the recorder. Stage 3 is the workers.
// None of it can block the review: an unfinished ledger is a smaller claim
// about the citations, which the specialist then reports as that.
//
// This lane starts before the review fan-out and only the citations specialist
// waits for it, so the lookups run while the core reviewers read the paper.
// How many references one call resolves. It is a batch size, not a sample: the
// call returns `remaining`, and the loop below runs until that is zero.
const LOOKUP_LIMIT = 20
// The most calls one recorder agent is asked for. `maxTurns` in
// agents/recorder.md is set above it, or the agent stops before the loop does.
const RESOLVE_CALL_CEILING = 26
// The reference count is the paper's, so the call count is too. A fixed twelve
// calls covered 220 references and stopped: a 300-reference review left 80
// unresolved, and the one number that said so was buried in the counts.
const referenceCount = (opened.citations && opened.citations.references) || 0
// A count the relay dropped asks for the whole allowance rather than the
// floor: a call too many answers `remaining: 0` and costs one turn, and a call
// too few leaves part of the reference list unchecked.
const resolveCalls = referenceCount > 0
  ? Math.min(RESOLVE_CALL_CEILING,
    Math.max(2, Math.ceil(referenceCount / LOOKUP_LIMIT) + 1))
  : RESOLVE_CALL_CEILING
// What those calls can reach. One call is kept back to see `remaining` go to
// zero, so the reach is one batch short of the allowance.
const resolveReach = (resolveCalls - 1) * LOOKUP_LIMIT
// How many rows a worker claims at once, and what the server will serve one
// worker in all. The allowance is the server's own number, carried by ingest,
// so the two cannot drift apart.
const JUDGE_BATCH = 10
const workerAllowance = opened.citation_worker_allowance || 0

async function resolveReferences() {
  return agent(
    'This run keeps a citation ledger: ingest already filled one row per reference and one per'
    + ' in-text use. The reference list holds ' + referenceCount + ' reference(s). Resolve them'
    + ' now. Call resolve_citations with recorder_key="'
    + recorderKey + '" and limit=' + LOOKUP_LIMIT + ', and call it again while it answers'
    + ' remaining above zero and retry_after zero — at most ' + resolveCalls + ' times, which'
    + ' covers ' + resolveReach + ' reference(s). An answer with retry_after above zero means the'
    + ' rows left are waiting out the retry wait between attempts, which is minutes long: calling'
    + ' again would do nothing, so stop there. If remaining is still above zero when you stop,'
    + ' whether you ran out of calls or the rows are waiting, report that remaining as it is: the'
    + ' references nobody reached are reported as unchecked, and guessing at them would be worse.'
    + ' Call no other tool. Report the last answer\'s remaining, its retry_after, its'
    + ' counts.references, its counts.resolved and its lookups_total, and how many times you'
    + ' called it. If it refuses, report the refusal verbatim and stop.',
    options('recorder', {
      label: 'resolve references', phase: 'Citations', agentType: 'pra:recorder', schema: RESOLVED,
    }),
  )
}

const judgePrompt = slot => 'You judge in-text citations against the sources this run has already'
  + ' resolved. Call claim_agent with slot="' + slot + '" and claim_code="' + codes[slot] + '"'
  + ' first; the code is yours alone and works once. Then call claim_citation_batch with size='
  + JUDGE_BATCH
  + ' and model set to the model you are running as. Each row gives you one citing sentence and'
  + ' the stored source text for the work it cites. For every row, call'
  + ' submit_citation_verdict once: the verdict, and the exact passage of the source text you'
  + ' relied on, copied character for character. A row with no source text can only be "cannot'
  + ' tell", which is the only verdict that takes no passage. Judge the sentence against that'
  + ' source and nothing else — you cannot read the manuscript and you are not looking for'
  + ' findings. Claim another batch when you have finished one, and stop when a claim comes back'
  + ' with no rows or your allowance runs out. Report how many verdicts were stored, how many'
  + ' batches you claimed, how many the server refused, the uses_unjudged from your last call,'
  + ' and the model you ran as.'

async function judgeUses(workers) {
  const answers = await parallel(workers.map(slot => () => agent(judgePrompt(slot), options(slot, {
    label: slot, phase: 'Citations', agentType: 'pra:citation-worker', schema: JUDGED,
  }))))
  return answers.filter(Boolean)
}

// What this run can cover, said before it runs rather than inferred from the
// counts afterwards. A truncated pass is reported either way — the ledger's own
// problem lines name the references nobody looked up and the uses nobody judged
// — but a reader watching the lane should not have to work out that a
// 300-reference review was never going to reach the end of the list.
function sayWhatIsReachable(workers) {
  if (referenceCount > resolveReach) {
    log('this reference list is longer than one pass of the lookups: ' + referenceCount
      + ' references, ' + resolveReach + ' reachable in ' + resolveCalls + ' call(s), so about '
      + (referenceCount - resolveReach) + ' will be reported as never looked up')
  }
  const uses = (opened.citations && opened.citations.uses) || 0
  // One claim plus one verdict per row of a batch, out of one worker's whole
  // tool allowance. It is what the server will serve, not an estimate of the
  // model's behaviour.
  const perWorker = Math.floor(workerAllowance / (JUDGE_BATCH + 1)) * JUDGE_BATCH
  const reach = perWorker * workers.length
  // Both numbers come through the same relay as the rest of the plan, and a
  // reach of nothing means one of them did not arrive — not that nothing can
  // be judged. The ledger's own counts report what was judged either way.
  if (workers.length > 0 && reach > 0 && uses > reach) {
    log('this paper cites more often than ' + workers.length + ' worker(s) can judge: ' + uses
      + ' in-text uses, about ' + reach + ' reachable (' + perWorker + ' each), so the rest are'
      + ' reported as never judged — run more citation workers to cover them')
  }
}

async function citationLane(workers) {
  sayWhatIsReachable(workers)
  const resolved = await resolveReferences()
  if (resolved && resolved.refusal) {
    log('the citation ledger was refused: ' + resolved.refusal)
    return { resolved: resolved, verdicts: 0, unjudged: null }
  }
  if (!resolved) {
    log('the reference lookups reported nothing back, so the ledger holds whatever they managed')
  } else {
    log('references: ' + (resolved.resolved || 0) + ' of ' + (resolved.references || 0)
      + ' resolved, ' + resolved.remaining + ' still owed a lookup, ' + (resolved.lookups || 0)
      + ' lookups')
  }
  // Resolving and judging are separate stages, and a run asked for zero
  // workers asked for the lookups without the verdicts. That is a legitimate
  // arm, so the lane does the lookups and stops: the specialist then reads a
  // resolved table whose uses are all unjudged, and the run says so.
  if (workers.length === 0) {
    log('no citation worker was asked for, so the resolved references go unjudged')
    return { resolved: resolved || null, verdicts: 0, unjudged: null, workers: [] }
  }
  const judged = await judgeUses(workers)
  const verdicts = judged.reduce((total, one) => total + (one.judged || 0), 0)
  const lefts = judged.map(one => one.left).filter(one => typeof one === 'number')
  const unjudged = lefts.length > 0 ? Math.min.apply(null, lefts) : null
  log(judged.length + ' of ' + workers.length + ' citation worker(s) reported, ' + verdicts
    + ' verdict(s) stored' + (unjudged ? ', ' + unjudged + ' in-text use(s) left unjudged' : ''))
  return {
    resolved: resolved || null,
    verdicts: verdicts,
    unjudged: unjudged,
    workers: judged.map(one => ({ judged: one.judged || 0, batches: one.batches || 0,
      model: one.model || null })),
  }
}

let citations = null
// The lane runs where the ledger does, which is where the citations specialist
// does — not where the workers do. Gating it on the worker count would have
// `--citation-workers 0` leave the references unresolved as well as unjudged,
// and the one arm that asks for the lookups without the verdicts would get
// neither.
const citationWork = specSlots.indexOf('spec-citations') >= 0
  ? citationLane(workerSlots).then(
    result => { citations = result; return result },
    error => {
      log('the citation ledger did not finish (' + error + '); the citations specialist will'
        + ' report what is in it')
      return null
    },
  )
  : Promise.resolve(null)

phase('Review')
const reviewPrompt = slot => code =>
  'You are reviewing a manuscript that has already been ingested into this run.'
  + ' Call claim_agent with slot="' + slot + '" and claim_code="' + code + '" first: it returns'
  + ' your token, the paper outline and the domain guidance. The code is yours alone and works'
  + ' once, so do not claim any other slot. Then read what you need and submit the findings you'
  + ' would defend, or none. Close with submit_report.'

const specialistPrompt = slot => {
  const track = slot.slice('spec-'.length)
  return code => 'You are the ' + track + ' specialist on this run. Call claim_agent with slot="'
    + slot + '" and claim_code="' + code + '" first: it returns your token and the paper outline.'
    + ' The code is yours alone and works once, so do not claim any other slot. Audit '
    + SPECIALIST_BRIEF[track]
    + '. Submit the findings you would defend, or none, and set escalate only where the finding'
    + ' bears on the central claim. Close with submit_report, passing report_markdown.'
}

const reviewStages = coreSlots.map(slot => () =>
  stage(slot, 'pra:core-reviewer', 'Review', reviewPrompt(slot), STAGE))
// The citations specialist is the one stage that waits: its instrument is the
// ledger, and starting it before the workers have filled one would have it
// reading an empty table. Everything else runs while the lookups do.
const specialistStages = specSlots.map(slot => () => (
  slot === 'spec-citations'
    ? citationWork.then(() => stage(slot, 'pra:citations', 'Review', specialistPrompt(slot), STAGE))
    : stage(slot, 'pra:' + slot.slice('spec-'.length), 'Review', specialistPrompt(slot), STAGE)))

// One barrier, and it is the right one: the editor needs every finding at
// once, and nothing downstream can start per-item.
const reported = await parallel(reviewStages.concat(specialistStages))
const failed = []
reviewStages.concat(specialistStages).forEach((_, index) => {
  if (!reported[index]) failed.push(coreSlots.concat(specSlots)[index])
})
if (failed.length > 0) {
  await block('These stages returned nothing twice: ' + failed.join(', '))
}
const findingsStored = reported.reduce((total, r) => total + (r.findings_stored || 0), 0)
log(reported.length + ' stages closed, ' + findingsStored + ' findings stored')

if (hasRevision) {
  phase('Revision')
  const revised = await stage(
    'revision-1', 'pra:core-reviewer', 'Revision',
    code => 'You reviewed this manuscript as core-1. Claim slot="revision-1" with claim_code="'
    + code + '" and revise your own work: list_findings shows what you stored. Withdraw nothing,'
    + ' but add what a second pass finds and sharpen what you now see differently.'
    + ' Close with submit_report.',
    STAGE,
  )
  if (!revised) await block('The revision stage returned nothing twice')
  reported.push(revised)
}

let edited = null
if (hasEditor) {
  phase('Editor')
  edited = await stage(
    'editor', 'pra:editor', 'Editor',
    code => 'Every reviewer and specialist has reported. Claim slot="editor" with claim_code="'
    + code + '", read every finding with'
    + ' list_findings, then submit_editor: one decision per finding with a reason, duplicates'
    + ' merged, at most about eight findings in the body, a ruling by name on every escalated'
    + ' finding, and the two-axis verdict in words. If it returns problems, fix them and submit'
    + ' again. Close with submit_report.',
    EDITED,
  )
  if (!edited) await block('The editor never submitted an accepted pooled review')
}

phase('Record')
const recorded = await agent(
  'Close the run. Call claim_agent with slot="recorder" and claim_code="' + codeFor('recorder')
  + '", then write_review with that token.'
  + ' Report the status, the body and appendix counts and the escalated count. If it refuses,'
  + ' report the refusal text verbatim in the refusal field.',
  options('recorder', {
    label: 'write review', phase: 'Record', agentType: 'pra:recorder', schema: RECORDED,
  }),
)
if (!recorded || !recorded.status) {
  await block('The run files were never written: the recorder returned nothing')
}
if (recorded.status !== 'complete' && recorded.status !== 'complete-without-findings') {
  log('the run is ' + recorded.status + ': ' + (recorded.refusal || 'no reason given'))
}

return {
  route: 'plugin workflow script',
  arm: opened.arm,
  paper_id: opened.paper_id,
  title: opened.title,
  status: recorded.status,
  refusal: recorded.refusal || null,
  models: assigned,
  stages: reported.map(r => ({
    slot: r.slot, attempts: r.attempts, model: r.model || null,
    assigned_model: assigned[r.slot] || null,
    findings: r.findings_stored || 0, none_found: !!r.none_found,
  })),
  editor: edited && {
    body: edited.body, escalated_ruled: edited.escalated_ruled || 0, attempts: edited.attempts,
  },
  citations: citations,
  findings_stored: findingsStored,
  body_findings: recorded.body_findings,
  appendix_findings: recorded.appendix_findings,
  escalated: recorded.escalated,
  run: run.out,
}
