// PeerReviewAgents — job page frontend.
//
// Renders the "Review Chamber": a pipeline rail across the top and a room of
// desk-cards below, one per agent, wired to a WebSocket of backend events.
// Each desk carries a small state machine (pending | running | done | error),
// a live token counter, and a role-tinted status ring. Clicking a desk opens
// a modal that streams the in-progress work or shows the finished report.
//
// This is a plain-DOM view — no canvas, no bundler. The AGENT_LAYOUT served
// by the backend (/jobs/<id>) tells us the roster, roles and glyphs; roles
// map to zones and to the rail stages below.

const JOB_ID = window.__JOB_ID;
if (!JOB_ID) {
    document.body.innerHTML = '<p style="padding:2rem">No job id in URL.</p>';
    throw new Error('missing job id');
}

// --- DOM hooks ---------------------------------------------------------

const els = {
    room:                 document.getElementById('room'),
    rail:                 document.getElementById('rail'),
    cost:                 document.getElementById('job-cost'),
    status:               document.getElementById('job-status'),
    manuscript:           document.getElementById('manuscript-name'),
    panel:                document.getElementById('panel'),
    panelTitle:           document.getElementById('panel-title'),
    panelEmoji:           document.getElementById('panel-emoji'),
    panelMeta:            document.getElementById('panel-meta'),
    panelBody:            document.getElementById('panel-body'),
    panelClose:           document.getElementById('panel-close'),
    completion:           document.getElementById('completion'),
    completionTitle:      document.getElementById('completion-title'),
    completionBadge:      document.getElementById('completion-badge'),
    completionSub:        document.getElementById('completion-sub'),
    completionStats:      document.getElementById('completion-stats'),
    completionFeatured:   document.getElementById('completion-featured'),
    completionFeaturedCards: document.getElementById('completion-featured-cards'),
    completionAllWrap:    document.getElementById('completion-all-wrap'),
    completionAllList:    document.getElementById('completion-all-list'),
    completionAllCount:   document.getElementById('completion-all-count'),
    completionClose:      document.getElementById('completion-close'),
    completionExplore:    document.getElementById('completion-explore'),
    viewSummaryBtn:       document.getElementById('view-summary-btn'),
};

// --- State -------------------------------------------------------------

const state = {
    job: null,             // /jobs/<id> snapshot
    agents: [],            // AGENT_LAYOUT from backend
    cards: new Map(),      // name -> desk element
    zones: [],             // zone <section>s (each has _agents + _metaEl)
    railNodes: new Map(),  // stage key -> .stage element
    railFill: null,
    railPulse: null,
    buffers: new Map(),    // name -> streamed text
    statusByAgent: new Map(),
    usageByAgent: new Map(),
    selected: null,        // currently inspected agent
    phase: null,
    panelPollHandle: null,
    lastFinal: null,
};

// role -> CSS custom property carrying the desk glow tint.
const ROLE_TINT = {
    reviewer:  '--t-reviewer',
    audit:     '--t-audit',
    debate:    '--t-debate',
    synthesis: '--t-synth',
    verifier:  '--t-verify',
    editor:    '--t-editor',
    recommend: '--t-reviewer',
};

// A one-line description of each agent's remit — gives every desk something
// truthful to say without fabricating "live" reasoning from the token stream
// (which is usually structured-output JSON fragments, not prose).
const BLURBS = {
    desk_screen:               'Triage — can reject before the full panel runs.',
    reviewer_methodology:      'Does the design actually support the conclusions?',
    reviewer_data_analysis:    'Tests, n, error bars, leakage, multiple comparisons.',
    reviewer_novelty:          'Is the contribution genuinely new and significant?',
    reviewer_clarity:          'Can a competent reader follow it without guessing?',
    reviewer_literature:       'Are citations accurate and prior work covered?',
    reviewer_rigor:            'Each load-bearing claim vs. the evidence for it.',
    reviewer_reproducibility:  'Enough data, code and detail to reproduce it?',
    reviewer_ethics:           'Approvals, consent, disclosures, dual-use.',
    audit_methods_completeness:'Required identifiers & reagent traceability.',
    audit_citation_integrity:  'Do the citations resolve and support their claims?',
    advocate:                  'Argues the strongest case for acceptance.',
    skeptic:                   'Tests whether the flaws are fatal or fixable.',
    meta_reviewer:             'Synthesises the panel + debate into one call.',
    author_rebuttal:           'Plays author; fixable vs. disputable critiques.',
    editor:                    'Weighs it all; writes the decision letter.',
    journal_recommender:       'Recommends tiered venues for the verdict.',
};

