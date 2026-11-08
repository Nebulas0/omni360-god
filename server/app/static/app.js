const state = {
  kind: "disc",
  query: "",
  page: 1,
  pageSize: 50,
  total: 0,
  items: [],
  selected: new Set(),
  targetIds: new Set(),
  queueSort: "newest",
  queueFilter: "active",
  searchTimer: null,
  jobSpeed: new Map(),
  openDetails: new Set(),
  activeSources: new Set(),
};

const $ = (id) => document.getElementById(id);

function escapeText(value) {
  return value === null || value === undefined ? "" : String(value);
}

async function api(path, options = {}) {
  const { headers: optionHeaders = {}, ...requestOptions } = options;
  const response = await fetch(path, {
    ...requestOptions,
    headers: {
      "Accept": "application/json",
      "X-Requested-With": "XMLHttpRequest",
      ...optionHeaders,
    },
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.error || detail;
    } catch (_) {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

function setServiceStatus(text, tone = "") {
  $("service-status").textContent = text;
  $("service-dot").className = `status-dot ${tone}`;
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function stateLabel(item) {
  if (item.downloaded && item.status === "complete") return "downloaded";
  if (item.status === "complete") return "prepared";
  if (item.status === "working") return item.latest_job_stage || "working";
  if (item.status === "queued") return "queued";
  if (item.status === "failed") return "retry available";
  return "ready";
}

function renderStats(data) {
  const counts = {};
  for (const row of data.counts || []) {
    counts[`${row.source_kind}:${row.status}`] = Number(row.count || 0);
  }
  const discTotal = Object.entries(counts)
    .filter(([key]) => key.startsWith("disc:"))
    .reduce((sum, [, value]) => sum + value, 0);
  const digitalTotal = Object.entries(counts)
    .filter(([key]) => key.startsWith("digital:"))
    .reduce((sum, [, value]) => sum + value, 0);
  const complete = Object.entries(counts)
    .filter(([key]) => key.endsWith(":complete"))
    .reduce((sum, [, value]) => sum + value, 0);
  $("disc-count").textContent = formatCount(discTotal);
  $("digital-count").textContent = formatCount(digitalTotal);
  $("complete-count").textContent = formatCount(complete);
}

function renderSyncStatus(data) {
  renderStats(data);
  const sync = data.sync || [];
  const hasError = sync.some((row) => row.status === "error");
  const isSyncing = sync.some((row) => row.status === "syncing");
  if (!data.remote_configured) {
    setServiceStatus("Remote credentials are not configured", "error");
  } else if (hasError) {
    const error = sync.find((row) => row.status === "error");
    setServiceStatus(error?.error || "Remote catalog sync failed", "error");
  } else if (isSyncing || sync.length === 0) {
    setServiceStatus("Indexing remote catalog...", "");
  } else {
    setServiceStatus("Catalog ready", "ready");
  }
}

function renderCatalogMessage(message = "", error = false) {
  const element = $("catalog-message");
  element.textContent = message;
  element.className = `catalog-message${error ? " error" : ""}`;
}

function renderCatalog() {
  const list = $("catalog-list");
  list.replaceChildren();
  if (!state.items.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = "No matching material yet.<br><span>The remote index may still be syncing.</span>";
    list.appendChild(empty);
  } else {
    state.items.forEach((item, index) => {
      const row = document.createElement("div");
      row.className = "catalog-row";
      row.style.animationDelay = `${Math.min(index, 12) * 24}ms`;

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selected.has(item.id);
      checkbox.disabled = ["complete", "queued", "working"].includes(item.status) || item.downloaded;
      checkbox.setAttribute("aria-label", `Select ${item.name}`);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selected.add(item.id);
        else state.selected.delete(item.id);
        updateSelection();
      });

      const name = document.createElement("div");
      name.className = "catalog-name";
      const title = document.createElement("strong");
      title.textContent = item.name;
      const source = document.createElement("small");
      source.textContent = [item.source_path, item.release?.label].filter(Boolean).join(" · ");
      name.append(title, source);
      if (item.status === "complete" && item.latest_job_id) {
        const button = reprocessButton({ id: item.latest_job_id, name: item.name,
          source_path: item.source_path, status: "complete" }, true);
        button.classList.add("catalog-reprocess");
        name.appendChild(button);
      }

      const size = document.createElement("div");
      size.className = "catalog-size";
      const sizeText = document.createElement("strong");
      sizeText.textContent = item.human_size;
      const typeText = document.createElement("small");
      typeText.textContent = item.type || "archive";
      const badge = document.createElement("span");
      badge.className = `state-badge ${item.downloaded ? "downloaded" : item.status}`;
      if (item.downloaded && item.downloaded_at) {
        const when = new Date(item.downloaded_at).toLocaleDateString();
        badge.textContent = `downloaded ${when}`;
      } else {
        badge.textContent = stateLabel(item);
      }
      size.append(sizeText, typeText, badge);

      row.append(checkbox, name, size);
      list.appendChild(row);
    });
  }

  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("page-label").textContent = `Page ${state.page} of ${pageCount}`;
  $("previous-page").disabled = state.page <= 1;
  $("next-page").disabled = state.page >= pageCount;
  updateSelection();
}

function updateSelection() {
  $("selected-count").textContent = state.selected.size;
  $("queue-button").disabled = state.selected.size === 0;
}

function renderStorage(data) {
  const filesystem = data.filesystem || {};
  const application = data.application || {};
  const total = Number(filesystem.total || 0);
  const used = Number(filesystem.used || 0);
  const percentage = total ? Math.min(100, (used / total) * 100) : 0;
  $("storage-free").textContent = `${filesystem.human_free || "—"} free`;
  $("storage-used").textContent = `Used ${filesystem.human_used || "—"}`;
  $("storage-total").textContent = `of ${filesystem.human_total || "—"}`;
  $("storage-bar-fill").style.width = `${percentage.toFixed(1)}%`;
  $("storage-detail").textContent = `GOD output ${application.human_output || "—"} · work/cache ${application.human_work || "—"} · app total ${application.human_total || "—"}`;
}

async function loadStorage() {
  try {
    renderStorage(await api("/api/storage"));
  } catch (error) {
    $("storage-detail").textContent = `Storage unavailable: ${error.message}`;
  }
}

async function loadState() {
  try {
    const data = await api("/api/state");
    renderSyncStatus(data);
  } catch (error) {
    setServiceStatus(error.message, "error");
  }
}

async function loadCatalog() {
  renderCatalogMessage("Loading this collection...");
  try {
    const params = new URLSearchParams({
      kind: state.kind,
      q: state.query,
      page: state.page,
      page_size: state.pageSize,
    });
    const data = await api(`/api/catalog?${params}`);
    state.items = data.items || [];
    state.total = Number(data.total || 0);
    renderCatalog();
    renderCatalogMessage(
      state.total ? `${formatCount(state.total)} items in this collection` : "No indexed items yet."
    );
  } catch (error) {
    state.items = [];
    state.total = 0;
    renderCatalog();
    renderCatalogMessage(error.message, true);
  }
}

function setKind(kind) {
  state.kind = kind;
  state.page = 1;
  document.querySelectorAll(".catalog-tab").forEach((tab) => {
    const active = tab.dataset.kind === kind;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  loadCatalog();
}

const STAGE_LABELS = {
  queued: "Waiting",
  starting: "Starting",
  downloading: "Downloading",
  extracting: "Extracting",
  converting: "Converting",
  organizing: "Organizing",
  unlocking: "Unlocking",
  packaging: "Packaging",
  complete: "Ready to download",
  checking: "Checking files",
  failed: "Failed",
};

const STATUS_PILLS = {
  queued: { label: "Queued", tone: "queued" },
  working: { label: "Working", tone: "working" },
  complete: { label: "Ready to download", tone: "complete" },
  failed: { label: "Failed", tone: "failed" },
};

function formatBytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let i = 0;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatEta(seconds) {
  if (!seconds || !isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function computeSpeedEta(job) {
  const now = Date.now();
  const prev = state.jobSpeed.get(job.id);
  const done = job.bytes_done || 0;
  const total = job.bytes_total || 0;
  if (job.stage !== "downloading" || !total) {
    state.jobSpeed.delete(job.id);
    return null;
  }
  if (!prev) {
    state.jobSpeed.set(job.id, { time: now, done });
    return null;
  }
  const elapsed = (now - prev.time) / 1000;
  if (elapsed < 1) return null;
  const delta = done - prev.done;
  state.jobSpeed.set(job.id, { time: now, done });
  if (delta <= 0) return { speed: 0, eta: null, done, total };
  const speed = delta / elapsed;
  const remaining = total - done;
  const eta = speed > 0 ? remaining / speed : null;
  return { speed, eta, done, total };
}

function shortStage(job) {
  if (job.error) return job.error;
  const stage = STAGE_LABELS[job.stage] || job.stage || job.status;
  if (job.status === "complete" && job.message && job.message.startsWith("Complete:")) {
    return stage;
  }
  return stage;
}

function detailPanel(key, label) {
  const details = document.createElement("details");
  details.className = "job-details";
  details.open = state.openDetails.has(key);
  details.addEventListener("toggle", () => {
    if (details.open) state.openDetails.add(key);
    else state.openDetails.delete(key);
  });
  const summary = document.createElement("summary");
  summary.textContent = label;
  details.appendChild(summary);
  return details;
}

function jobNotice(message, error = false) {
  const element = $("job-notice");
  element.textContent = message;
  element.className = `job-notice${error ? " error" : ""}`;
}

function reprocessButton(job, compact = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "job-action reprocess";
  button.textContent = compact ? "Reprocess" : job.status === "failed" ? "Retry with current settings" : "Reprocess with current settings";
  button.disabled = state.activeSources.has(job.source_path);
  button.title = `Run ${job.name} again. Saved job files are kept.`;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/jobs/${job.id}/reprocess`, { method: "POST" });
      state.queueFilter = "active";
      $("queue-filter").value = "active";
      jobNotice(`Queued ${job.name} with current settings. Previous job files are kept.`);
      await Promise.all([loadJobs(), loadCatalog()]);
    } catch (error) {
      button.disabled = false;
      jobNotice(error.message, true);
    }
  });
  return button;
}

function renderChecks(report, key) {
  const area = document.createElement("div");
  area.className = "bundle-checks";
  if (!report) {
    const note = document.createElement("p");
    note.className = "legacy-note";
    note.textContent = "Checks were not recorded for this older bundle. Reprocess a source to record them.";
    area.appendChild(note);
    return area;
  }
  const list = document.createElement("ul");
  list.className = "check-list";
  for (const check of report.checks || []) {
    const row = document.createElement("li");
    row.className = `check-row check-${check.state}`;
    const icon = document.createElement("span");
    icon.className = "check-icon";
    icon.textContent = check.state === "passed" ? "✓" : check.state === "warning" ? "!" : "–";
    icon.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    const label = document.createElement("strong");
    label.textContent = check.label;
    const detail = document.createElement("small");
    detail.textContent = check.detail;
    text.append(label, detail);
    row.append(icon, text);
    list.appendChild(row);
  }
  area.appendChild(list);
  if ((report.warnings || []).length) {
    const warning = document.createElement("div");
    warning.className = "bundle-warning";
    const label = document.createElement("strong");
    label.textContent = "Ready with warnings";
    warning.appendChild(label);
    for (const message of report.warnings) {
      const p = document.createElement("p");
      p.textContent = message;
      warning.appendChild(p);
    }
    area.appendChild(warning);
  }
  const updates = (report.titles || []).flatMap((title) =>
    (title.updates || []).map((update) => ({ title: title.name, ...update })));
  if (updates.length) {
    const details = detailPanel(`${key}-updates`, "Title update details");
    for (const update of updates) {
      const line = document.createElement("p");
      line.textContent = `${update.title}: ${update.message}`;
      details.appendChild(line);
    }
    area.appendChild(details);
  }
  return area;
}

function renderJobCard(job, history = [job]) {
  const item = document.createElement("article");
  item.className = `job ${job.status}`;
  const combined = (job.downloads || []).find((d) => d.kind === "combined");
  const complete = job.status === "complete";
  const key = combined?.name || `job-${job.id}`;
  const hasWarnings = Boolean(job.report?.warnings?.length);
  if (hasWarnings) item.classList.add("has-warnings");

  const header = document.createElement("div");
  header.className = "job-header";
  const pill = document.createElement("span");
  const pillDef = STATUS_PILLS[job.status] || STATUS_PILLS.queued;
  pill.className = `status-pill ${hasWarnings ? "warning" : pillDef.tone}`;
  pill.textContent = hasWarnings ? "Ready with warnings" : pillDef.label;
  header.appendChild(pill);
  if (!complete) {
    const percent = document.createElement("span");
    percent.className = "job-percent";
    percent.textContent = `${Math.round(job.progress || 0)}%`;
    header.appendChild(percent);
  }
  const title = document.createElement("h3");
  title.className = "job-title";
  title.textContent = complete && combined
    ? (job.report?.titles || []).map((t) => t.name).join(" + ") || combined.name.replace(/\s*\[[^\]]+\]\.zip$/, "")
    : job.name;
  const subtitle = document.createElement("p");
  subtitle.className = "job-subtitle";
  subtitle.textContent = complete ? "Latest bundle · current files for this title" : [job.source_kind, job.release?.label].filter(Boolean).join(" · ");
  item.append(header, title, subtitle);

  if (!complete) {
    const stage = document.createElement("p");
    stage.className = "job-stage";
    const speed = computeSpeedEta(job);
    stage.textContent = shortStage(job);
    if (speed?.speed > 0) stage.textContent += ` · ${formatBytes(speed.speed)}/s · ETA ${formatEta(speed.eta)}`;
    const progress = document.createElement("div");
    progress.className = "job-progress";
    const bar = document.createElement("span");
    bar.style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
    progress.appendChild(bar);
    item.append(stage, progress);
  }

  if (complete) {
    item.appendChild(renderChecks(job.report, key));
    if (combined) {
      const link = document.createElement("a");
      link.className = "bundle-download";
      link.href = combined.url;
      link.download = "";
      const label = document.createElement("strong");
      label.textContent = "Download latest game bundle ↗";
      const filename = document.createElement("small");
      filename.textContent = combined.name;
      link.append(label, filename);
      item.appendChild(link);
    }
    const guide = document.createElement("a");
    guide.className = "install-guide-link";
    guide.href = job.install_guide_url;
    guide.download = "";
    guide.textContent = "↓ Xbox installation guide";
    item.appendChild(guide);

    const previous = detailPanel(`${key}-history`, `Job history & saved files (${history.length})`);
    const note = document.createElement("p");
    note.textContent = "Saved files reflect each job when it ran. An add-on job may contain only that add-on. Use the latest bundle above for installation.";
    previous.appendChild(note);
    for (const source of history) {
      const row = document.createElement("div");
      row.className = "history-job";
      const name = document.createElement("strong");
      name.textContent = source.name;
      const when = document.createElement("small");
      const setting = source.job_report?.unlock_enabled;
      when.textContent = `${new Date(source.created_at).toLocaleString()} · Unlock ${setting == null ? "not recorded" : setting ? "on" : "off"}`;
      row.append(name, when);
      const snapshot = (source.downloads || []).find((d) => d.kind === "snapshot");
      if (snapshot) {
        const link = document.createElement("a");
        link.href = snapshot.url;
        link.download = "";
        link.textContent = "Download files saved by this job";
        row.appendChild(link);
      }
      if (source.can_reprocess) row.appendChild(reprocessButton(source));
      previous.appendChild(row);
    }
    item.appendChild(previous);
  }

  const footer = document.createElement("div");
  footer.className = "job-footer";
  if (complete) {
    const marked = document.createElement("button");
    marked.className = "job-action confirm";
    marked.type = "button";
    marked.textContent = job.download_confirmed ? "Downloaded ✓" : "Mark downloaded";
    marked.disabled = Boolean(job.download_confirmed);
    marked.addEventListener("click", () => markDownloaded(job));
    footer.appendChild(marked);
  } else if (job.can_reprocess) footer.appendChild(reprocessButton(job));
  if (["complete", "failed"].includes(job.status)) {
    const remove = document.createElement("button");
    remove.className = "job-action delete";
    remove.type = "button";
    remove.textContent = complete ? "Delete bundle…" : "Delete job…";
    remove.addEventListener("click", () => deleteJob(job));
    footer.appendChild(remove);
  }
  item.appendChild(footer);
  return item;
}

function renderJobs(jobs) {
  const list = $("job-list");
  list.replaceChildren();
  state.activeSources = new Set(jobs.filter((j) => ["queued", "working"].includes(j.status)).map((j) => j.source_path));
  if (!jobs.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = "Your downloads will appear here.<br><span>Select a source and choose Queue selected.</span>";
    list.appendChild(empty);
    return;
  }
  for (const job of jobs.filter((j) => j.status !== "complete")) list.appendChild(renderJobCard(job));
  const groups = new Map();
  for (const job of jobs.filter((j) => j.status === "complete")) {
    const key = (job.downloads || []).find((d) => d.kind === "combined")?.name || `job-${job.id}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(job);
  }
  for (const history of groups.values()) {
    history.sort((a, b) => b.id - a.id);
    list.appendChild(renderJobCard(history[0], history));
  }
}

async function loadJobs() {
  try {
    const data = await api(`/api/jobs?limit=30&sort=${encodeURIComponent(state.queueSort)}&filter=${encodeURIComponent(state.queueFilter)}`);
    renderJobs(data.jobs || []);
    await loadState();
  } catch (error) {
    jobNotice(`Could not refresh jobs: ${error.message}`, true);
  }
}

async function loadTargets() {
  try {
    const data = await api("/api/targets");
    const options = $("target-options");
    options.replaceChildren();
    if (!(data.targets || []).length) {
      const empty = document.createElement("span");
      empty.className = "target-empty";
      empty.textContent = "No existing title bundles yet.";
      options.appendChild(empty);
      return;
    }
    for (const target of data.targets) {
      const label = document.createElement("label");
      label.className = "target-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = target.title_id;
      input.checked = state.targetIds.has(target.title_id);
      input.addEventListener("change", () => {
        if (input.checked) state.targetIds.add(input.value);
        else state.targetIds.delete(input.value);
      });
      const text = document.createElement("span");
      text.textContent = `${target.title_name} [${target.title_id}]`;
      label.append(input, text);
      options.appendChild(label);
    }
  } catch (error) {
    renderCatalogMessage(`Could not load destinations: ${error.message}`, true);
  }
}

async function markDownloaded(job) {
  try {
    await api(`/api/jobs/${job.id}/download-confirmed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ downloaded: true }),
    });
    await loadJobs();
  } catch (error) {
    renderCatalogMessage(`Could not mark download: ${error.message}`, true);
  }
}

async function deleteJob(job) {
  const shared = job.shared_output_count > 1
    ? ` This combined archive is shared by ${job.shared_output_count} jobs, so it will disappear from all of them.`
    : "";
  if (!window.confirm(`Delete generated files for ${job.name}?${shared}`)) return;
  const deleteTitleDir = window.confirm(
    "Also delete the generated title directory containing the GOD/DLC files? This cannot be undone."
  );
  const deleteWork = window.confirm(
    "Also delete this job's downloaded source, snapshot, and work files?"
  );
  try {
    await api(`/api/jobs/${job.id}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirm: true,
        delete_output: true,
        delete_title_dir: deleteTitleDir,
        delete_work: deleteWork,
      }),
    });
    await Promise.all([loadJobs(), loadState(), loadCatalog(), loadTargets()]);
  } catch (error) {
    renderCatalogMessage(`Delete failed: ${error.message}`, true);
  }
}

async function queueSelected() {
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  $("queue-button").disabled = true;
  renderCatalogMessage(`Adding ${ids.length} item${ids.length === 1 ? "" : "s"} to the forge...`);
  try {
    const data = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        item_ids: ids,
        target_title_ids: Array.from(state.targetIds),
      }),
    });
    state.selected.clear();
    const queued = (data.jobs || []).filter((job) => job.status === "queued").length;
    renderCatalogMessage(`${queued} job${queued === 1 ? "" : "s"} queued. The worker will keep going after you leave this page.`);
    await Promise.all([loadCatalog(), loadJobs()]);
  } catch (error) {
    renderCatalogMessage(error.message, true);
    updateSelection();
  }
}

