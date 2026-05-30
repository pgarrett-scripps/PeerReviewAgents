// PeerReviewAgents — job page frontend.
//
// Renders a 2D "office" with one sprite per agent and wires it to a
// WebSocket of events from the backend. Each sprite has a small state
// machine (idle | working | done | error) plus a tween for desk-to-stage
// movement when a phase changes. Clicking a sprite opens the side panel
// and either streams the in-progress token buffer or fetches the
// finished markdown body, depending on which tab is active.
//
// We pull PixiJS from a CDN as an ES module (no bundler) and lean on
// emoji glyphs as sprite art — they look fine on canvas, weigh nothing,
// and the AGENT_LAYOUT served by the backend tells us which glyph to
// use for each role.

import { Application, Container, Graphics, Text, TextStyle }
    from 'https://cdn.jsdelivr.net/npm/pixi.js@8.6.6/dist/pixi.min.mjs';

const JOB_ID = window.__JOB_ID;
if (!JOB_ID) {
    document.body.innerHTML = '<p style="padding:2rem">No job id in URL.</p>';
    throw new Error('missing job id');
}

// --- DOM hooks ---------------------------------------------------------

const els = {
    stage:                document.querySelector('.stage'),
    room:                 document.getElementById('room'),
    cost:                 document.getElementById('job-cost'),
    status:               document.getElementById('job-status'),
    manuscript:           document.getElementById('manuscript-name'),
    panel:                document.getElementById('panel'),
    panelTitle:           document.getElementById('panel-title'),
    panelEmoji:           document.getElementById('panel-emoji'),
    panelMeta:            document.getElementById('panel-meta'),
    panelBody:            document.getElementById('panel-body'),
    panelClose:           document.getElementById('panel-close'),
    panelExpand:          document.getElementById('panel-expand'),
    // Completion overlay (replaces the legacy bottom banner).
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
    sprites: new Map(),    // name -> sprite container
    desks: new Map(),      // name -> {x, y}
    stage: { x: 0, y: 0 },
    buffers: new Map(),    // name -> streamed text
    statusByAgent: new Map(),
    usageByAgent: new Map(),
    selected: null,        // currently inspected agent
    phase: null,
    // While a panel is open we poll /jobs/<id>/agents/<name> to pick
    // up the finished markdown body when the agent transitions to done.
    panelPollHandle: null,
    // Latest "final" event payload (or a synthetic one we build from the
    // job snapshot on a refresh) so the topbar "View summary" button can
    // re-open the completion modal without re-fetching state.
    lastFinal: null,
};

// --- Layout maths ------------------------------------------------------
//
// The room is divided into three zones:
//   left:  reviewer bullpen (4x2 grid)
//   center: debate stage (advocate, skeptic facing each other)
//   right: synthesis room (meta_reviewer / author / editor in a row)

function computeLayout(width, height) {
    const margin = 60;
    const innerW = width  - margin * 2;
    const innerH = height - margin * 2;

    const reviewersBox = {
        x: margin,
        y: margin,
        w: innerW * 0.42,
        h: innerH * 0.85,
    };
    const debateBox = {
        x: margin + innerW * 0.45,
        y: margin + innerH * 0.10,
        w: innerW * 0.20,
        h: innerH * 0.40,
    };
    const synthesisBox = {
        x: margin + innerW * 0.70,
        y: margin + innerH * 0.10,
        w: innerW * 0.30,
        h: innerH * 0.85,
    };

    const positions = new Map();

    // 4x2 grid for the 8 reviewers (column-major so rows feel tidy)
    const reviewerNames = state.agents
        .filter(a => a.role === 'reviewer')
        .map(a => a.name);
    const cols = 4, rows = 2;
    const colStep = reviewersBox.w / (cols + 1);
    const rowStep = (reviewersBox.h * 0.7) / (rows + 1);
    reviewerNames.forEach((name, i) => {
        const col = i % cols;
        const row = Math.floor(i / cols);
        positions.set(name, {
            x: reviewersBox.x + colStep * (col + 1),
            y: reviewersBox.y + rowStep * (row + 1),
        });
    });
    // Debate stage: two podiums facing each other
    positions.set('advocate', {
        x: debateBox.x + debateBox.w * 0.25,
        y: debateBox.y + debateBox.h * 0.5,
    });
    positions.set('skeptic', {
        x: debateBox.x + debateBox.w * 0.75,
        y: debateBox.y + debateBox.h * 0.5,
    });

    // Synthesis row
    const synthNames = ['meta_reviewer', 'author_rebuttal', 'editor'];
    const synthStep = synthesisBox.h / (synthNames.length + 1);
    synthNames.forEach((name, i) => {
        positions.set(name, {
            x: synthesisBox.x + synthesisBox.w * 0.5,
            y: synthesisBox.y + synthStep * (i + 1),
        });
    });

    return { positions, reviewersBox, debateBox, synthesisBox };
}