// --- helpers -----------------------------------------------------------

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function renderMarkdown(md) {
    if (window.marked) return window.marked.parse(md);
    return `<pre>${escapeHtml(md)}</pre>`;
}

const byRole = (role) => state.agents.filter(a => a.role === role).map(a => a.name);

// --- desk cards --------------------------------------------------------

function buildDesk(agent) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'desk pending';
    el.dataset.agent = agent.name;
    el.style.setProperty('--tint', `var(${ROLE_TINT[agent.role] || '--t-reviewer'})`);
    el.innerHTML = [
        '<span class="medallion">',
        '  <span class="ring"></span>',
        `  <span class="glyph">${escapeHtml(agent.emoji || '·')}</span>`,
        '  <span class="stamp"></span>',
        '</span>',
        `<span class="name">${escapeHtml(agent.label)}</span>`,
        '<span class="stat">queued</span>',
        `<span class="thought">${escapeHtml(BLURBS[agent.name] || 'Working the manuscript.')}</span>`,
    ].join('');
    el.addEventListener('click', () => openPanel(agent.name));
    return el;
}

function deskStat(status, usage) {
    const out = usage?.output_tokens || 0;
    const total = (usage?.input_tokens || 0) + out;
    const cost = usage?.cost_usd || 0;
    switch (status) {
        case 'running': return out ? `streaming · ${(out / 1000).toFixed(1)}k` : 'streaming…';
        case 'done':    return total ? `${(total / 1000).toFixed(1)}k tok · $${cost.toFixed(3)}` : 'report ready';
        case 'error':   return 'failed — click for details';
        default:        return 'queued';
    }
}

function applyDesk(name) {
    const el = state.cards.get(name);
    if (!el) return;
    const status = state.statusByAgent.get(name) || 'pending';
    el.classList.remove('pending', 'running', 'done', 'error');
    el.classList.add(status);
    el.querySelector('.stamp').textContent =
        status === 'done' ? '✓' : status === 'error' ? '✕' : '';
    el.querySelector('.stat').textContent = deskStat(status, state.usageByAgent.get(name));
}

// --- zones -------------------------------------------------------------

function zoneMetaText(names) {
    let done = 0, running = 0, err = 0;
    for (const n of names) {
        const s = state.statusByAgent.get(n);
        if (s === 'done') done++;
        else if (s === 'running') running++;
        else if (s === 'error') err++;
    }
    const parts = [];
    if (done) parts.push(`${done} done`);
    if (running) parts.push(`${running} working`);
    if (err) parts.push(`${err} failed`);
    return parts.length ? parts.join(' · ') : 'queued';
}

function zoneSection(cls, badge, title, agents, cols) {
    const sec = document.createElement('section');
    sec.className = `zone ${cls}`;
    const head = document.createElement('div');
    head.className = 'zone-head';
    head.innerHTML =
        `<span class="badge">${escapeHtml(badge)}</span>` +
        `<h2>${escapeHtml(title)}</h2>` +
        `<span class="meta" data-zone-meta></span>`;
    sec.appendChild(head);
    const grid = document.createElement('div');
    grid.className = `desk-grid cols${cols}`;
    for (const a of agents) {
        const d = buildDesk(a);
        grid.appendChild(d);
        state.cards.set(a.name, d);
    }
    sec.appendChild(grid);
    sec._agents = agents.map(a => a.name);
    sec._metaEl = head.querySelector('[data-zone-meta]');
    return sec;
}

function updateZoneMetas() {
    for (const sec of state.zones) {
        sec._metaEl.textContent = zoneMetaText(sec._agents);
    }
}

