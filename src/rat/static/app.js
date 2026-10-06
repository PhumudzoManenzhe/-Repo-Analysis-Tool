"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const number = new Intl.NumberFormat("en-US");
const percent = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

const state = {
  repositories: [],
  selectedId: null,
  metrics: [],
  query: new URLSearchParams(),
  source: "clone",
  sortKey: "churn",
  sortDirection: "desc",
  polling: false,
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") detail = payload.detail;
      if (Array.isArray(payload.detail)) {
        detail = payload.detail.map((item) => item.msg || "Invalid input").join("; ");
      }
    } catch (_) {
      // Keep the status-based message when the response is not JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

function showToast(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 3200);
}

function setHealth(online) {
  const health = $("#api-health");
  health.className = `health ${online ? "online" : "offline"}`;
  health.lastChild.textContent = online ? " API online" : " API unavailable";
}

async function checkHealth() {
  try {
    await api("/api/health");
    setHealth(true);
  } catch (_) {
    setHealth(false);
  }
}

async function loadRepositories(preferredId = state.selectedId) {
  try {
    state.repositories = await api("/api/repositories");
    renderRepositoryList();
    if (!state.repositories.length) {
      showWelcome();
      return;
    }
    const selected = state.repositories.find((repo) => repo.id === preferredId)
      || state.repositories[0];
    await selectRepository(selected.id);
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderRepositoryList() {
  const list = $("#repository-list");
  list.replaceChildren();
  if (!state.repositories.length) {
    const empty = document.createElement("div");
    empty.className = "sidebar-empty";
    empty.textContent = "No repositories yet.";
    list.append(empty);
    return;
  }

  for (const repository of state.repositories) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `repository-item${repository.id === state.selectedId ? " active" : ""}`;
    button.addEventListener("click", () => selectRepository(repository.id));

    const dot = document.createElement("i");
    dot.className = `repository-dot ${repository.status}`;
    const copy = document.createElement("span");
    copy.className = "repository-item-copy";
    const name = document.createElement("strong");
    name.textContent = repository.name;
    const source = document.createElement("small");
    source.textContent = `${repository.source_type} · ${repository.status}`;
    copy.append(name, source);
    const count = document.createElement("span");
    count.className = "repository-item-count";
    count.textContent = repository.commit_count ? number.format(repository.commit_count) : "—";
    button.append(dot, copy, count);
    list.append(button);
  }
}

function showWelcome() {
  state.selectedId = null;
  $("#welcome-panel").classList.remove("hidden");
  $("#repository-dashboard").classList.add("hidden");
  $("#page-title").textContent = "Repository overview";
  $("#page-subtitle").textContent = "Measure how code changes, who owns it, and where complexity accumulates.";
}

async function selectRepository(repositoryId) {
  state.selectedId = repositoryId;
  renderRepositoryList();
  const repository = state.repositories.find((item) => item.id === repositoryId);
  if (!repository) return;

  $("#welcome-panel").classList.add("hidden");
  $("#repository-dashboard").classList.remove("hidden");
  $("#page-title").textContent = repository.name;
  $("#page-subtitle").textContent = "Inspect ownership, growth, and change concentration across this repository.";
  $("#repository-name").textContent = repository.name;
  $("#repository-reference").textContent = repository.ref_sha || repository.source_ref;
  $("#repository-source").textContent = repository.source_type.toUpperCase();
  $("#repository-commits").textContent = number.format(repository.commit_count || 0);
  $("#repository-date").textContent = formatDate(repository.analysed_at || repository.created_at);

  const badge = $("#repository-status");
  badge.textContent = repository.status;
  badge.className = `status-badge ${repository.status}`;
  const notice = $("#repository-message");
  notice.classList.add("hidden");
  notice.classList.toggle("error", repository.status === "failed");

  if (repository.status !== "ready") {
    notice.textContent = repository.error
      || (repository.status === "failed" ? "Repository analysis failed." : "Repository analysis is still in progress.");
    notice.classList.remove("hidden");
    $("#metrics-content").classList.add("hidden");
    return;
  }

  $("#metrics-content").classList.remove("hidden");
  await loadMetrics();
}

function formatDate(value) {
  if (!value) return "—";
  const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

function buildMetricQuery() {
  const query = new URLSearchParams();
  const values = {
    object_type: $("#filter-object-type").value,
    path: $("#filter-path").value.trim(),
    author: $("#filter-author").value.trim(),
    since: $("#filter-since").value.trim(),
    until: $("#filter-until").value.trim(),
  };
  for (const [key, value] of Object.entries(values)) {
    if (value) query.set(key, value);
  }
  const commits = $("#filter-commits").value.trim().split(/[\s,]+/).filter(Boolean);
  for (const commit of commits) query.append("commits", commit);
  if (commits.length && (values.since || values.until)) {
    throw new Error("Commit hashes cannot be combined with a time range.");
  }
  return query;
}

async function loadMetrics() {
  if (!state.selectedId) return;
  setTableState("Loading metrics…");
  try {
    state.query = buildMetricQuery();
    const suffix = state.query.toString() ? `?${state.query}` : "";
    state.metrics = await api(`/api/repositories/${state.selectedId}/metrics${suffix}`);
    updateCsvLink();
    renderSummary();
    renderMetrics();
  } catch (error) {
    state.metrics = [];
    setTableState(error.message);
    showToast(error.message, true);
  }
}

function updateCsvLink() {
  const suffix = state.query.toString() ? `?${state.query}` : "";
  $("#csv-download").href = `/api/repositories/${state.selectedId}/metrics.csv${suffix}`;
}

function renderSummary() {
  const root = state.metrics.find((row) => (
    row.object_type === "repository" && row.path === "/" && row.author === "ALL"
  ));
  const aggregate = root || state.metrics.find((row) => row.author === "ALL") || state.metrics[0];
  $("#summary-commits").textContent = aggregate ? number.format(aggregate.commit_count) : "0";
  $("#summary-added").textContent = aggregate ? number.format(aggregate.added) : "0";
  $("#summary-removed").textContent = aggregate ? number.format(aggregate.removed) : "0";
  $("#summary-churn").textContent = aggregate ? number.format(aggregate.churn) : "0";
}

function renderMetrics() {
  const search = $("#table-search").value.trim().toLowerCase();
  let rows = state.metrics.filter((row) => (
    !search || `${row.path} ${row.object_type} ${row.author}`.toLowerCase().includes(search)
  ));
  rows = [...rows].sort(compareRows);
  $("#result-count").textContent = `${number.format(rows.length)} ${rows.length === 1 ? "row" : "rows"}`;

  const body = $("#metrics-table-body");
  body.replaceChildren();
  for (const row of rows) body.append(metricRow(row));
  setTableState(rows.length ? null : "No metrics match the current filters.");
}

function compareRows(left, right) {
  const a = left[state.sortKey];
  const b = right[state.sortKey];
  const order = typeof a === "number" ? (a ?? -Infinity) - (b ?? -Infinity)
    : String(a ?? "").localeCompare(String(b ?? ""));
  return state.sortDirection === "asc" ? order : -order;
}

function metricRow(metric) {
  const row = document.createElement("tr");
  row.append(
    cell(metric.path, "mono"),
    pillCell(metric.object_type),
    cell(metric.author),
    numericCell(metric.added, "value-positive"),
    numericCell(metric.removed, "value-negative"),
    numericCell(metric.growth, metric.growth >= 0 ? "value-positive" : "value-negative"),
    numericCell(metric.churn),
    numericCell(metric.modifications),
    cell(metric.ownership == null ? "—" : `${percent.format(metric.ownership * 100)}%`, "numeric"),
  );
  return row;
}

function cell(value, className = "") {
  const element = document.createElement("td");
  element.className = className;
  element.textContent = value;
  element.title = String(value);
  return element;
}

function numericCell(value, className = "") {
  return cell(number.format(value), `numeric ${className}`.trim());
}

function pillCell(value) {
  const element = document.createElement("td");
  const pill = document.createElement("span");
  pill.className = "type-pill";
  pill.textContent = value;
  element.append(pill);
  return element;
}

function setTableState(message) {
  const tableState = $("#table-state");
  tableState.textContent = message || "";
  tableState.classList.toggle("hidden", !message);
}

function openIngestion() {
  resetIngestionFeedback();
  $("#ingestion-dialog").showModal();
}

function selectSource(source) {
  state.source = source;
  $$(".source-tab").forEach((tab) => {
    const active = tab.dataset.source === source;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  $$(".source-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `source-${source}`));
}

function resetIngestionFeedback() {
  $("#ingestion-error").classList.add("hidden");
  $("#job-progress").classList.add("hidden");
  $("#job-progress-bar").style.width = "0%";
  setIngestionBusy(false);
}

function setIngestionBusy(busy) {
  $("#submit-ingestion").disabled = busy;
  $("#submit-ingestion").textContent = busy ? "Analysis running…" : "Start analysis";
  $$("#ingestion-dialog [value='cancel']").forEach((button) => { button.disabled = busy; });
}

async function submitIngestion() {
  resetIngestionFeedback();
  setIngestionBusy(true);
  try {
    if (state.source === "local") {
      const path = $("#local-path").value.trim();
      if (!path) throw new Error("Enter a local repository path.");
      const repository = await api("/api/repositories/local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, name: optionalValue("#local-name"), ref: valueOrHead("#local-ref") }),
      });
      $("#ingestion-dialog").close();
      showToast("Repository analysis complete.");
      await loadRepositories(repository.id);
      return;
    }

    const job = state.source === "zip" ? await submitZip() : await submitClone();
    localStorage.setItem("rat-active-job", JSON.stringify({ id: job.id, repositoryId: job.repository_id }));
    await pollJob(job.id, job.repository_id);
  } catch (error) {
    showIngestionError(error.message);
    setIngestionBusy(false);
  }
}

async function submitZip() {
  const file = $("#zip-file").files[0];
  if (!file) throw new Error("Choose a ZIP archive to upload.");
  const form = new FormData();
  form.append("file", file);
  const name = optionalValue("#zip-name");
  if (name) form.append("name", name);
  form.append("ref", valueOrHead("#zip-ref"));
  return api("/api/repositories/zip", { method: "POST", body: form });
}

function submitClone() {
  const url = $("#clone-url").value.trim();
  if (!url) throw new Error("Enter an HTTPS repository URL.");
  return api("/api/repositories/clone", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, name: optionalValue("#clone-name"), ref: valueOrHead("#clone-ref") }),
  });
}

function optionalValue(selector) {
  return $(selector).value.trim() || null;
}

function valueOrHead(selector) {
  return $(selector).value.trim() || "HEAD";
}

async function pollJob(jobId, repositoryId) {
  state.polling = true;
  $("#job-progress").classList.remove("hidden");
  while (state.polling) {
    const job = await api(`/api/jobs/${jobId}`);
    updateProgress(job);
    if (job.status === "succeeded") {
      localStorage.removeItem("rat-active-job");
      state.polling = false;
      $("#ingestion-dialog").close();
      showToast("Repository analysis complete.");
      await loadRepositories(repositoryId);
      return;
    }
    if (job.status === "failed") {
      localStorage.removeItem("rat-active-job");
      state.polling = false;
      throw new Error(job.error || "Repository ingestion failed.");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 650));
  }
}

