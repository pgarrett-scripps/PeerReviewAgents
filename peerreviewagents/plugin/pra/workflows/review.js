export const meta = {
  name: 'review',
  description: 'Two-track peer review: ingest, N core reviewers and six specialists in parallel, editor, write the run',
  whenToUse: 'Reviewing one manuscript PDF with PeerReviewAgents 0.9. Pass {pdf, out, reviewers, arm}, and {reviewer_models, specialist_model, editor_model, recorder_model} to choose the models.',
  phases: [
    { title: 'Ingest', detail: 'import the PDF and fix the plan for this arm' },
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

const SPECIALIST_BRIEF = {
  'stats': 'the statistics: tests against the design, n, multiplicity, effect sizes and the arithmetic of the reported numbers',
  'figures-tables': 'the figures and tables against their captions, the text that cites them and the numbers they should match',
  'methods-reporting': 'whether the work could be repeated from what is written, and whether the required declarations are there',
  'citations': 'whether each cited work supports the sentence it is attached to, and whether the references resolve',
  'consistency': 'whether the paper agrees with itself across abstract, results, figures and supplements',
  'field': 'the paper against the standards of its own field, working from the domain pack the server routes to you',
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
  + '. Report the paper id, the title, the block count and the slot list it returned, and copy'
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
const unplanned = slots.filter(slot => !spawns[slot])
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
const specialistStages = specSlots.map(slot => () =>
  stage(slot, 'pra:' + slot.slice('spec-'.length), 'Review', specialistPrompt(slot), STAGE))

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
  findings_stored: findingsStored,
  body_findings: recorded.body_findings,
  appendix_findings: recorded.appendix_findings,
  escalated: recorded.escalated,
  run: run.out,
}
