export const meta = {
  name: 'review',
  description: 'Two-track peer review: ingest, N core reviewers and six specialists in parallel, editor, write the run',
  whenToUse: 'Reviewing one manuscript PDF with PeerReviewAgents 0.9. Pass {pdf, out, reviewers, arm}.',
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
  },
  required: ['paper_id', 'arm', 'slots'],
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

// One retry, then blocked: a stage that returns nothing, or returns without
// closing its slot in the ledger, is not a stage that ran.
async function stage(slot, agentType, phaseName, prompt, schema) {
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const result = await agent(prompt, {
      label: label(slot, attempt), phase: phaseName, agentType, schema,
    })
    const ok = result && (schema === EDITED ? result.accepted : result.closed)
    if (ok) return Object.assign({ slot, attempts: attempt }, result)
    const what = result ? 'returned without closing its stage' : 'returned nothing'
    log(slot + ': ' + what + (attempt === 1 ? '; retrying once' : '; the run is blocked'))
  }
  return null
}

async function block(reason) {
  await agent(
    'A stage of this run failed twice. Call mark_blocked with this reason, exactly as written:'
    + ' "' + reason + '". Then report the status it returned.',
    { label: 'mark blocked', phase: 'Record', agentType: 'pra:recorder' },
  )
  throw new Error('review: ' + reason)
}

phase('Ingest')
const opened = await agent(
  'Open a review run. Call ingest exactly once with pdf="' + run.pdf + '", out="' + run.out
  + '", reviewers=' + REVIEWERS + ', arm="' + ARM + '"'
  + (run.literature_cutoff ? ', literature_cutoff="' + run.literature_cutoff + '"' : '')
  + (run.tracks ? ', tracks=' + JSON.stringify(run.tracks) : '')
  + '. Report the paper id, the title, the block count and the slot list it returned.',
  { label: 'ingest', phase: 'Ingest', agentType: 'pra:recorder', schema: OPENED },
)
if (!opened || !opened.slots || opened.slots.length === 0) {
  throw new Error('review: ingest returned no plan, so no stage can be assigned')
}

// The server's slot list is the plan. The arm decides it, so the script never
// has to hold a second copy of which stages a given arm requires.
const slots = opened.slots
const coreSlots = slots.filter(s => /^core-[0-9]+$/.test(s))
const specSlots = slots.filter(s => s.indexOf('spec-') === 0)
const hasRevision = slots.indexOf('revision-1') >= 0
const hasEditor = slots.indexOf('editor') >= 0
log('arm ' + opened.arm + ': ' + coreSlots.length + ' core reviewer(s), '
  + specSlots.length + ' specialist(s), editor ' + (hasEditor ? 'yes' : 'no'))

phase('Review')
const reviewPrompt = slot =>
  'You are reviewing a manuscript that has already been ingested into this run.'
  + ' Call claim_agent with slot="' + slot + '" first: it returns your token, the paper'
  + ' outline and the domain guidance. Then read what you need and submit the findings you'
  + ' would defend, or none. Close with submit_report.'

const specialistPrompt = slot => {
  const track = slot.slice('spec-'.length)
  return 'You are the ' + track + ' specialist on this run. Call claim_agent with slot="'
    + slot + '" first: it returns your token and the paper outline. Audit ' + SPECIALIST_BRIEF[track]
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
    'You reviewed this manuscript as core-1. Claim slot="revision-1" and revise your own work:'
    + ' list_findings shows what you stored. Withdraw nothing, but add what a second pass'
    + ' finds and sharpen what you now see differently. Close with submit_report.',
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
    'Every reviewer and specialist has reported. Claim slot="editor", read every finding with'
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
  'Close the run. Call claim_agent with slot="recorder", then write_review with that token.'
  + ' Report the status, the body and appendix counts and the escalated count. If it refuses,'
  + ' report the refusal text verbatim in the refusal field.',
  { label: 'write review', phase: 'Record', agentType: 'pra:recorder', schema: RECORDED },
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
  stages: reported.map(r => ({
    slot: r.slot, attempts: r.attempts, model: r.model || null,
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