function renderRoom() {
    els.room.innerHTML = '';
    state.cards = new Map();
    state.zones = [];

    // Optional desk-screen triage gate, only when enabled for this job.
    const gate = state.agents.find(a => a.name === 'desk_screen');
    if (state.job && state.job.desk_screen && gate) {
        const sec = zoneSection('gate', 'Triage', 'Desk Screen', [gate], 1);
        els.room.appendChild(sec);
        state.zones.push(sec);
    }

    const reviewers = state.agents.filter(a => a.role === 'reviewer');
    if (reviewers.length) {
        const sec = zoneSection('reviewers', 'Bullpen', 'Specialist Reviewers', reviewers, 4);
        els.room.appendChild(sec);
        state.zones.push(sec);
    }

    const audits = state.agents.filter(a => a.role === 'audit');
    const debate = state.agents.filter(a => a.role === 'debate');
    if (audits.length || debate.length) {
        const split = document.createElement('div');
        split.className = 'zone-split';
        if (audits.length) {
            const sec = zoneSection('audits', 'Compliance', 'Auditors', audits, 2);
            split.appendChild(sec);
            state.zones.push(sec);
        }
        if (debate.length) {
            const sec = zoneSection('debate', 'Adversarial', 'Debate', debate, 2);
            split.appendChild(sec);
            state.zones.push(sec);
        }
        els.room.appendChild(split);
    }

    const editorial = state.agents.filter(
        a => ['synthesis', 'verifier', 'editor', 'recommend'].includes(a.role)
             && a.name !== 'desk_screen');
    if (editorial.length) {
        const sec = zoneSection('editorial', 'Chair & Bench', 'Editorial', editorial, 4);
        els.room.appendChild(sec);
        state.zones.push(sec);
    }

    for (const a of state.agents) applyDesk(a.name);
    updateZoneMetas();
}

// --- pipeline rail -----------------------------------------------------

const RAIL = [
    { key: 'reviewers', label: 'Reviewers',  glyph: '🔬' },
    { key: 'audits',    label: 'Audits',     glyph: '📋' },
    { key: 'debate',    label: 'Debate',     glyph: '🗣️' },
    { key: 'chair',     label: 'Area Chair', glyph: '🧑‍⚖️' },
    { key: 'editor',    label: 'Editor',     glyph: '👔' },
    { key: 'verdict',   label: 'Verdict',    glyph: '⚖️' },
];

function stageNames(key) {
    switch (key) {
        case 'reviewers': return byRole('reviewer');
        case 'audits':    return byRole('audit');
        case 'debate':    return byRole('debate');
        case 'chair':     return [...byRole('synthesis'), ...byRole('verifier')];
        case 'editor':    return byRole('editor').filter(n => n !== 'desk_screen');
        default:          return [];
    }
}

function stageState(key) {
    if (key === 'verdict') {
        const done = !!(state.job && state.job.decision) ||
                     !!(state.lastFinal && state.lastFinal.status === 'done');
        return done ? 'done' : 'pending';
    }
    const names = stageNames(key);
    if (!names.length) return 'pending';
    const st = names.map(n => state.statusByAgent.get(n) || 'pending');
    if (st.every(s => s === 'done' || s === 'error')) return 'done';
    if (st.some(s => s === 'running' || s === 'done')) return 'active';
    return 'pending';
}

function stageCount(key, st) {
    if (key === 'verdict') {
        if (state.job && state.job.decision) return DECISION_LABELS[state.job.decision] || state.job.decision;
        return st === 'done' ? 'ruled' : '—';
    }
    const names = stageNames(key);
    if (!names.length) return '—';
    const done = names.filter(n => {
        const s = state.statusByAgent.get(n);
        return s === 'done' || s === 'error';
    }).length;
    if (names.length > 1) return `${done} / ${names.length}`;
    return st === 'done' ? 'done' : st === 'active' ? 'running' : 'queued';
}