async function refreshCatalog() {
  try {
    await api("/api/catalog/refresh", { method: "POST" });
    renderCatalogMessage("Remote catalog refresh started. Existing items remain selectable.");
    setServiceStatus("Refreshing remote catalog...", "");
  } catch (error) {
    renderCatalogMessage(error.message, true);
  }
}

$("queue-button").addEventListener("click", queueSelected);
$("prepare-xbox-button").addEventListener("click", () => {
  const releases=[...state.selected].map(id=>`catalog-${id}`);
  if(releases.length>32){renderCatalogMessage("Select at most 32 sources to review for Xbox preparation.",true);return;}
  window.location.href="/consoles/prepare"+(releases.length?"?releases="+encodeURIComponent(releases.join(",")):"");
});
$("refresh-button").addEventListener("click", refreshCatalog);
$("clean-deleted-work").addEventListener("click", async () => {
  if (!window.confirm("Delete work directories and orphaned source-cache files for all deleted jobs? This cannot be undone.")) return;
  try {
    const result = await api("/api/cleanup/deleted-work", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    renderCatalogMessage(`${result.deleted.length} deleted work paths removed.`);
    await Promise.all([loadStorage(), loadJobs(), loadTargets()]);
  } catch (error) {
    renderCatalogMessage(`Cleanup failed: ${error.message}`, true);
  }
});

$("queue-sort").addEventListener("change", (event) => {
  state.queueSort = event.target.value;
  loadJobs();
});
$("queue-filter").addEventListener("change", (event) => {
  state.queueFilter = event.target.value;
  loadJobs();
});

$("targets-button").addEventListener("click", async () => {
  const panel = $("target-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) await loadTargets();
});

$("select-page-button").addEventListener("click", () => {
  const selectable = state.items.filter((item) => !["complete", "queued", "working"].includes(item.status) && !item.downloaded);
  const allSelected = selectable.length > 0 && selectable.every((item) => state.selected.has(item.id));
  selectable.forEach((item) => {
    if (allSelected) state.selected.delete(item.id);
    else state.selected.add(item.id);
  });
  renderCatalog();
});
document.querySelectorAll(".catalog-tab").forEach((tab) => {
  tab.addEventListener("click", () => setKind(tab.dataset.kind));
});
$("previous-page").addEventListener("click", () => {
  if (state.page > 1) {
    state.page -= 1;
    loadCatalog();
  }
});
$("next-page").addEventListener("click", () => {
  if (state.page < Math.ceil(state.total / state.pageSize)) {
    state.page += 1;
    loadCatalog();
  }
});
$("search-input").addEventListener("input", (event) => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => {
    state.query = event.target.value.trim();
    state.page = 1;
    loadCatalog();
  }, 260);
});

