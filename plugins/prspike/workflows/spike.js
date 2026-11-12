export const meta = {
  name: 'spike',
  description: 'Ingest one PDF, run one tool-restricted reviewer, check a finding was stored',
  whenToUse: 'Build step 1 of PeerReviewAgents 0.9: proves the plugin mechanism runs headless.',
  phases: [
    { title: 'Ingest', detail: 'import the PDF into an immutable workspace' },
    { title: 'Review', detail: 'one core-reviewer with the peerreview tools only' },
    { title: 'Check', detail: 'the run must carry a stored finding' },
  ],
}

// The stages are a script, not a prompt, so a missing stage is a failure
// rather than something the model forgot. Both stages run as the same
// `core-reviewer` definition: it is the only agent whose tool list is the
// peerreview server and nothing else.

const run = args || {}
if (!run.pdf || !run.out) {
  throw new Error('spike: args must be {"pdf": "<path to PDF>", "out": "<run folder>"}')
}

const INDEX = {
  type: 'object',
  properties: {
    title: { type: 'string', description: 'the title ingest returned' },
    block_count: { type: 'integer', description: 'how many blocks the manuscript has' },
    sections: { type: 'array', items: { type: 'string' }, description: 'section names, in order' },
  },
  required: ['title', 'block_count', 'sections'],
}

const REVIEW = {
  type: 'object',
  properties: {
    receipt: { type: 'string', description: 'the receipt submit_finding returned, verbatim' },
    severity: { type: 'string' },
    claim: { type: 'string' },
    blocks: { type: 'array', items: { type: 'string' }, description: 'the block ids the finding cites' },
    blocks_read: { type: 'integer', description: 'how many blocks you read before judging' },
  },
  required: ['receipt', 'severity', 'claim', 'blocks'],
}

phase('Ingest')
const index = await agent(
  `Call the ingest tool exactly once, with pdf="${run.pdf}" and output="${run.out}".` +
  ' Then stop and report the title, the block count and the section names it returned.' +
  ' Do not read any blocks and do not submit a finding in this turn.',
  { label: 'ingest', phase: 'Ingest', agentType: 'prspike:core-reviewer', schema: INDEX },
)
if (!index || !index.block_count) {
  throw new Error('spike: the ingest stage returned nothing, so the run is blocked')
}
log(`ingested "${index.title}": ${index.block_count} blocks, ${index.sections.length} sections`)

phase('Review')
const review = await agent(
  `Review the manuscript already ingested at out="${run.out}" (title: ${index.title},` +
  ` ${index.block_count} blocks, sections: ${index.sections.join(', ')}).` +
  ' Call ingest once more first with the same arguments to attach to the run, then read the' +
  ' blocks you need and submit exactly one finding. Report the receipt verbatim.',
  { label: 'core-reviewer', phase: 'Review', agentType: 'prspike:core-reviewer', schema: REVIEW },
)
if (!review) {
  throw new Error('spike: the reviewer returned nothing, so the run is blocked')
}

phase('Check')
// A workflow script has no filesystem access, so the receipt is as far as the
// check goes in here. The batch starter reads findings.jsonl afterwards, and
// 0.9 should give the server a `list_findings` tool so a verifier stage can
// check the ledger without taking the reviewer's word for it.
if (!review.receipt || review.receipt.length < 8) {
  throw new Error('spike: no finding was stored; submit_finding returned no receipt')
}
if (!review.blocks || review.blocks.length === 0) {
  throw new Error('spike: the stored finding cites no evidence')
}
log(`finding stored, receipt ${review.receipt}`)

return {
  route: 'plugin workflow script',
  paper: index.title,
  blocks: index.block_count,
  finding: {
    receipt: review.receipt,
    severity: review.severity,
    claim: review.claim,
    blocks: review.blocks,
  },
  findings_file: `${run.out}/findings.jsonl`,
}