function renderRail() {
    els.rail.innerHTML = '';
    const track = document.createElement('div');
    track.className = 'rail-track';
    els.rail.appendChild(track);

    const fill = document.createElement('div');
    fill.className = 'rail-fill';
    const pulse = document.createElement('div');
    pulse.className = 'rail-pulse';
    fill.appendChild(pulse);
    els.rail.appendChild(fill);
    state.railFill = fill;
    state.railPulse = pulse;

    state.railNodes = new Map();
    for (const s of RAIL) {
        const stage = document.createElement('div');
        stage.className = 'stage';
        stage.dataset.key = s.key;
        stage.innerHTML =
            `<div class="node">${s.glyph}</div>` +
            `<div class="nm">${escapeHtml(s.label)}</div>` +
            `<div class="ct">—</div>`;
        els.rail.appendChild(stage);
        state.railNodes.set(s.key, stage);
    }
    updateRail();
}

function updateRail() {
    if (!state.railNodes || !state.railNodes.size) return;
    let lastLit = -1;
    RAIL.forEach((s, i) => {
        const st = stageState(s.key);
        const el = state.railNodes.get(s.key);
        el.classList.remove('done', 'active');
        if (st === 'done') el.classList.add('done');
        else if (st === 'active') el.classList.add('active');
        if (st !== 'pending') lastLit = i;
        el.querySelector('.ct').textContent = stageCount(s.key, st);
    });

    const n = RAIL.length, left = 6, track = 88, step = track / n;
    const center = (i) => left + (i + 0.5) * step;
    state.railFill.style.width = lastLit < 0 ? '0%' : `${center(lastLit) - left}%`;

    const running = state.job && state.job.status === 'running';
    state.railPulse.style.display = (running && lastLit >= 0) ? 'block' : 'none';
}

// --- agent report modal ------------------------------------------------

function openPanel(name) {
    state.selected = name;
    const agent = state.agents.find(a => a.name === name);
    if (agent) {
        els.panelTitle.textContent = agent.label;
        els.panelEmoji.textContent = agent.emoji || '·';
    } else {
        els.panelTitle.textContent = name;
        els.panelEmoji.textContent = '·';
    }
    els.panelBody.scrollTop = 0;
    els.panel.hidden = false;
    refreshPanel();
    startPanelPoll();
}

function closePanel() {
    state.selected = null;
    stopPanelPoll();
    els.panel.hidden = true;
}

els.panelClose.addEventListener('click', closePanel);
els.panel.addEventListener('click', (e) => {
    if (e.target instanceof HTMLElement && e.target.dataset.close === '1') closePanel();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !els.panel.hidden) closePanel();
});

function startPanelPoll() {
    stopPanelPoll();
    state.panelPollHandle = setInterval(refreshPanel, 750);
}
function stopPanelPoll() {
    if (state.panelPollHandle != null) {
        clearInterval(state.panelPollHandle);
        state.panelPollHandle = null;
    }
}

async function refreshPanel() {
    if (!state.selected) return;
    const name = state.selected;
    const status = state.statusByAgent.get(name) || 'pending';
    const usage = state.usageByAgent.get(name) ||
        { input_tokens: 0, output_tokens: 0, cost_usd: 0 };

    let payload = null;
    if (status === 'done' || status === 'error') {
        try {
            const resp = await fetch(`/jobs/${JOB_ID}/agents/${encodeURIComponent(name)}`);
            if (resp.ok) payload = await resp.json();
        } catch (err) {
            // Falls through to the buffer rendering path.
        }
        if (state.selected !== name) return;
    }

    const body = (payload && payload.body) || state.buffers.get(name) ||
        (payload && payload.streamed) || '';

    const streamedBytes = (state.buffers.get(name) || '').length;
    const metaParts = [
        `<span>status: <strong>${status}</strong></span>`,
        `<span>tok in: ${usage.input_tokens || 0}</span>`,
        `<span>tok out: ${usage.output_tokens || 0}</span>`,
        `<span>cost: $${(usage.cost_usd || 0).toFixed(4)}</span>`,
    ];
    if (status === 'running' && streamedBytes > 0) {
        metaParts.splice(1, 0, `<span>streamed: ${streamedBytes.toLocaleString()} chars</span>`);
    }
    els.panelMeta.innerHTML = metaParts.join('');

    if (status === 'running') {
        els.panelBody.innerHTML = renderWorkingState(name, usage, streamedBytes);
        return;
    }
    if (status === 'pending') {
        els.panelBody.innerHTML =
            `<p class="muted">${escapeHtml(name)} hasn't started yet. The panel will fill in once it begins.</p>`;
        return;
    }
    if (!body) {
        els.panelBody.innerHTML =
            `<p class="muted">${escapeHtml(name)} finished without producing a body. Check the run log for errors.</p>`;
        return;
    }
    els.panelBody.innerHTML = renderMarkdown(body);
    els.panelBody.scrollTop = els.panelBody.scrollHeight;

    if (status === 'done' || status === 'error') stopPanelPoll();
}

