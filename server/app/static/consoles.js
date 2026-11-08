"use strict";
const node = id => document.getElementById(id);
const state = {cursor: "0", loading: false, reload: false};
function el(tag, className = "", text) {
  const item = document.createElement(tag); item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
function bytes(value) {
  let n = Number(value || 0), i = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}
const date = value => value ? new Date(value).toLocaleString() : "No contact yet";
async function call(path, data) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {method: data === undefined ? "GET" : "POST", signal: controller.signal, cache: "no-store",
      headers: {"Accept": "application/json", "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
      ...(data === undefined ? {} : {body: JSON.stringify(data)})});
    if (!(response.headers.get("Content-Type") || "").includes("application/json")) throw new Error("Sign in again, then refresh this page.");
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  } finally { clearTimeout(timer); }
}
function action(label, path, data, confirmation, message, danger = false) {
  const button = el("button", danger ? "button-danger" : "button-secondary", label); button.type = "button";
  button.onclick = async () => {
    if (confirmation && !window.confirm(confirmation)) return;
    button.disabled = true;
    try {
      const result = await call(path, data);
      node("notice").textContent = message + (result.released_bytes ? ` ${bytes(result.released_bytes)} removed.` : "");
      await load(true);
    } catch (error) { node("notice").textContent = error.name === "AbortError" ? "Request timed out. Refresh to check its result." : error.message; }
    finally { button.disabled = false; }
  };
  return button;
}
function renderDevices(devices) {
  const selected = node("console-filter").value;
  const all = el("option", "", "All consoles"); all.value = "";
  node("console-filter").replaceChildren(all); node("devices").replaceChildren();
  for (const device of devices) {
    const option = el("option", "", device.name + (device.hidden ? " · removed" : "")); option.value = device.id; node("console-filter").append(option);
    const card = el("article", "device-row"), text = el("div", "device-text");
    text.append(el("strong", "", device.name), el("p", "muted", device.revoked ? (device.hidden ? "Removed entry · access revoked" : "Access revoked") : `Last contact: ${date(device.last_seen)}`));
    card.append(text);
    if (!device.revoked) card.append(action("Revoke access", `/api/consoles/${device.id}/revoke`, {}, `Revoke access for ${device.name}? It will need pairing again.`, "Console access revoked."));
    else if (!device.hidden) card.append(action("Remove entry", `/api/consoles/${device.id}/remove`, {}, null, "Revoked console removed from this list."));
    node("devices").append(card);
  }
  if (!devices.length) node("devices").append(el("p", "empty", "No consoles paired yet. Enter a pairing code below to get started."));
  if (!devices.some(d => !d.revoked)) document.querySelector('.pair-panel').open = true;
  node("console-filter").value = devices.some(d => d.id === selected) ? selected : "";
}
function renderRequest(request, expanded) {
  const installed = request.installation_status === "installed", removed = request.error_code === "FILES_REMOVED";
  const preparing = ["queued", "waiting", "preparing"].includes(request.state), transfer = request.transfer;
  const failed = request.installation_status === "failed" || request.state === "failed";
  const status = installed ? "Installed on Xbox" : removed ? "Download removed" : failed ? "Needs attention" : request.installation_status === "cancelled" ? "Paused on Xbox" : preparing ? "Preparing on server" : request.state === "expired" ? "Download expired" : transfer ? "Awaiting Xbox result" : "Ready for Xbox";
  const card = el("article", "request-card"), top = el("div", "request-top"), heading = el("div", "request-heading");
  heading.append(el("p", "eyebrow", request.console_name), el("h3", "", request.name));
  top.append(heading, el("span", `status-chip ${installed ? "success" : failed ? "warning" : ""}`, status)); card.append(top);
  card.append(el("p", "request-meta", `${request.conversion === "server" ? "Server preparation" : "ISO download · convert on Xbox"} · Requested ${date(request.created_at)}`));
  if(request.inbox)card.append(el("p","muted",`Prepared for Xbox inbox${request.accepted_at?" · accepted by Xbox":" · waiting for Xbox"}${request.expires_at?` · expires ${date(new Date(request.expires_at*1000).toISOString())}`:""}`));
  const progress = el("div", "activity-box");
  if (preparing) {
    const bar = el("progress"); bar.max = 100; if (request.jobs.length) bar.value = request.progress;
    bar.setAttribute("aria-label", "Server preparation progress");
    progress.append(el("strong", "", request.jobs.length ? `Server preparation · ${Math.round(request.progress)}%` : "Preparing download"), bar, el("p", "muted", request.message));
  } else if (installed) {
    progress.append(el("strong", "", "The Xbox reported a successful installation."), el("p", "muted", `Reported ${date(request.reported_at)}${removed ? " · Prepared server copy removed." : ""}`));
  } else if (removed || request.state === "expired") {
    progress.append(el("p", "", removed ? "Prepared server download deleted. Request this installation again in Omni360 if needed." : request.message));
  } else if (transfer) {
    progress.append(el("strong", "", `${transfer.archive ? "Archive part" : "File"} ${transfer.file_index} of ${transfer.file_count} requested`), el("p", "filename", transfer.filename), el("p", "muted", `Last download request ${date(transfer.requested_at)}. Waiting for the Xbox to report installation.`));
    if (transfer.archive) progress.append(el("p", "muted", "Xbox checking and conversion happen on the console. Their exact percentage is not reported here."));
  } else progress.append(el("p", "", request.message));
  card.append(progress);
  if (failed) card.append(el("p", "error-note", "An error was reported. Open the Queue on the Xbox or inspect the preparation details below."));
  const details = el("details", "request-details"); details.dataset.request = request.id; details.open = expanded.has(request.id);
  details.append(el("summary", "", `Sources & preparation · ${request.sources.length}`));
  const list = el("ul", "source-list"); for (const source of request.sources) list.append(el("li", "filename", source.name)); details.append(list);
  for (const job of request.jobs) details.append(el("p", "muted", `${job.status === "complete" ? "Prepared" : job.stage} · ${job.message || job.status}`));
  card.append(details);
  const footer = el("div", "request-footer");
  footer.append(el("span", "muted", request.prepared_bytes ? `${bytes(request.prepared_bytes)} · prepared server copy` : "No prepared server download stored"));
  if (request.can_delete) footer.append(action("Delete download files", `/api/consoles/requests/${request.id}/delete-files`, {confirm: true},
    `Delete ${bytes(request.prepared_bytes)} of prepared SERVER download files for ${request.name}?\n\nAn unfinished Xbox download may stop and need to be requested again. Shared library games, original source archives, and games installed on your Xbox will remain.`, "Prepared server download deleted.", true));
  if(request.inbox&&["expired","failed","cancelled"].includes(request.state))footer.append(action("Prepare again",`/api/consoles/${request.device_id}/prepare`,{selection:request.selection,request_key:"web_"+crypto.randomUUID().replaceAll("-","")},null,"Fresh preparation requested."));
  card.append(footer); return card;
}
async function load(force = false) {
  if (state.loading) { if (force) state.reload = true; return; }
  state.loading = true;
  try {
    const query = new URLSearchParams({cursor: state.cursor, include_removed: node("include-removed").checked ? "1" : "0"});
    if (node("console-filter").value) query.set("device", node("console-filter").value);
    const data = await call(`/api/consoles?${query}`), expanded = new Set([...node("requests").querySelectorAll("details[open]")].map(d => d.dataset.request));
    renderDevices(data.devices); node("requests").replaceChildren();
    for (const request of data.installs) node("requests").append(renderRequest(request, expanded));
    if (!data.installs.length) node("requests").append(el("p", "empty", "Requests will appear here when you start an installation in Omni360."));
    node("paired-count").textContent = data.devices.filter(d => !d.revoked).length;
    node("preparing-count").textContent = data.preparing; node("storage-count").textContent = bytes(data.storage_bytes);
    node("request-count").textContent = `${data.total} request${data.total === 1 ? "" : "s"}`;
    state.next = data.next_cursor; state.previous = data.previous_cursor;
    node("next").disabled = !state.next; node("previous").disabled = !state.previous;
    node("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) { node("notice").textContent = error.name === "AbortError" ? "Could not refresh activity. Trying again shortly." : error.message; }
  finally { state.loading = false; if (state.reload) { state.reload = false; void load(); } }
}
node("pair-form").onsubmit = async event => {
  event.preventDefault();
  try {
    await call("/api/consoles/pair", {code: node("code").value.trim(), name: node("name").value.trim()});
    node("notice").textContent = "Approved. Keep Omni360 open to finish pairing."; node("code").value = ""; await load(true);
  } catch (error) { node("notice").textContent = error.message; }
};
node("console-filter").onchange = node("include-removed").onchange = () => { state.cursor = "0"; void load(true); };
node("refresh").onclick = () => void load(true);
node("next").onclick = () => { if (state.next) { state.cursor = state.next; void load(true); } };
node("previous").onclick = () => { if (state.previous) { state.cursor = state.previous; void load(true); } };
void load(); setInterval(() => { if (!document.hidden) void load(); }, 5000);
