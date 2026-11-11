/* The browser side of the engine review: a form, a poll, a rendered REVIEW.md.
 *
 * There is nothing to stream. The engine runs each stage as one long model
 * call and records the stage it reached in progress.json, so this polls the
 * job's status and the report instead of pretending to watch tokens arrive.
 */

const FINISHED = ["done", "done-without-findings", "error", "cancelled"];
const POLL_MS = 3000;

function text(node, value) {
  node.textContent = value === null || value === undefined ? "" : String(value);
}

function stamp(seconds) {
  if (!seconds) return "—";
  return new Date(seconds * 1000).toLocaleString();
}

function renderMarkdown(target, source) {
  // marked + DOMPurify are vendored: the report is local, but it is model
  // output, so it is sanitized before it reaches the page.
  if (window.marked && window.DOMPurify) {
    target.innerHTML = window.DOMPurify.sanitize(window.marked.parse(source));
  } else {
    const pre = document.createElement("pre");
    pre.textContent = source;
    target.replaceChildren(pre);
  }
}

async function readJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}

/* ---------------------------------------------------------------- home page */

async function fillForm() {
  const caps = await readJson(await fetch("/api/capabilities"));
  const defaults = caps.form_defaults || {};
  const mode = document.getElementById("mode");
  for (const name of caps.review_modes || ["ensemble"]) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    option.selected = name === defaults.mode;
    mode.append(option);
  }
  const policy = document.getElementById("policy");
  const policies = caps.policies || [caps.default_policy];
  for (const name of policies) {
    const option = document.createElement("option");
    option.value = name === caps.default_policy ? "" : name;
    option.textContent = name === caps.default_policy ? `${name} (default)` : name;
    option.selected = name === defaults.policy;
    policy.append(option);
  }
  if (defaults.reviewers) document.getElementById("reviewers").value = defaults.reviewers;
  if (defaults.provider) document.getElementById("provider").value = defaults.provider;
  if (defaults.literature_cutoff) {
    document.getElementById("cutoff").value = defaults.literature_cutoff;
  }
}

function jobRow(job) {
  const item = document.createElement("li");
  const link = document.createElement("a");
  link.href = `/review.html?job=${encodeURIComponent(job.job_id)}`;
  link.textContent = job.manuscript_name || job.job_id;
  const meta = document.createElement("span");
  meta.className = "meta";
  const stage = job.progress && job.progress.stage ? ` · ${job.progress.stage}` : "";
  meta.textContent = `${job.status}${stage} · ${job.review_mode || ""} · ${stamp(job.created_at)}`;
  item.append(link, meta);
  if (job.errors && job.errors.length) {
    const why = document.createElement("p");
    why.className = "error";
    why.textContent = job.errors[job.errors.length - 1];
    item.append(why);
  }
  return item;
}

function runRow(run) {
  const item = document.createElement("li");
  const link = document.createElement("a");
  link.href = `/review.html?run=${encodeURIComponent(run.name)}`;
  link.textContent = run.name;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = stamp(run.finished_at);
  item.append(link, meta);
  return item;
}

function fill(list, rows, empty, build) {
  if (!rows.length) {
    const item = document.createElement("li");
    item.className = "empty";
    item.textContent = empty;
    list.replaceChildren(item);
    return;
  }
  list.replaceChildren(...rows.map(build));
}

async function refreshLists() {
  const jobs = await readJson(await fetch("/api/reviews"));
  fill(document.getElementById("jobs"), jobs.reviews || [], "No review started yet.", jobRow);
  const runs = await readJson(await fetch("/api/runs"));
  fill(document.getElementById("runs"), runs.runs || [], "Nothing saved yet.", runRow);
}

function startHome() {
  const form = document.getElementById("review-form");
  const error = document.getElementById("form-error");
  const button = document.getElementById("start");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    try {
      const started = await readJson(
        await fetch("/api/reviews", { method: "POST", body: new FormData(form) })
      );
      window.location.href = `/review.html?job=${encodeURIComponent(started.job_id)}`;
    } catch (exc) {
      text(error, exc.message);
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });

  fillForm().catch((exc) => {
    text(error, exc.message);
    error.hidden = false;
  });
  refreshLists().catch(() => {});
  window.setInterval(() => refreshLists().catch(() => {}), POLL_MS);
}