function updateProgress(job) {
  const progress = Math.max(0, Math.min(100, job.progress || 0));
  $("#job-stage").textContent = job.stage || job.status;
  $("#job-percent").textContent = `${progress}%`;
  $("#job-progress-bar").style.width = `${progress}%`;
  $("#job-message").textContent = job.message || "Processing repository";
}

function showIngestionError(message) {
  const error = $("#ingestion-error");
  error.textContent = message;
  error.classList.remove("hidden");
}

async function resumeActiveJob() {
  const saved = localStorage.getItem("rat-active-job");
  if (!saved) return;
  try {
    const job = JSON.parse(saved);
    openIngestion();
    setIngestionBusy(true);
    await pollJob(job.id, job.repositoryId);
  } catch (error) {
    localStorage.removeItem("rat-active-job");
    showIngestionError(error.message);
    setIngestionBusy(false);
  }
}

function syncCommitFilters() {
  const hasCommits = Boolean($("#filter-commits").value.trim());
  const hasRange = Boolean($("#filter-since").value || $("#filter-until").value);
  $("#filter-since").disabled = hasCommits;
  $("#filter-until").disabled = hasCommits;
  $("#filter-commits").disabled = hasRange;
}

function resetFilters() {
  $("#metrics-filters").reset();
  syncCommitFilters();
  loadMetrics();
}

