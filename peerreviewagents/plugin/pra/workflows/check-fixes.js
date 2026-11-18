export const meta = {
  name: 'check-fixes',
  description: 'Check a new draft against problems a review already raised: ingest, read the draft, one lane per batch of problems, write the check',
  whenToUse: 'Checking a revised manuscript against the problems a previous review raised, with PeerReviewAgents 0.10. Pass {pdf, out, problems} — the new draft, the run folder, and the path to the problems file — and {specialist_model, recorder_model, reader_model} to choose the models. Nothing new is raised here: every verdict is on a problem from that file. A review is the `review` workflow.',
  phases: [
    { title: 'Ingest', detail: 'import the new draft, read the problems file and fix the plan' },
    { title: 'Read', detail: 'one model reads the page images, repairs the converted equations and tables, describes the figures, and seals the new draft' },
    { title: 'Check', detail: 'one lane per batch of problems, in parallel, each answering its own problems against the new draft' },
    { title: 'Record', detail: 'write fix-check.json and FIXES.md, or mark the run blocked' },
  ],
}

// The same shape as the `review` script and for the same reason: the stages are
// a script, so a stage that never ran is a failure rather than something the
// model forgot. The server owns identity, the problems, the quotations and the
// files; this script owns the order, the fan-out and the one retry. It has no
// filesystem access, so every check here is made through what the server
// returns.
//
// What is different from a review: nothing is raised. There is no reviewer, no
// specialist, no editor and no citation ledger, and the lanes are not required
// to report — a batch nobody answered is counted as not checked, which is the
// honest answer and not a blocked run.

const run = args || {}
if (!run.pdf || !run.out || !run.problems) {
  throw new Error('check-fixes: args must include {"pdf": "<path to the new draft>", '
    + '"out": "<run folder>", "problems": "<path to the problems file>"}')
}

const OPENED = {
  type: 'object',
  properties: {
    paper_id: { type: 'string' },
    title: { type: 'string' },
    arm: { type: 'string' },
    line_count: { type: 'integer', description: 'how many lines paper.md holds; omit it where '
                                                + 'the response says sealed: false, because the '
                                                + 'file does not exist yet' },
    sealed: { type: 'boolean', description: "ingest's sealed, verbatim" },
    reader: {
      type: 'object',
      properties: {
        slot: { type: 'string' },
        model: { type: 'string' },
        allowance: { type: 'integer', description: "the reader's tool allowance" },
      },
      description: 'the reader entry ingest returned; omit it where it returned reader: null',
    },
    draft: {
      type: 'object',
      properties: {
        pages: { type: 'integer', description: 'draft.pages' },
        pieces: { type: 'integer', description: 'draft.pieces' },
      },
      description: 'the pages and pieces from the draft block; omit it where draft is null',
    },
    fix_check: {
      type: 'object',
      properties: {
        slots: { type: 'array', items: { type: 'string' },
          description: 'fix_check.slots, verbatim: one checker per batch of problems' },
        batch: { type: 'integer', description: 'how many problems one checker is handed' },
        limit: { type: 'integer', description: 'the cap on problems this run checks' },
        problems: { type: 'integer', description: 'how many problems the file holds' },
        over_cap: { type: 'integer', description: 'how many are past the cap' },
      },
      description: 'the fix_check entry ingest returned; omit it where it returned fix_check: null',
    },
    recorder_key: { type: 'string' },
    claim_codes: {
      type: 'array',
      items: {
        type: 'object',
        properties: { slot: { type: 'string' }, claim_code: { type: 'string' } },
        required: ['slot', 'claim_code'],
      },
    },
    models: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          slot: { type: 'string' },
          model: { type: 'string' },
          spawn: { type: 'string', description: 'the model override to start that agent with' },
        },
        required: ['slot'],
      },
    },
    refusal: { type: 'string', description: 'the request_rejected text, if ingest refused' },
  },
  required: ['arm', 'sealed'],
}