/* -------------------------------------------------------------- review page */

function describe(job) {
  const rows = [
    ["Status", job.status],
    ["Stage", (job.progress && job.progress.stage) || "—"],
    ["Manuscript", job.manuscript_name],
    ["Mode", job.review_mode],
    ["Reviewers", (job.progress && job.progress.reviewers) || "—"],
    ["Policy", job.review_policy],
    ["Model", job.model],
    ["Literature cutoff", job.literature_cutoff || "none"],
    ["Started", stamp(job.started_at)],
    ["Finished", stamp(job.finished_at)],
  ];
  const list = document.getElementById("status");
  list.replaceChildren();
  for (const [label, value] of rows) {
    const key = document.createElement("dt");
    text(key, label);
    const val = document.createElement("dd");
    text(val, value === null || value === undefined || value === "" ? "—" : value);
    list.append(key, val);
  }
}

async function showArtifacts(jobId) {
  const body = await readJson(await fetch(`/api/reviews/${jobId}/artifacts`));
  const artifacts = body.artifacts || [];
  if (!artifacts.length) return;
  const card = document.getElementById("artifact-card");
  const list = document.getElementById("artifacts");
  list.replaceChildren(
    ...artifacts.map((artifact) => {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `/api/reviews/${jobId}/artifacts/${artifact.name}`;
      link.textContent = artifact.name;
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = `${artifact.size} bytes`;
      item.append(link, meta);
      return item;
    })
  );
  card.hidden = false;
}

async function showReport(url) {
  const response = await fetch(url);
  if (!response.ok) return false;
  renderMarkdown(document.getElementById("report"), await response.text());
  document.getElementById("report-note").hidden = true;
  return true;
}

async function startJobPage(jobId) {
  const error = document.getElementById("status-error");
  const cancel = document.getElementById("cancel");
  let reported = false;

  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    try {
      const state = await readJson(
        await fetch(`/api/reviews/${jobId}/cancel`, { method: "POST" })
      );
      if (!state.cancelled) {
        text(error, state.note);
        error.hidden = false;
      }
    } catch (exc) {
      text(error, exc.message);
      error.hidden = false;
    }
  });

  async function tick() {
    let job;
    try {
      job = await readJson(await fetch(`/api/reviews/${jobId}`));
    } catch (exc) {
      text(error, exc.message);
      error.hidden = false;
      return true;
    }
    text(document.getElementById("subject"), job.manuscript_name || jobId);
    describe(job);
    cancel.hidden = FINISHED.includes(job.status);
    if (job.errors && job.errors.length) {
      text(error, job.errors[job.errors.length - 1]);
      error.hidden = false;
    }
    if (!reported && (job.reports || []).includes("REVIEW.md")) {
      reported = await showReport(`/api/reviews/${jobId}/artifacts/REVIEW.md`);
    }
    if (FINISHED.includes(job.status)) {
      await showArtifacts(jobId).catch(() => {});
      if (!reported) {
        text(
          document.getElementById("report-note"),
          job.status === "done-without-findings"
            ? "The review finished and endorsed no finding, so there is no report."
            : "This run did not report."
        );
      }
      return true;
    }
    return false;
  }

  if (await tick()) return;
  const timer = window.setInterval(async () => {
    if (await tick()) window.clearInterval(timer);
  }, POLL_MS);
}

async function startSavedPage(name) {
  text(document.getElementById("subject"), name);
  document.getElementById("status-card").hidden = true;
  if (!(await showReport(`/api/runs/${encodeURIComponent(name)}/report`))) {
    text(document.getElementById("report-note"), "This run has no REVIEW.md.");
  }
}

function startReviewPage() {
  const params = new URLSearchParams(window.location.search);
  const job = params.get("job");
  const run = params.get("run");
  if (job) {
    startJobPage(job);
  } else if (run) {
    startSavedPage(run);
  } else {
    text(document.getElementById("subject"), "No review named.");
  }
}

if (document.getElementById("review-form")) {
  startHome();
} else if (document.getElementById("report")) {
  startReviewPage();
}