// --- Pixi bootstrapping ------------------------------------------------

const app = new Application();
await app.init({
    background: 0x0f1118,
    resizeTo: els.room,
    antialias: true,
    autoDensity: true,
    resolution: window.devicePixelRatio || 1,
});
els.room.appendChild(app.canvas);

const world = new Container();
app.stage.addChild(world);

// Persistent layers so we can clear/redraw the floor on resize without
// nuking sprites.
const floorLayer  = new Container();
const labelLayer  = new Container();
const spriteLayer = new Container();
world.addChild(floorLayer, labelLayer, spriteLayer);

let layout = null;

function drawFloor() {
    floorLayer.removeChildren();
    labelLayer.removeChildren();
    if (!layout) return;
    const zones = [
        { box: layout.reviewersBox, label: 'Reviewers',  color: 0x232a3d },
        { box: layout.debateBox,    label: 'Debate',     color: 0x2a2438 },
        { box: layout.synthesisBox, label: 'Editorial',  color: 0x222c34 },
    ];
    for (const z of zones) {
        const g = new Graphics();
        g.roundRect(z.box.x, z.box.y, z.box.w, z.box.h, 14)
         .fill({ color: z.color, alpha: 0.85 })
         .stroke({ color: 0x303749, width: 1, alpha: 0.9 });
        floorLayer.addChild(g);

        const t = new Text({
            text: z.label.toUpperCase(),
            style: new TextStyle({
                fontFamily: 'system-ui, sans-serif',
                fontSize: 12,
                fontWeight: '700',
                fill: 0x6e7691,
                letterSpacing: 2,
            }),
        });
        t.position.set(z.box.x + 14, z.box.y + 10);
        labelLayer.addChild(t);
    }
}

// --- Sprite construction ----------------------------------------------

const STATUS_COLOR = {
    pending: 0x3a4256,
    running: 0xf6c177,
    done:    0x6ed392,
    error:   0xef6c6c,
};

const ROLE_TINT = {
    reviewer:  0x6ea8ff,
    debate:    0xb388ff,
    synthesis: 0x88d4c2,
    author:    0xffadad,
    editor:    0xff9f1c,
};

function buildSprite(agent) {
    const container = new Container();
    container.eventMode = 'static';
    container.cursor = 'pointer';
    container.label = agent.name;

    const ring = new Graphics();
    container.addChild(ring);

    const disc = new Graphics();
    disc.circle(0, 0, 26).fill({ color: 0x1c2030 })
                          .stroke({ color: 0x2a3145, width: 2 });
    container.addChild(disc);

    const emoji = new Text({
        text: agent.emoji || '·',
        style: new TextStyle({ fontFamily: 'system-ui, "Segoe UI Emoji"', fontSize: 30 }),
    });
    emoji.anchor.set(0.5);
    container.addChild(emoji);

    const label = new Text({
        text: agent.label,
        style: new TextStyle({
            fontFamily: 'system-ui, sans-serif',
            fontSize: 11,
            fontWeight: '600',
            fill: 0xc8cee0,
        }),
    });
    label.anchor.set(0.5, 0);
    label.position.set(0, 36);
    container.addChild(label);

    // Thinking bubble — shown while running, hidden otherwise.
    const bubble = new Graphics();
    bubble.circle(18, -26, 4).fill({ color: 0xf6c177 });
    bubble.circle(26, -32, 3).fill({ color: 0xf6c177, alpha: 0.7 });
    bubble.circle(32, -36, 2).fill({ color: 0xf6c177, alpha: 0.45 });
    bubble.visible = false;
    container.addChild(bubble);

    // Pulse hover effect with the role tint
    const tint = ROLE_TINT[agent.role] ?? 0x6ea8ff;
    container.on('pointerover', () => ring.tint = tint);
    container.on('pointerout',  () => ring.tint = 0xffffff);
    container.on('pointertap',  () => openPanel(agent.name));

    container._parts = { ring, disc, emoji, label, bubble };
    container._agent = agent;
    container._state = 'pending';
    container._phase = 0;  // animation phase for the bubble bob
    return container;
}