// The seal, which is the Read stage's whole result. Identical in kind to the
// review script's: the new draft exists as numbered lines or it does not, and
// every verdict below quotes those lines.
const SEALED = {
  type: 'object',
  properties: {
    sealed: { type: 'boolean' },
    pages: { type: 'integer' },
    pages_viewed: { type: 'integer' },
    actions: { type: 'integer', description: 'pieces repaired or confirmed' },
    tool_calls: { type: 'integer' },
    line_count: { type: 'integer', description: "statistics.lines['paper.md']" },
    refusal: { type: 'string', description: 'the last refusal, verbatim, if it could not seal' },
  },
  required: ['sealed'],
}

// A retry needs a code the first attempt has not spent.
const REISSUED = {
  type: 'object',
  properties: { claim_code: { type: 'string' } },
  required: ['claim_code'],
}

// One checker's own account of its batch. There is no `closed` here, because a
// checker closes no stage: the verdicts it stored are the work, and the server
// counts them whether this answer arrives or not.
const ANSWERED = {
  type: 'object',
  properties: {
    answered: { type: 'integer', description: 'how many problems you stored a verdict for' },
    count: { type: 'integer', description: 'how many problems fix_queue handed you' },
    fixed: { type: 'integer' },
    not_fixed: { type: 'integer', description: 'verdicts of not-fixed' },
    cannot_tell: { type: 'integer', description: 'verdicts of cannot-tell' },
    model: { type: 'string', description: 'the model you are running as' },
    summary: { type: 'string', description: 'one or two sentences' },
  },
  required: ['answered', 'count', 'summary'],
}

const WRITTEN = {
  type: 'object',
  properties: {
    status: { type: 'string', description: 'the status write_fix_check returned' },
    checked: { type: 'integer' },
    not_checked: { type: 'integer' },
    fixed: { type: 'integer', description: 'counts.fixed' },
    not_fixed: { type: 'integer', description: "counts['not-fixed']" },
    cannot_tell: { type: 'integer', description: "counts['cannot-tell']" },
    fix_check: { type: 'string', description: 'the path it returned for fix-check.json' },
    fixes: { type: 'string', description: 'the path it returned for FIXES.md' },
    refusal: { type: 'string', description: 'the request_rejected text, if it refused' },
  },
  required: ['status'],
}

function label(slot, attempt) {
  return attempt > 1 ? slot + ' (retry)' : slot
}

let codes = {}
let recorderKey = ''
const codeFor = slot => codes[slot] || ''
let spawns = {}
let assigned = {}
function options(slot, rest) {
  const spawn = spawns[slot]
  if (!spawn) return rest
  return Object.assign({}, rest, { model: spawn })
}

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

async function block(reason) {
  await agent(
    'A stage of this run failed. Call mark_blocked with recorder_key="' + recorderKey
    + '" and this reason, exactly as written: "' + reason + '".'
    + ' Then report the status it returned.',
    options('recorder', { label: 'mark blocked', phase: 'Record', agentType: 'pra:recorder' }),
  )
  throw new Error('check-fixes: ' + reason)
}