function setSettingsStatus(message, error = false) {
  const element = $("settings-status");
  element.textContent = message;
  element.className = `settings-status${error ? " error" : ""}`;
}

function setUnlockSettingsStatus(message, error = false) {
  const element = $("unlock-settings-status");
  element.textContent = message;
  element.className = `settings-status${error ? " error" : ""}`;
}

$("unlock-packages-input").addEventListener("change", async (event) => {
  const input = event.target;
  const enabled = input.checked;
  input.disabled = true;
  setUnlockSettingsStatus("Saving...");
  try {
    const data = await api("/api/settings/unlock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unlock_packages: enabled }),
    });
    input.checked = data.unlock_packages;
    setUnlockSettingsStatus(data.message);
  } catch (error) {
    input.checked = !enabled;
    setUnlockSettingsStatus(error.message, true);
  } finally {
    input.disabled = false;
  }
});

let settingsAuthMode = "api-key";

function setSettingsAuthMode(mode) {
  settingsAuthMode = mode === "basic" ? "basic" : "api-key";
  document.querySelectorAll(".settings-tab").forEach((tab) => {
    const active = tab.dataset.auth === settingsAuthMode;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("settings-api-key").hidden = settingsAuthMode !== "api-key";
  $("settings-basic").hidden = settingsAuthMode !== "basic";
}

async function loadSettings() {
  $("unlock-packages-input").disabled = true;
  try {
    const data = await api("/api/settings");
    $("unlock-packages-input").checked = data.unlock_packages;
    $("unlock-packages-input").disabled = false;
    setUnlockSettingsStatus(`XBLA/DLC unlock ${data.unlock_packages ? "enabled" : "disabled"} for new jobs.`);
    const parts = [];
    if (data.api_key_configured) parts.push("API key configured");
    else parts.push("No API key");
    if (data.basic_configured) parts.push(`Basic Auth: ${data.basic_username}`);
    setSettingsAuthMode(data.auth_mode);
    parts.push(`active: ${data.auth_mode === "basic" ? "Basic Auth" : "X-API-Key"}`);
    setSettingsStatus(parts.join(" · "));
  } catch (error) {
    setSettingsStatus(error.message, true);
    setUnlockSettingsStatus("Could not load unlock setting. Reopen Settings to retry.", true);
  }
}

document.querySelectorAll(".settings-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    setSettingsAuthMode(tab.dataset.auth);
  });
});