function setSpriteState(sprite, status) {
    if (!sprite) return;
    sprite._state = status;
    const color = STATUS_COLOR[status] ?? STATUS_COLOR.pending;
    const { ring, bubble } = sprite._parts;
    ring.clear();
    ring.circle(0, 0, 32).stroke({ color, width: 3, alpha: status === 'pending' ? 0.4 : 0.9 });
    bubble.visible = status === 'running';
}

// --- Layout + sprite placement -----------------------------------------

function layoutSprites() {
    if (!layout) return;
    for (const agent of state.agents) {
        const pos = layout.positions.get(agent.name);
        if (!pos) continue;
        state.desks.set(agent.name, pos);
        let sprite = state.sprites.get(agent.name);
        if (!sprite) {
            sprite = buildSprite(agent);
            state.sprites.set(agent.name, sprite);
            spriteLayer.addChild(sprite);
            setSpriteState(sprite, state.statusByAgent.get(agent.name) || 'pending');
        }
        sprite.position.set(pos.x, pos.y);
        sprite._home = { x: pos.x, y: pos.y };
    }
}

function relayout() {
    layout = computeLayout(app.screen.width, app.screen.height);
    drawFloor();
    layoutSprites();
}

window.addEventListener('resize', () => {
    // PixiJS resizes the canvas via resizeTo; recompute layout once
    // the next frame settles.
    queueMicrotask(relayout);
});

// --- Animation tick: bob "working" sprites & their bubbles -------------

app.ticker.add((ticker) => {
    const dt = ticker.deltaMS / 1000;
    for (const sprite of state.sprites.values()) {
        if (sprite._state !== 'running') continue;
        sprite._phase += dt * 4;
        const bob = Math.sin(sprite._phase) * 3;
        sprite.position.y = (sprite._home?.y ?? sprite.position.y) + bob;
    }
});

// --- Side panel --------------------------------------------------------

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
    els.panel.classList.remove('panel-collapsed');
    els.panel.setAttribute('aria-hidden', 'false');
    // The grid layout is driven by classes on the stage; this animates
    // the room/panel split open without absolute positioning tricks.
    els.stage.classList.add('panel-open');
    refreshPanel();
    startPanelPoll();
    // Let the room canvas resize into the new column width.
    window.dispatchEvent(new Event('resize'));
}

function closePanel() {
    state.selected = null;
    stopPanelPoll();
    els.stage.classList.remove('panel-open', 'panel-expanded');
    els.panel.classList.add('panel-collapsed');
    els.panel.setAttribute('aria-hidden', 'true');
    els.panelExpand.textContent = '⇱';
    window.dispatchEvent(new Event('resize'));
}

function togglePanelExpand() {
    const expanded = els.stage.classList.toggle('panel-expanded');
    els.panelExpand.textContent = expanded ? '⇲' : '⇱';
    els.panelExpand.title = expanded ? 'Shrink' : 'Expand';
    window.dispatchEvent(new Event('resize'));
}

els.panelClose.addEventListener('click', closePanel);
els.panelExpand.addEventListener('click', togglePanelExpand);

// --- completion overlay handlers --------------------------------------

function dismissCompletion() {
    els.completion.hidden = true;
}
function reopenCompletion() {
    if (state.lastFinal) renderCompletion(state.lastFinal);
}
els.completionClose.addEventListener('click', dismissCompletion);
els.completionExplore.addEventListener('click', dismissCompletion);
els.completion.addEventListener('click', (e) => {
    // Click on the backdrop (not the card itself) dismisses.
    if (e.target instanceof HTMLElement && e.target.dataset.close === '1') {
        dismissCompletion();
    }
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !els.completion.hidden) dismissCompletion();
});
els.viewSummaryBtn.addEventListener('click', reopenCompletion);