function renderWorkingState(name, usage, streamedBytes) {
    const tokIn = (usage.input_tokens || 0).toLocaleString();
    const tokOut = (usage.output_tokens || 0).toLocaleString();
    const cost = (usage.cost_usd || 0).toFixed(4);
    const chars = streamedBytes.toLocaleString();
    return [
        '<div class="working">',
        '  <div class="working-row">',
        '    <span class="spinner"></span>',
        `    <span class="working-label">${escapeHtml(name)} is working…</span>`,
        '  </div>',
        '  <dl class="working-stats">',
        '    <div><dt>chars streamed</dt><dd>' + chars + '</dd></div>',
        '    <div><dt>tokens in</dt><dd>' + tokIn + '</dd></div>',
        '    <div><dt>tokens out</dt><dd>' + tokOut + '</dd></div>',
        '    <div><dt>cost so far</dt><dd>$' + cost + '</dd></div>',
        '  </dl>',
        '  <p class="muted working-note">',
        '    The rendered report will appear here once this agent finishes.',
        '  </p>',
        '</div>',
    ].join('\n');
}

// --- completion overlay handlers --------------------------------------

function dismissCompletion() { els.completion.hidden = true; }
function reopenCompletion() { if (state.lastFinal) renderCompletion(state.lastFinal); }
els.completionClose.addEventListener('click', dismissCompletion);
els.completionExplore.addEventListener('click', dismissCompletion);
els.completion.addEventListener('click', (e) => {
    if (e.target instanceof HTMLElement && e.target.dataset.close === '1') dismissCompletion();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !els.completion.hidden) dismissCompletion();
});
els.viewSummaryBtn.addEventListener('click', reopenCompletion);

function showSummaryButton(status) {
    // Topbar entry point for the verdict overlay — the ONLY thing that pops
    // it. We deliberately never auto-open the overlay: the user is here to
    // watch the room work, and covering it is jarring.
    if (status === 'done') {
        els.viewSummaryBtn.textContent = 'View summary';
        els.viewSummaryBtn.classList.remove('btn-pill-error');
        els.viewSummaryBtn.hidden = false;
    } else if (status === 'error') {
        els.viewSummaryBtn.textContent = 'View error';
        els.viewSummaryBtn.classList.add('btn-pill-error');
        els.viewSummaryBtn.hidden = false;
    } else {
        els.viewSummaryBtn.hidden = true;
    }
}

function pulseSummaryButton() {
    els.viewSummaryBtn.classList.remove('pulse');
    void els.viewSummaryBtn.offsetWidth;   // force reflow so the anim re-triggers
    els.viewSummaryBtn.classList.add('pulse');
}

// --- WebSocket event handling -----------------------------------------

function setJobStatus(s) {
    els.status.textContent = s;
    els.status.className = `pill pill-${s}`;
}

function setCost(usd) {
    els.cost.textContent = `$${(usd || 0).toFixed(4)}`;
}