phase('Ingest')
const opened = await agent(
  'Open a fix-check run: this is a new draft of a paper that was reviewed, and the problems'
  + ' that review raised are in a file. Call ingest exactly once with pdf="' + run.pdf
  + '", out="' + run.out + '", arm="fix-check", problems="' + run.problems + '"'
  + (run.specialist_model ? ', specialist_model="' + run.specialist_model + '"' : '')
  + (run.recorder_model ? ', recorder_model="' + run.recorder_model + '"' : '')
  + (run.reader_model ? ', reader_model="' + run.reader_model + '"' : '')
  + '. Report the paper id, the title, the arm, the line count of paper.md and — where the'
  + ' response holds them rather than null — the reader entry, the fix_check entry and the'
  + " draft's pages and pieces. Copy the recorder_key, every claim_code and every models entry"
  + ' out of the response verbatim: they are the only copies, and each stage is handed its own'
  + ' code and its own model from here. Report sealed as the response gives it; a response with'
  + ' sealed: false has no line count, so leave that field out rather than reporting a zero. If'
  + ' it refuses, report the refusal text verbatim in the refusal field. Do not claim any slot'
  + ' and do not call any other tool: the problems are the server\'s and every checker is a'
  + ' separate agent.',
  { label: 'ingest', phase: 'Ingest', agentType: 'pra:recorder', schema: OPENED },
)
if (!opened) {
  throw new Error('check-fixes: ingest returned nothing, so there is no run to check anything in')
}
if (opened.refusal) {
  throw new Error('check-fixes: ingest refused: ' + opened.refusal)
}
if (!opened.recorder_key || !opened.claim_codes || opened.claim_codes.length === 0) {
  throw new Error('check-fixes: ingest returned no claim codes, so no agent can prove which slot'
    + ' it is')
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

// The checkers are the whole plan of this run, and the server cut them from the
// problems file: one lane per batch, so the list is read here rather than
// computed from a count this script would hold a second copy of.
const fixCheck = opened.fix_check || {}
const checkSlots = (fixCheck.slots || []).filter(slot => !!codes[slot])
const problemCount = fixCheck.problems || 0
const overCap = fixCheck.over_cap || 0
if (!(fixCheck.slots || []).length) {
  await block('ingest planned no checker, so nothing would read the new draft against the '
    + 'problems: the run was opened on no problems, or not as a fix check')
}
if (checkSlots.length !== (fixCheck.slots || []).length) {
  await block('ingest returned a checker with no claim code, so that batch of problems could '
    + 'not be claimed by anything')
}
// The reader is planned beside the checkers and is not one of them: it works
// before them, on a draft, and answers no problem. Its model has to arrive for
// the same reason every other one does — the draft every verdict quotes is what
// it writes.
const readerSlot = opened.reader && opened.reader.slot ? opened.reader.slot : ''
const unplanned = checkSlots.concat(readerSlot ? [readerSlot] : []).concat(['recorder'])
  .filter(slot => !spawns[slot])
if (unplanned.length > 0) {
  await block('ingest returned no model for ' + unplanned.join(', ') + ', so '
    + (unplanned.length === 1 ? 'that slot' : 'those slots')
    + ' would run whatever the agent file pins while the run records the plan')
}
log('fix check: ' + problemCount + ' problem(s) in ' + checkSlots.length + ' lane(s) of up to '
  + (fixCheck.batch || '?') + (overCap ? ', ' + overCap + ' past the cap of '
    + (fixCheck.limit || '?') + ' and counted as not checked' : ''))
log('models: ' + checkSlots.map(s => s + ' ' + (assigned[s] || spawns[s])).join(', '))

// -- the Read stage ---------------------------------------------------------
//
// The same stage as a review's, for the same reason: the converter mangles
// equations and tables and returns no figure images, and a verdict quotes
// lines, which do not exist until the draft is sealed. This is the *new*
// draft — the one the problems are checked against.

phase('Read')
const readerPrompt = code => 'You read this manuscript\'s pages and repair the converted text.'
  + ' Call claim_agent with slot="' + readerSlot + '" and claim_code="' + code + '" first: it'
  + ' returns your token, what the draft holds and what you have to account for. The code is'
  + ' yours alone and works once. Then call read_draft — it costs nothing — and work through'
  + ' every part of every document it names.'
  + ' You must account for every equation, every table and every figure, and view every page:'
  + ' for each piece either repair it from the page image (replace_equation, replace_table,'
  + ' describe_figure, merge_equations for one equation the converter split, add_piece for one it'
  + ' missed) or, having looked at the page, confirm_piece that the text is what the page prints.'
  + ' A repair is refused unless you have viewed that page in this attempt, so view the page'
  + ' before you touch anything on it. Transcribe and describe: do not judge the science and do'
  + ' not correct what the authors printed, even an obvious typo.'
  + ' Then call seal_manuscript. It refuses while anything is unaccounted for and names what is'
  + ' left; finish those and seal again. Report whether it answered sealed: true, the ai_read'
  + ' counts and the line count from its response. If you cannot seal, report sealed: false with'
  + ' the last refusal verbatim.'

async function readStage() {
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const code = attempt === 1 ? codeFor(readerSlot) : await reissue(readerSlot, 'Read')
    if (!code) {
      log('the reader has no claim code, so the new draft cannot be read')
      return null
    }
    const result = await agent(readerPrompt(code), options(readerSlot, {
      label: label(readerSlot, attempt), phase: 'Read', agentType: 'pra:reader', schema: SEALED,
    }))
    if (result && result.sealed) return Object.assign({ attempts: attempt }, result)
    const what = result && result.refusal ? 'was refused the seal (' + result.refusal + ')'
      : result ? 'reported back without sealing the draft' : 'returned nothing'
    log('the reader ' + what + (attempt === 1 ? '; retrying once' : ''))
  }
  return null
}