function showSummaryButton(status) {
    // Topbar entry point for opening the completion modal — it's the
    // ONLY thing that should pop the modal. We deliberately do not
    // auto-open the modal on the live 'final' event or on a refresh,
    // because the user is here to watch the room work; covering it
    // with an overlay is jarring.
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
    // Brief attention pulse so the user notices the new button without
    // shoving a modal in their face.
    els.viewSummaryBtn.classList.remove('pulse');
    // Force a reflow so the animation re-triggers if it was already on.
    void els.viewSummaryBtn.offsetWidth;
    els.viewSummaryBtn.classList.add('pulse');
}

function startPanelPoll() {
    stopPanelPoll();
    // Pull the rendered body on a steady cadence — picks up the
    // finished markdown body as soon as the worker writes it without
    // racing the WebSocket node_end event.
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
    // Only hit the REST endpoint when the agent is settled — while it's
    // running we have nothing finished to show in the body, and the
    // streaming spinner pulls its data straight from state.
    if (status === 'done' || status === 'error') {
        try {
            const resp = await fetch(`/jobs/${JOB_ID}/agents/${encodeURIComponent(name)}`);
            if (resp.ok) payload = await resp.json();
        } catch (err) {
            // Falls through to the buffer rendering path.
        }
        if (state.selected !== name) return;
    }

    // Finished body (rendered markdown from the agent's pydantic schema)
    // or the streamed buffer as a fallback if the worker hasn't published
    // the rendered body yet.
    const body = (payload && payload.body) || state.buffers.get(name) || (payload && payload.streamed) || '';

    // Panel meta line: status + token counters + cost.
    const streamedBytes = (state.buffers.get(name) || '').length;
    const metaParts = [
        `<span>status: <strong>${status}</strong></span>`,
        `<span>tok in: ${usage.input_tokens || 0}</span>`,
        `<span>tok out: ${usage.output_tokens || 0}</span>`,
        `<span>cost: $${(usage.cost_usd || 0).toFixed(4)}</span>`,
    ];
    if (status === 'running' && streamedBytes > 0) {
        metaParts.splice(1, 0,
            `<span>streamed: ${streamedBytes.toLocaleString()} chars</span>`,
        );
    }
    els.panelMeta.innerHTML = metaParts.join('');

    // Running agents get a spinner + live counters. We deliberately don't
    // render the streaming buffer here — with structured outputs the
    // bytes flowing through are usually JSON fragments, not human-friendly
    // prose. Once the node finishes the body renders below.
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

    if (status === 'done' || status === 'error') {
        stopPanelPoll();
    }
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

function escapeHtml(s) {
    return s.replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

function renderMarkdown(md) {
    if (window.marked) {
        return window.marked.parse(md);
    }
    return `<pre>${escapeHtml(md)}</pre>`;
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
            setJobStatus(ev.status);
            break;
        case 'phase':
            state.phase = ev.phase;
            break;
        case 'node_start': {
            state.statusByAgent.set(ev.agent, 'running');
            setSpriteState(state.sprites.get(ev.agent), 'running');
            if (state.selected === ev.agent) startPanelPoll();
            break;
        }
        case 'node_end': {
            // Don't overwrite an error state.
            const prev = state.statusByAgent.get(ev.agent);
            if (prev !== 'error') {
                state.statusByAgent.set(ev.agent, 'done');
                setSpriteState(state.sprites.get(ev.agent), 'done');
            }
            // Force one more refresh so the panel grabs the final body
            // even if the polling tick is mid-interval.
            if (state.selected === ev.agent) refreshPanel();
            break;
        }
        case 'token': {
            const cur = state.buffers.get(ev.agent) || '';
            state.buffers.set(ev.agent, cur + ev.text);
            // Panel re-renders on the poll tick — no per-token DOM work.
            break;
        }
        case 'usage': {
            const cur = state.usageByAgent.get(ev.agent) ||
                { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
            cur.input_tokens  += ev.input_tokens  || 0;
            cur.output_tokens += ev.output_tokens || 0;
            cur.cost_usd      += ev.cost_usd      || 0;
            state.usageByAgent.set(ev.agent, cur);
            if (ev.total_cost != null) setCost(ev.total_cost);
            // usage updates land on the panel via the next poll tick.
            break;
        }
        case 'log': {
            console.log(`[${ev.agent}]`, ev.text);
            if (ev.agent === 'error') {
                // Flag any sprite whose state is still pending as
                // unknown rather than the whole UI lighting up red.
                els.status.classList.add('pill-error');
            }
            break;
        }
        case 'final':
            state.lastFinal = ev;
            showSummaryButton(ev.status);
            // Don't auto-pop the modal — it covers the room the user
            // came to see. Highlight the topbar button instead; the
            // user opens the modal when they want it.
            pulseSummaryButton();
            break;
        default:
            // Unknown event types are no-ops; forward-compatibility.
            break;
    }
}

// Top three files the user most wants to land on first.
const FEATURED_REPORTS = [
    { file: 'summary.md',                  title: 'Summary',                blurb: 'Decision badge + per-reviewer scores at a glance.' },
    { file: 'decision_letter.md',          title: 'Decision Letter',        blurb: 'Editor-in-Chief’s reasoning + required revisions.' },
    { file: 'journal_recommendations.md',  title: 'Journal Recommendations',blurb: 'Tiered venue suggestions (as-is / after revision / fallback).' },
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

    // Reset state from a previous (e.g. cached) render.
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
            `Editor-in-Chief returned ${DECISION_LABELS[ev.decision] || ev.decision}.`;
    } else {
        els.completionBadge.classList.add('error');
        els.completionBadge.textContent = 'Error';
        els.completionTitle.textContent = 'Review failed';
        els.completionSub.textContent =
            (ev.errors && ev.errors.length)
                ? ev.errors.slice(0, 3).join(' · ')
                : 'No decision was produced. Check the run log.';
    }

    // Stats row: cost / duration / manuscript / job id.
    const stats = [];
    if (ev.total_cost != null) {
        stats.push({ label: 'cost', value: `$${(ev.total_cost || 0).toFixed(4)}` });
    }
    const dur = jobDuration(state.job);
    if (dur) stats.push({ label: 'duration', value: dur });
    if (state.job && state.job.manuscript_filename) {
        stats.push({ label: 'manuscript', value: state.job.manuscript_filename });
    }
    if (ev.report_dir) {
        // Just the basename (job id is the slug); the full path is in the
        // CLI output. Most users don't need the absolute path here.
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

    // Report files: featured cards + collapsible full list.
    let files = [];
    try {
        const resp = await fetch(`/jobs/${JOB_ID}/reports`);
        if (resp.ok) {
            const data = await resp.json();
            files = data.files || [];
        }
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
            a.innerHTML = `
                <span class="report-card-title">${escapeHtml(f.title)}</span>
                <span class="report-card-meta">${escapeHtml(f.file)}</span>
            `;
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
        // Featured covered everything; still let the user open the
        // accordion to confirm there's nothing else hiding.
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

    relayout();

    // If the job already finished (refresh / bookmarked link / a fast-
    // failing run that completed before the page loaded), don't pop the
    // modal automatically — that hides the room and is jarring. Just
    // reveal the "View summary" button so the user can open it when
    // they want.
    if (state.job.status === 'done' || state.job.status === 'error') {
        state.lastFinal = {
            status: state.job.status,
            decision: state.job.decision,
            total_cost: state.job.total_cost,
            report_dir: state.job.report_dir,
            errors: state.job.errors,
        };
        showSummaryButton(state.job.status);
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
        // If the job is still running per our local view, retry once
        // after a beat — handles browser refresh more than real outages.
        if (els.status.textContent === 'running') {
            setTimeout(connectSocket, 1500);
        }
    };
}

boot().catch(err => {
    console.error(err);
    document.body.innerHTML =
        `<p style="padding:2rem">Boot failed: ${escapeHtml(String(err))}</p>`;
});