function bindEvents() {
  $("#open-ingestion").addEventListener("click", openIngestion);
  $$('[data-open-ingestion]').forEach((button) => button.addEventListener("click", openIngestion));
  $("#refresh-repositories").addEventListener("click", () => loadRepositories());
  $$(".source-tab").forEach((tab) => tab.addEventListener("click", () => selectSource(tab.dataset.source)));
  $("#submit-ingestion").addEventListener("click", submitIngestion);
  $("#ingestion-dialog").addEventListener("cancel", (event) => {
    if (state.polling) event.preventDefault();
  });
  $("#zip-file").addEventListener("change", (event) => {
    $("#zip-file-name").textContent = event.target.files[0]?.name || "A Git working tree with its .git directory";
  });
  $("#metrics-filters").addEventListener("submit", (event) => { event.preventDefault(); loadMetrics(); });
  $("#reset-filters").addEventListener("click", resetFilters);
  $("#filter-commits").addEventListener("input", syncCommitFilters);
  $("#filter-since").addEventListener("input", syncCommitFilters);
  $("#filter-until").addEventListener("input", syncCommitFilters);
  $("#table-search").addEventListener("input", renderMetrics);
  $$('[data-sort]').forEach((button) => button.addEventListener("click", () => {
    const key = button.dataset.sort;
    state.sortDirection = state.sortKey === key && state.sortDirection === "desc" ? "asc" : "desc";
    state.sortKey = key;
    renderMetrics();
  }));
}

async function initialize() {
  bindEvents();
  await Promise.all([checkHealth(), loadRepositories()]);
  await resumeActiveJob();
}

initialize();