function handleEvent(ev) {
    switch (ev.type) {
        case 'job_status':
            if (state.job) state.job.status = ev.status;
            setJobStatus(ev.status);
            updateRail();
            break;
        case 'phase':
            state.phase = ev.phase;
            break;
        case 'node_start': {
            state.statusByAgent.set(ev.agent, 'running');
            applyDesk(ev.agent);
            updateZoneMetas();
            updateRail();
            if (state.selected === ev.agent) startPanelPoll();
            break;
        }
        case 'node_end': {
            const prev = state.statusByAgent.get(ev.agent);
            if (prev !== 'error') {
                state.statusByAgent.set(ev.agent, 'done');
                applyDesk(ev.agent);
            }
            updateZoneMetas();
            updateRail();
            if (state.selected === ev.agent) refreshPanel();
            break;
        }
        case 'node_error': {
            state.statusByAgent.set(ev.agent, 'error');
            applyDesk(ev.agent);
            updateZoneMetas();
            updateRail();
            if (ev.text) console.warn(`[${ev.agent}] ${ev.text}`);
            if (state.selected === ev.agent) refreshPanel();
            break;
        }
        case 'token': {
            const cur = state.buffers.get(ev.agent) || '';
            state.buffers.set(ev.agent, cur + ev.text);
            break;
        }
        case 'usage': {
            const cur = state.usageByAgent.get(ev.agent) ||
                { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
            cur.input_tokens  += ev.input_tokens  || 0;
            cur.output_tokens += ev.output_tokens || 0;
            cur.cost_usd      += ev.cost_usd      || 0;
            state.usageByAgent.set(ev.agent, cur);
            applyDesk(ev.agent);   // refresh the live token counter on the desk
            if (ev.total_cost != null) setCost(ev.total_cost);
            break;
        }
        case 'log': {
            console.log(`[${ev.agent}]`, ev.text);
            if (ev.agent === 'error') els.status.classList.add('pill-error');
            break;
        }
        case 'final':
            state.lastFinal = ev;
            if (state.job) state.job.decision = ev.decision;
            showSummaryButton(ev.status);
            pulseSummaryButton();
            updateRail();
            break;
        default:
            break;
    }
}

// Top three files the user most wants to land on first.
const FEATURED_REPORTS = [
    { file: 'summary.md',                  title: 'Summary',                 blurb: 'Decision badge + per-reviewer scores at a glance.' },
    { file: 'decision_letter.md',          title: 'Decision Letter',         blurb: 'Editor-in-Chief’s reasoning + required revisions.' },
    { file: 'journal_recommendations.md',  title: 'Journal Recommendations', blurb: 'Tiered venue suggestions (as-is / after revision / fallback).' },
];

const DECISION_LABELS = {
    accept: 'Accept',
    minor:  'Minor Revision',
    major:  'Major Revision',
    reject: 'Reject',
};

async function renderCompletion(ev) {
    const overlay = els.completion;
    overlay.hidden = false;

    els.completionBadge.className = 'decision-badge';
    els.completionStats.innerHTML = '';
    els.completionFeaturedCards.innerHTML = '';
    els.completionAllList.innerHTML = '';
    els.completionFeatured.hidden = true;
    els.completionAllWrap.hidden = true;

    const ok = ev.status === 'done' && ev.decision;
    if (ok) {
        els.completionBadge.classList.add(ev.decision);
        els.completionBadge.textContent = DECISION_LABELS[ev.decision] || ev.decision;
        els.completionTitle.textContent = 'Review complete';
        els.completionSub.textContent =
            `The Editor-in-Chief returned ${DECISION_LABELS[ev.decision] || ev.decision}.`;
    } else {
        els.completionBadge.classList.add('error');
        els.completionBadge.textContent = 'Error';
        els.completionTitle.textContent = 'Review failed';
        els.completionSub.textContent =
            (ev.errors && ev.errors.length)
                ? ev.errors.slice(0, 3).join(' · ')
                : 'No decision was produced. Check the run log.';
    }

    const stats = [];
    if (ev.total_cost != null) stats.push({ label: 'cost', value: `$${(ev.total_cost || 0).toFixed(4)}` });
    const dur = jobDuration(state.job);
    if (dur) stats.push({ label: 'duration', value: dur });
    if (state.job && state.job.manuscript_filename) stats.push({ label: 'manuscript', value: state.job.manuscript_filename });
    if (ev.report_dir) {
        const base = ev.report_dir.split('/').filter(Boolean).pop();
        if (base) stats.push({ label: 'job', value: base });
    }
    for (const s of stats) {
        const div = document.createElement('div');
        const dt = document.createElement('dt');
        const dd = document.createElement('dd');
        dt.textContent = s.label;
        dd.textContent = s.value;
        div.appendChild(dt);
        div.appendChild(dd);
        els.completionStats.appendChild(div);
    }

    let files = [];
    try {
        const resp = await fetch(`/jobs/${JOB_ID}/reports`);
        if (resp.ok) files = (await resp.json()).files || [];
    } catch (err) {
        console.warn('failed to list reports', err);
    }
    if (files.length === 0) return;

    const fileSet = new Set(files);
    const featured = FEATURED_REPORTS.filter(f => fileSet.has(f.file));
    if (featured.length) {
        els.completionFeatured.hidden = false;
        for (const f of featured) {
            const a = document.createElement('a');
            a.className = 'report-card';
            a.href = `/jobs/${JOB_ID}/report/${encodeURIComponent(f.file)}`;
            a.target = '_blank';
            a.rel = 'noopener';
            a.innerHTML =
                `<span class="report-card-title">${escapeHtml(f.title)}</span>` +
                `<span class="report-card-meta">${escapeHtml(f.file)}</span>`;
            els.completionFeaturedCards.appendChild(a);
        }
    }

    const remaining = files.filter(f => !FEATURED_REPORTS.some(x => x.file === f));
    if (remaining.length) {
        els.completionAllWrap.hidden = false;
        els.completionAllCount.textContent = String(files.length);
        for (const f of remaining) {
            const li = document.createElement('li');
            const a = document.createElement('a');
            a.href = `/jobs/${JOB_ID}/report/${encodeURIComponent(f)}`;
            a.textContent = f;
            a.target = '_blank';
            a.rel = 'noopener';
            li.appendChild(a);
            els.completionAllList.appendChild(li);
        }
    } else if (featured.length) {
        els.completionAllWrap.hidden = false;
        els.completionAllCount.textContent = String(files.length);
    }
}

function jobDuration(job) {
    if (!job || !job.started_at) return '';
    const end = job.finished_at || Date.now() / 1000;
    const seconds = Math.max(0, end - job.started_at);
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${s}s`;
}

// --- Boot --------------------------------------------------------------

async function boot() {
    const resp = await fetch(`/jobs/${JOB_ID}`);
    if (!resp.ok) {
        document.body.innerHTML = '<p style="padding:2rem">Job not found.</p>';
        return;
    }
    state.job = await resp.json();
    state.agents = state.job.agents || [];
    els.manuscript.textContent = state.job.manuscript_filename || '';
    setJobStatus(state.job.status);
    setCost(state.job.total_cost || 0);
    for (const [name, s] of Object.entries(state.job.agent_status || {})) {
        state.statusByAgent.set(name, s);
    }
    for (const [name, u] of Object.entries(state.job.agent_usage || {})) {
        state.usageByAgent.set(name, u);
    }

    renderRail();
    renderRoom();

    // A job that already finished (refresh / bookmark / fast fail): don't pop
    // the overlay automatically — just offer the "View summary" button.
    if (state.job.status === 'done' || state.job.status === 'error') {
        state.lastFinal = {
            status: state.job.status,
            decision: state.job.decision,
            total_cost: state.job.total_cost,
            report_dir: state.job.report_dir,
            errors: state.job.errors,
        };
        showSummaryButton(state.job.status);
        updateRail();
    }

    connectSocket();
}

function connectSocket() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/jobs/${JOB_ID}/events`);
    ws.onmessage = (msg) => {
        try {
            handleEvent(JSON.parse(msg.data));
        } catch (err) {
            console.error('bad event', err, msg.data);
        }
    };
    ws.onclose = () => {
        if (els.status.textContent === 'running') setTimeout(connectSocket, 1500);
    };
}

boot().catch(err => {
    console.error(err);
    document.body.innerHTML =
        `<p style="padding:2rem">Boot failed: ${escapeHtml(String(err))}</p>`;
});
