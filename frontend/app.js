"use strict";

const $ = (id) => document.getElementById(id);
const LABELS = { anthropic: "Claude (Anthropic)", openai: "OpenAI", gemini: "Gemini (Google)" };
let CONFIG = null;
let selectedProvider = null;
let fileId = null;
let pollTimer = null;

// ---- browser-only key storage (per provider) ----
const keyStore = {
  get: (p) => localStorage.getItem("v8_key_" + p) || "",
  set: (p, v) => v ? localStorage.setItem("v8_key_" + p, v) : localStorage.removeItem("v8_key_" + p),
  clear: (p) => localStorage.removeItem("v8_key_" + p),
};

async function init() {
  CONFIG = await (await fetch("/api/config")).json();
  setupNav();
  renderProviders();
  const d = CONFIG.defaults;
  $("batch").value = d.batch_size;
  $("conc").value = d.concurrency;
  $("maxtok").value = d.max_tokens;
  $("mode").addEventListener("change", () => {
    const notes = $("mode").value === "notes";
    $("levelWrap").classList.toggle("hidden", !notes);
    $("colsRow").classList.toggle("hidden", notes);
  });
  setupKeyField();
  setupUpload();
  $("run").addEventListener("click", run);
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchTab(t.dataset.tab)));
}

function setupNav() {
  document.querySelectorAll("[data-view]").forEach((b) =>
    b.addEventListener("click", (e) => {
      e.preventDefault();
      showView(b.dataset.view);
    }));
}
function showView(view) {
  $("view-tool").classList.toggle("hidden", view !== "tool");
  $("view-help").classList.toggle("hidden", view !== "help");
  document.querySelectorAll(".navbtn").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderProviders() {
  const wrap = $("providers");
  wrap.innerHTML = "";
  for (const [name, info] of Object.entries(CONFIG.providers)) {
    const el = document.createElement("div");
    el.className = "provider";
    el.dataset.name = name;
    el.innerHTML =
      `<div class="name">${LABELS[name] || name}</div>` +
      `<div class="status">${providerStatus(name, info)}</div>`;
    el.addEventListener("click", () => selectProvider(name));
    wrap.appendChild(el);
  }
  const pref = CONFIG.providers[CONFIG.defaults.provider] ? CONFIG.defaults.provider
    : Object.keys(CONFIG.providers)[0];
  selectProvider(pref);
}

function providerStatus(name, info) {
  if (info.configured) return `<span class="dot on"></span>server key set`;
  if (keyStore.get(name)) return `<span class="dot on"></span>your key saved`;
  return `<span class="dot off"></span>bring your own key`;
}

function selectProvider(name) {
  selectedProvider = name;
  document.querySelectorAll(".provider").forEach((el) =>
    el.classList.toggle("active", el.dataset.name === name));
  $("model").value = CONFIG.providers[name].default_model;
  const apikey = $("apikey");
  apikey.value = keyStore.get(name);
  const info = CONFIG.providers[name];
  apikey.placeholder = info.configured
    ? "Server key configured - leave blank to use it"
    : `Paste your ${LABELS[name]} API key`;
}

function setupKeyField() {
  const apikey = $("apikey");
  apikey.addEventListener("input", () => {
    if ($("remember").checked) keyStore.set(selectedProvider, apikey.value.trim());
    refreshProviderStatus();
  });
  $("remember").addEventListener("change", () => {
    if ($("remember").checked) keyStore.set(selectedProvider, apikey.value.trim());
    else keyStore.clear(selectedProvider);
  });
  $("clearKey").addEventListener("click", () => {
    apikey.value = "";
    keyStore.clear(selectedProvider);
    refreshProviderStatus();
  });
  $("toggleKey").addEventListener("click", () => {
    const t = apikey.type === "password" ? "text" : "password";
    apikey.type = t;
    $("toggleKey").textContent = t === "password" ? "show" : "hide";
  });
}

function refreshProviderStatus() {
  document.querySelectorAll(".provider").forEach((el) => {
    const name = el.dataset.name;
    el.querySelector(".status").innerHTML = providerStatus(name, CONFIG.providers[name]);
  });
}

function providerUsable(name) {
  return CONFIG.providers[name].configured || !!$("apikey").value.trim() || !!keyStore.get(name);
}

function setupUpload() {
  const drop = $("drop");
  const input = $("file");
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => input.files[0] && uploadFile(input.files[0]));
  ["dragover", "dragenter"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (ev) => {
    const f = ev.dataTransfer.files[0];
    if (f) uploadFile(f);
  });
}

async function uploadFile(file) {
  $("dropText").textContent = "Uploading " + file.name + " ...";
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  if (!res.ok) {
    const e = await res.json().catch(() => ({}));
    $("dropText").textContent = "Upload failed: " + (e.detail || res.status);
    return;
  }
  const info = await res.json();
  fileId = info.file_id;
  $("dropText").textContent = "✓ " + info.filename;
  renderFileInfo(info);
  $("run").disabled = false;
}