let lineCount = opened.line_count || 0
let read = null
if (opened.sealed) {
  log('the manuscript in this folder was already sealed, so there is no draft to read: the'
    + ' checkers read the lines it already has')
} else if (!readerSlot) {
  await block('ingest returned an unsealed draft and no reader, so nothing can write the '
    + 'manuscript and no problem can be answered against it')
} else {
  log('reading ' + ((opened.draft && opened.draft.pages) || '?') + ' page(s) and '
    + ((opened.draft && opened.draft.pieces) || '?') + ' piece(s) with '
    + (assigned[readerSlot] || spawns[readerSlot]) + ', allowance '
    + ((opened.reader && opened.reader.allowance) || '?') + ' call(s)')
  read = await readStage()
  if (read) {
    log('read: ' + (read.pages_viewed || 0) + ' of ' + (read.pages || 0) + ' page(s) viewed, '
      + (read.actions || 0) + ' piece(s) repaired or confirmed in ' + (read.tool_calls || 0)
      + ' call(s)')
    lineCount = read.line_count || lineCount
  } else {
    // The same fallback as a review's, and the same reason: with no manuscript
    // there is nothing to check at all. The draft is sealed as converted and
    // the checkers read the converter's text, with the manifest saying the
    // paper was not AI-read — a quotation of a garbled equation is still a
    // quotation the server can anchor.
    const fallback = await agent(
      'The Read stage failed twice, so this run has no manuscript and no problem can be answered'
      + ' against it. Seal it as converted: call seal_as_converted with recorder_key="'
      + recorderKey + '" and reason="the reader stage returned twice without sealing the new'
      + ' draft". Report whether it answered sealed: true and statistics.lines[\'paper.md\'] as'
      + ' the line count. If it refuses, report the refusal verbatim.',
      options('recorder', {
        label: 'seal as converted', phase: 'Read', agentType: 'pra:recorder', schema: SEALED,
      }),
    )
    if (!fallback || !fallback.sealed) {
      await block('the reader did not seal the new draft and it could not be sealed as '
        + 'converted' + (fallback && fallback.refusal ? ': ' + fallback.refusal : ''))
    }
    log('the new draft was sealed as converted after the reader failed: the checkers read the'
      + ' converter\'s equations and no figure has a description')
    lineCount = fallback.line_count || lineCount
  }
}
if (lineCount > 0) log('the new draft is ' + lineCount + ' line(s)')

// -- the checkers -----------------------------------------------------------
//
// One lane per batch, in parallel, each isolated: a checker sees its own
// problems and nothing else, and the server deals them out. No retry here and
// no block: a lane that returns nothing has still stored whatever verdicts it
// submitted, and the problems it never answered are counted as not checked by
// the written document rather than guessed at from this side.

phase('Check')
const checkPrompt = slot => code =>
  'This is a new draft of a paper that was reviewed, and you check it against problems that'
  + ' review raised. Call claim_agent with slot="' + slot + '" and claim_code="' + code
  + '" first: it returns your token and says how many problems are yours. The code is yours'
  + ' alone and works once, so do not claim any other slot. Then call read_paper and read every'
  + ' line of the new draft — it costs nothing — and then fix_queue, which hands you your'
  + ' problems with the quotation(s) of the old draft each was raised against and whether those'
  + ' words are still in this one. Answer every problem with submit_fix_check: fixed where this'
  + ' draft no longer has it, not-fixed where it still does, cannot-tell where this draft does'
  + ' not settle it. fixed and not-fixed carry a quotation of this draft, exact as read_paper'
  + ' served it, which the server checks before anything is stored; where the fix was a deletion'
  + ' there is nothing left to quote, so answer with the searches you ran and the server will'
  + ' confirm the old words are gone. cannot-tell carries the reason. Every verdict carries a'
  + ' note for the authors. You review nothing, you raise no problem of your own, and you do not'
  + ' judge whether a problem was worth raising. Report how many problems you were handed, how'
  + ' many you answered and how the verdicts came out.'

