"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const number = new Intl.NumberFormat("en-US");
const percent = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

const state = {
  repositories: [],
  selectedId: null,
  metrics: [],
  authors: [],
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
  $("#manage-authors").disabled = repository.status !== "ready";
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
  await Promise.all([loadMetrics(), loadAuthors()]);
}

async function loadAuthors() {
  state.authors = await api(`/api/repositories/${state.selectedId}/authors`);
  const suggestions = $("#author-suggestions");
  suggestions.replaceChildren();
  const names = new Set(
    state.authors.map((author) => author.canonical_display_name || author.display_name),
  );
  for (const name of names) {
    const option = document.createElement("option");
    option.value = name;
    suggestions.append(option);
  }
}

function formatDate(value) {
  if (!value) return "—";
  const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

function unixTimestamp(selector) {
  const value = $(selector).value;
  if (!value) return "";
  return String(Math.floor(new Date(value).getTime() / 1000));
}

function buildMetricQuery() {
  const query = new URLSearchParams();
  const values = {
    object_type: $("#filter-object-type").value,
    path: $("#filter-path").value.trim(),
    author: $("#filter-author").value.trim(),
    since: unixTimestamp("#filter-since"),
    until: unixTimestamp("#filter-until"),
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
  renderVisualizations(aggregate);
}

function renderVisualizations(aggregate) {
  const added = aggregate?.added || 0;
  const removed = aggregate?.removed || 0;
  const churn = added + removed;
  const addedShare = churn ? (added / churn) * 100 : 50;
  $("#churn-added").style.width = `${addedShare}%`;
  $("#churn-removed").style.width = `${100 - addedShare}%`;
  $("#churn-balance").textContent = churn
    ? `${percent.format(addedShare)}% additions`
    : "No line churn";

  const chart = $("#ownership-chart");
  chart.replaceChildren();
  if (!aggregate) return;
  const authors = state.metrics
    .filter((row) => (
      row.author !== "ALL"
      && row.object_type === aggregate.object_type
      && row.path === aggregate.path
    ))
    .sort((left, right) => (right.ownership || 0) - (left.ownership || 0))
    .slice(0, 4);
  if (!authors.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "No author churn in this selection.";
    chart.append(empty);
    return;
  }
  for (const author of authors) {
    const row = document.createElement("div");
    row.className = "ownership-row";
    const name = document.createElement("span");
    name.className = "ownership-name";
    name.textContent = author.author;
    name.title = author.author;
    const track = document.createElement("span");
    track.className = "ownership-track";
    const bar = document.createElement("i");
    bar.style.width = `${(author.ownership || 0) * 100}%`;
    track.append(bar);
    const value = document.createElement("span");
    value.className = "ownership-value";
    value.textContent = `${percent.format((author.ownership || 0) * 100)}%`;
    row.append(name, track, value);
    chart.append(row);
  }
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

async function openAuthors() {
  if (!state.selectedId) return;
  $("#author-error").classList.add("hidden");
  $("#authors-dialog").showModal();
  try {
    state.authors = await api(`/api/repositories/${state.selectedId}/authors`);
    renderAuthors();
  } catch (error) {
    showAuthorError(error.message);
  }
}

function renderAuthors() {
  $("#author-count").textContent = `${state.authors.length} ${state.authors.length === 1 ? "identity" : "identities"}`;
  const source = $("#source-author");
  const previousSource = Number(source.value);
  source.replaceChildren();
  for (const author of state.authors) source.append(authorOption(author));
  if (state.authors.some((author) => author.id === previousSource)) {
    source.value = String(previousSource);
  }
  renderCanonicalAuthors();

  const list = $("#author-list");
  list.replaceChildren();
  for (const author of state.authors) {
    const row = document.createElement("div");
    row.className = "author-row";
    const identity = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = author.display_name;
    const detail = document.createElement("small");
    detail.textContent = `${number.format(author.commit_count)} commits`;
    identity.append(title, detail);
    const mapping = document.createElement("span");
    mapping.className = "merge-tag";
    mapping.textContent = author.canonical_display_name
      ? `→ ${author.canonical_display_name}`
      : "Canonical";
    row.append(identity, mapping);
    if (author.canonical_author_id) {
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "unmerge-button";
      undo.textContent = "Undo";
      undo.addEventListener("click", () => unmergeAuthor(author.id));
      row.append(undo);
    } else {
      row.append(document.createElement("span"));
    }
    list.append(row);
  }
  $("#merge-author").disabled = state.authors.length < 2;
}

function renderCanonicalAuthors() {
  const sourceId = Number($("#source-author").value);
  const canonical = $("#canonical-author");
  canonical.replaceChildren();
  for (const author of state.authors) {
    if (author.id !== sourceId && !author.canonical_author_id) {
      canonical.append(authorOption(author));
    }
  }
}

function authorOption(author) {
  const option = document.createElement("option");
  option.value = author.id;
  option.textContent = `${author.display_name} · ${author.commit_count} commits`;
  return option;
}

async function mergeAuthor() {
  const sourceId = Number($("#source-author").value);
  const canonicalId = Number($("#canonical-author").value);
  if (!sourceId || !canonicalId) {
    showAuthorError("Select two different author identities.");
    return;
  }
  try {
    state.authors = await api(`/api/repositories/${state.selectedId}/author-merges`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_author_id: sourceId, canonical_author_id: canonicalId }),
    });
    $("#author-error").classList.add("hidden");
    renderAuthors();
    await loadMetrics();
    showToast("Author identities merged.");
  } catch (error) {
    showAuthorError(error.message);
  }
}

async function unmergeAuthor(sourceId) {
  try {
    state.authors = await api(
      `/api/repositories/${state.selectedId}/author-merges/${sourceId}`,
      { method: "DELETE" },
    );
    renderAuthors();
    await loadMetrics();
    showToast("Author merge removed.");
  } catch (error) {
    showAuthorError(error.message);
  }
}

function showAuthorError(message) {
  const error = $("#author-error");
  error.textContent = message;
  error.classList.remove("hidden");
}

function bindEvents() {
  $("#open-ingestion").addEventListener("click", openIngestion);
  $$('[data-open-ingestion]').forEach((button) => button.addEventListener("click", openIngestion));
  $("#manage-authors").addEventListener("click", openAuthors);
  $("#close-authors").addEventListener("click", () => $("#authors-dialog").close());
  $("#source-author").addEventListener("change", renderCanonicalAuthors);
  $("#merge-author").addEventListener("click", mergeAuthor);
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