function renderFileInfo(info) {
  const found = ["TITLE", "NOTE", "TAGS", "COLOR"].map((h) => {
    const ok = info.headers[h];
    return `<span class="pill ${ok ? "" : "miss"}">${h}${ok ? " = " + ok : " missing"}</span>`;
  }).join(" ");
  $("fileInfo").innerHTML =
    `<table>` +
    `<tr><td>Sheet</td><td class="v">${info.sheet_name}</td></tr>` +
    `<tr><td>Data rows</td><td class="v">${info.row_count}</td></tr>` +
    `<tr><td>Target columns</td><td class="v">${found}</td></tr>` +
    `</table>` +
    (info.missing_targets.length ? `<p class="err">Missing: ${info.missing_targets.join(", ")} (will be skipped)</p>` : "");
  $("fileInfo").classList.remove("hidden");
}

async function run() {
  if (!fileId) return;
  if (!selectedProvider || !providerUsable(selectedProvider)) {
    $("progressWrap").classList.remove("hidden");
    $("progressText").innerHTML =
      `<span class="err">No API key for ${LABELS[selectedProvider] || selectedProvider}. Paste your key above (or add it to .env).</span>`;
    return;
  }
  $("run").disabled = true;
  $("download").classList.add("hidden");
  $("reportsCard").classList.add("hidden");
  $("stats").classList.add("hidden");
  $("progressWrap").classList.remove("hidden");
  $("progressText").textContent = "Starting...";
  const body = {
    file_id: fileId,
    provider: selectedProvider,
    model: $("model").value.trim(),
    api_key: $("apikey").value.trim() || null,
    mode: $("mode").value,
    note_level: $("level").value,
    batch_size: +$("batch").value,
    concurrency: +$("conc").value,
    max_tokens: +$("maxtok").value,
    write_type: $("colType").checked,
    write_confidence: $("colConf").checked,
    write_review: $("colReview").checked,
  };
  const res = await fetch("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({}));
    $("progressText").innerHTML = `<span class="err">${e.detail || "Failed to start."}</span>`;
    $("run").disabled = false;
    return;
  }
  const { job_id } = await res.json();
  poll(job_id);
}

function poll(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const job = await (await fetch("/api/jobs/" + jobId)).json();
    updateProgress(job);
    if (job.status === "done" || job.status === "error") {
      clearInterval(pollTimer);
      finish(job, jobId);
    }
  }, 1500);
}

function updateProgress(job) {
  const pct = job.batches_total ? Math.round((job.batches_done / job.batches_total) * 100) : 0;
  $("bar").style.width = pct + "%";
  $("progressText").textContent =
    `${job.status} - batch ${job.batches_done}/${job.batches_total} - ` +
    `${job.processed_notes}/${job.total_notes} notes - ${job.cells_written} cells written`;
}

function finish(job, jobId) {
  $("run").disabled = false;
  if (job.status === "error") {
    $("progressText").innerHTML = `<span class="err">Error: ${job.error}</span>`;
    return;
  }
  $("stats").classList.remove("hidden");
  $("stats").innerHTML = [
    ["title_edits", "Titles"], ["tag_edits", "Tags"],
    ["color_edits", "Colours"], ["note_edits", "Notes"],
    ["cells_written", "Cells"], ["total_notes", "Notes seen"],
  ].map(([k, l]) => `<div class="stat"><div class="n">${job[k]}</div><div class="l">${l}</div></div>`).join("");
  const dl = $("download");
  dl.href = "/api/jobs/" + jobId + "/download";
  dl.classList.remove("hidden");
  renderReports(job);
}

function renderReports(job) {
  $("reportsCard").classList.remove("hidden");
  const rev = job.review || [];
  $("tab-review").innerHTML = rev.length
    ? `<p class="muted">${rev.length} rows flagged (MEDIUM/LOW)</p><table>` +
      `<tr><th>Row</th><th>Type</th><th>Title</th><th>Conf.</th></tr>` +
      rev.map((r) =>
        `<tr><td>${r.row}</td><td>${esc(r.type)}</td><td>${esc(r.title)}</td>` +
        `<td class="conf-${r.confidence}">${r.confidence}</td></tr>`).join("") +
      `</table>`
    : `<p class="muted">No rows flagged for review.</p>`;
  const pend = Object.entries(job.pending || {}).sort((a, b) => b[1].length - a[1].length);
  $("tab-pending").innerHTML = pend.length
    ? `<p class="muted">${pend.length} suggested tags (not applied automatically)</p><table>` +
      `<tr><th>Tag</th><th>Count</th><th>Rows</th></tr>` +
      pend.map(([t, rows]) =>
        `<tr><td>${esc(t)}</td><td>${rows.length}</td><td>${rows.slice(0, 25).join(", ")}${rows.length > 25 ? " ..." : ""}</td></tr>`).join("") +
      `</table>`
    : `<p class="muted">No pending tags.</p>`;
  const w = job.warnings || [];
  $("tab-warn").innerHTML = w.length
    ? `<ul class="bullets">${w.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
    : `<p class="muted">No warnings.</p>`;
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  ["review", "pending", "warn"].forEach((n) =>
    $("tab-" + n).classList.toggle("hidden", n !== name));
}

function esc(s) {
  return (s || "").toString().replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

init();