$("settings-button").addEventListener("click", async () => {
  const panel = $("settings-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) {
    await loadSettings();
    $("unlock-packages-input").focus({ preventScroll: true });
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
});
$("settings-cancel").addEventListener("click", () => {
  $("settings-panel").hidden = true;
  $("api-key-input").value = "";
  $("basic-user-input").value = "";
  $("basic-password-input").value = "";
});

$("settings-test").addEventListener("click", async () => {
  const payload = { auth_mode: settingsAuthMode };
  const apiKey = $("api-key-input").value.trim();
  const basicUser = $("basic-user-input").value.trim();
  const basicPassword = $("basic-password-input").value;
  if (apiKey) payload.api_key = apiKey;
  if (basicUser) payload.basic_user = basicUser;
  if (basicPassword) payload.basic_password = basicPassword;
  if (!Object.keys(payload).length) {
    setSettingsStatus("Enter credentials to test first.", true);
    return;
  }
  setSettingsStatus("Testing connection...");
  try {
    const data = await api("/api/settings/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (data.ok) {
      setSettingsStatus(`✓ Connection successful (HTTP ${data.status}, ${data.auth_mode})`);
    } else {
      setSettingsStatus(`✗ ${data.error || "Connection failed"} (HTTP ${data.status || "n/a"})`, true);
    }
  } catch (error) {
    setSettingsStatus(error.message, true);
  }
});

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = { auth_mode: settingsAuthMode };
  const apiKey = $("api-key-input").value.trim();
  const basicUser = $("basic-user-input").value.trim();
  const basicPassword = $("basic-password-input").value;
  if (apiKey) payload.api_key = apiKey;
  if (basicUser || basicPassword) {
    payload.basic_user = basicUser;
    payload.basic_password = basicPassword;
  }
  if (!Object.keys(payload).length) {
    setSettingsStatus("Enter credentials to save first.", true);
    return;
  }
  setSettingsStatus("Saving credentials and restarting catalog sync...");
  try {
    const data = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("api-key-input").value = "";
    $("basic-user-input").value = "";
    $("basic-password-input").value = "";
    setSettingsStatus(`${data.message}. Current values are not shown.`);
    setServiceStatus("Refreshing remote catalog...", "");
    await Promise.all([loadState(), loadCatalog(), loadJobs(), loadTargets()]);
  } catch (error) {
    setSettingsStatus(error.message, true);
  }
});

let jobPollTimer = null;
let lastJobCount = 0;

async function pollJobs() {
  await loadJobs();
  const active = document.querySelectorAll("#job-list .job.working, #job-list .job.queued, #job-list .job.failed").length;
  lastJobCount = active;
  const interval = active > 0 ? 3500 : 15000;
  if (jobPollTimer) clearTimeout(jobPollTimer);
  jobPollTimer = setTimeout(pollJobs, interval);
}

(async function boot() {
  await Promise.all([loadState(), loadCatalog(), loadJobs(), loadTargets(), loadStorage()]);
  pollJobs();
  window.setInterval(loadStorage, 15000);
  window.setInterval(async () => {
    await loadState();
    if (!state.items.length) await loadCatalog();
  }, 8000);
})();

$("install-help-button").addEventListener("click", () => $("install-dialog").showModal());
$("install-dialog-close").addEventListener("click", () => $("install-dialog").close());
