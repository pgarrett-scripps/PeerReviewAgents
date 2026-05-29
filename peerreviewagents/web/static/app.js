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
    room:           document.getElementById('room'),
    cost:           document.getElementById('job-cost'),
    status:         document.getElementById('job-status'),
    manuscript:     document.getElementById('manuscript-name'),
    panel:          document.getElementById('panel'),
    panelTitle:     document.getElementById('panel-title'),
    panelEmoji:     document.getElementById('panel-emoji'),
    panelMeta:      document.getElementById('panel-meta'),
    panelBody:      document.getElementById('panel-body'),
    panelClose:     document.getElementById('panel-close'),
    banner:         document.getElementById('banner'),
    bannerTitle:    document.getElementById('banner-title'),
    bannerSub:      document.getElementById('banner-sub'),
    bannerLinks:    document.getElementById('banner-links'),
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
    els.panel.classList.add('panel-open');
    refreshPanel();
    startPanelPoll();
}

function closePanel() {
    state.selected = null;
    stopPanelPoll();
    els.panel.classList.remove('panel-open');
    els.panel.classList.add('panel-collapsed');
}

els.panelClose.addEventListener('click', closePanel);

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

// Strip a leading "---\nkey: value\n...\n---\n" frontmatter block.
// We surface the scalars in the panel meta header instead.
function stripFrontmatter(text) {
    if (!text || !text.startsWith('---')) return text;
    const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
    return m ? text.slice(m[0].length) : text;
}

function parseFrontmatter(text) {
    if (!text || !text.startsWith('---')) return {};
    const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    if (!m) return {};
    const out = {};
    for (const line of m[1].split(/\r?\n/)) {
        const idx = line.indexOf(':');
        if (idx < 0) continue;
        const k = line.slice(0, idx).trim();
        const v = line.slice(idx + 1).trim().replace(/^['"]|['"]$/g, '');
        if (k) out[k] = v;
    }
    return out;
}

async function refreshPanel() {
    if (!state.selected) return;
    const name = state.selected;
    const status = state.statusByAgent.get(name) || 'pending';
    const usage = state.usageByAgent.get(name) ||
        { input_tokens: 0, output_tokens: 0, cost_usd: 0 };

    let payload = null;
    try {
        const resp = await fetch(`/jobs/${JOB_ID}/agents/${encodeURIComponent(name)}`);
        if (resp.ok) payload = await resp.json();
    } catch (err) {
        // Fall through to the streamed-buffer rendering path below.
    }
    if (state.selected !== name) return;

    // Prefer the finished/rendered body; fall back to the live stream
    // (also markdown) while the agent is still writing.
    const text = (payload && payload.body) || state.buffers.get(name) || (payload && payload.streamed) || '';
    const fm = parseFrontmatter(text);
    const visible = stripFrontmatter(text);

    // Panel meta line: status + usage + frontmatter scalars.
    const metaParts = [
        `<span>status: <strong>${status}</strong></span>`,
        `<span>tok in: ${usage.input_tokens || 0}</span>`,
        `<span>tok out: ${usage.output_tokens || 0}</span>`,
        `<span>cost: $${(usage.cost_usd || 0).toFixed(4)}</span>`,
    ];
    for (const [k, v] of Object.entries(fm)) {
        metaParts.push(`<span><strong>${escapeHtml(k)}:</strong> ${escapeHtml(v)}</span>`);
    }
    els.panelMeta.innerHTML = metaParts.join('');

    if (!visible) {
        els.panelBody.innerHTML =
            `<p class="muted">No output yet. Once ${name} starts writing, the report will render here.</p>`;
        return;
    }
    els.panelBody.innerHTML = renderMarkdown(visible);
    els.panelBody.scrollTop = els.panelBody.scrollHeight;

    if (status === 'done' || status === 'error') {
        stopPanelPoll();
    }
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
            renderBanner(ev);
            break;
        default:
            // Unknown event types are no-ops; forward-compatibility.
            break;
    }
}

async function renderBanner(ev) {
    const banner = els.banner;
    banner.hidden = false;
    banner.classList.remove('accept', 'minor', 'major', 'reject');
    const decisionMap = {
        accept: 'Accept',
        minor:  'Minor Revision',
        major:  'Major Revision',
        reject: 'Reject',
    };
    if (ev.status === 'done' && ev.decision) {
        banner.classList.add(ev.decision);
        els.bannerTitle.textContent = `Decision: ${decisionMap[ev.decision] || ev.decision}`;
        els.bannerSub.textContent =
            `Total cost: $${(ev.total_cost || 0).toFixed(4)}` +
            (ev.report_dir ? ` · Reports written to ${ev.report_dir}` : '');
    } else {
        els.bannerTitle.textContent = 'Review failed';
        els.bannerSub.textContent =
            (ev.errors && ev.errors.length)
                ? ev.errors.join('; ')
                : 'no decision was produced';
    }
    els.bannerLinks.innerHTML = '';
    try {
        const resp = await fetch(`/jobs/${JOB_ID}/reports`);
        const data = await resp.json();
        for (const f of (data.files || [])) {
            const li = document.createElement('li');
            const a  = document.createElement('a');
            a.href = `/jobs/${JOB_ID}/report/${encodeURIComponent(f)}`;
            a.textContent = f;
            a.target = '_blank';
            li.appendChild(a);
            els.bannerLinks.appendChild(li);
        }
    } catch (err) {
        console.warn('failed to list reports', err);
    }
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