const answers = (await parallel(checkSlots.map(slot => () =>
  agent(checkPrompt(slot)(codeFor(slot)), options(slot, {
    label: slot, phase: 'Check', agentType: 'pra:fix-check', schema: ANSWERED,
  })).then(result => (result ? Object.assign({ slot }, result) : { slot, silent: true })),
))).filter(Boolean)
answers.forEach(one => {
  if (one.silent) {
    log(one.slot + ': returned nothing, so its problems are answered only as far as it stored'
      + ' verdicts before it stopped; the rest are counted as not checked')
    return
  }
  log(one.slot + ': ' + (one.answered || 0) + ' of ' + (one.count || 0) + ' problem(s) answered — '
    + (one.fixed || 0) + ' fixed, ' + (one.not_fixed || 0) + ' not fixed, '
    + (one.cannot_tell || 0) + ' cannot tell')
})

// -- the written check ------------------------------------------------------
//
// `write_fix_check` is the recorder's, as `write_review` is in a review, and it
// is the only thing that writes fix-check.json and FIXES.md. A check that
// answered nothing is still a finished check — the problems are listed as not
// checked — so this stage is where the run gets its status.

phase('Record')
const written = status => status === 'complete' || status === 'complete-without-findings'
let recorded = await agent(
  'Close the run. Call write_fix_check with recorder_key="' + recorderKey + '".'
  + ' Report the status, checked, not_checked, the counts for fixed, not-fixed and cannot-tell,'
  + ' and the fix_check and fixes paths it returned. If it refuses, report the refusal text'
  + ' verbatim in the refusal field — and where it refuses because the check is already'
  + ' written, report the status and the counts it returns with that refusal, which are the'
  + ' counts of the check that was written. Do not call write_fix_check twice.',
  options('recorder', {
    label: 'write the check', phase: 'Record', agentType: 'pra:recorder', schema: WRITTEN,
  }),
)
// A write that landed and an answer that was lost is not a run that was never
// written, so the folder is asked what it holds before anything is blocked.
if (!recorded || !written(recorded.status)) {
  const asFound = await agent(
    'The check of this run may already be written. Call run_status with recorder_key="'
    + recorderKey + '" and report its status. Report only what it gives you: leave a field out'
    + ' where the answer has no number for it, and never report 0 for a count that is absent.'
    + ' Call nothing else.',
    options('recorder', {
      label: 'read the run', phase: 'Record', agentType: 'pra:recorder', schema: WRITTEN,
    }),
  )
  if (asFound && written(asFound.status)) {
    log('the recorder did not answer, and the run folder is ' + asFound.status
      + ': the check was written')
    recorded = Object.assign({}, asFound, { refusal: (recorded && recorded.refusal) || null })
  }
}
if (!recorded || !recorded.status) {
  await block('The run files were never written: the recorder returned nothing and the run '
    + 'folder does not say the check was written')
}
if (!written(recorded.status)) {
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
  read: read && {
    model: assigned[readerSlot] || spawns[readerSlot] || null,
    attempts: read.attempts,
    pages_viewed: read.pages_viewed || 0,
    pages: read.pages || 0,
    actions: read.actions || 0,
    tool_calls: read.tool_calls || 0,
  },
  line_count: lineCount || null,
  problems: problemCount,
  over_cap: overCap,
  // One row per lane, including a lane that never answered: a silent checker is
  // not a lane that found nothing, and the written check counts its problems as
  // not checked.
  lanes: answers.map(one => ({
    slot: one.slot,
    silent: !!one.silent,
    handed: one.count || 0,
    answered: one.answered || 0,
    model: one.model || assigned[one.slot] || null,
  })),
  checked: recorded.checked,
  not_checked: recorded.not_checked,
  fixed: recorded.fixed,
  not_fixed: recorded.not_fixed,
  cannot_tell: recorded.cannot_tell,
  fix_check: recorded.fix_check || null,
  fixes: recorded.fixes || null,
  run: run.out,
}
