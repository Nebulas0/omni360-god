"use strict";
const node = (id) => document.getElementById(id);
async function call(path, data) {
  const response = await fetch(path, {method: data ? "POST" : "GET", headers: {
    "Accept": "application/json", "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"
  }, ...(data ? {body: JSON.stringify(data)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function row(title, detail) {
  const wrap = document.createElement("div"); wrap.className = "console-row";
  const text = document.createElement("div");
  const strong = document.createElement("strong"); strong.textContent = title;
  const p = document.createElement("p"); p.textContent = detail;
  text.append(strong, p); wrap.append(text); return wrap;
}
async function load() {
  try {
    const data = await call("/api/consoles");
    node("devices").replaceChildren(); node("requests").replaceChildren();
    if (!data.devices.length) node("devices").textContent = "No consoles paired yet.";
    for (const device of data.devices) {
      const item = row(device.name, device.revoked ? "Access revoked" : `Last connected: ${device.last_seen || "Waiting for the Xbox"}`);
      if (!device.revoked) {
        const button = document.createElement("button"); button.textContent = "Revoke access";
        button.onclick = async () => {
          try { await call(`/api/consoles/${device.id}/revoke`, {}); await load(); }
          catch (error) { node("notice").textContent = error.message; }
        };
        item.append(button);
      }
      node("devices").append(item);
    }
    for (const request of data.installs) node("requests").append(row(`${request.conversion === "server" ? "Server" : "Xbox"} conversion · ${request.installation_status || request.state}`, request.message));
    if (!data.installs.length) node("requests").textContent = "Installation requests from paired consoles will appear here.";
  } catch (error) { node("notice").textContent = error.message; }
}
node("pair-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    await call("/api/consoles/pair", {code: node("code").value.trim(), name: node("name").value.trim()});
    node("notice").textContent = "Approved. Keep Omni360 open to finish pairing.";
    node("code").value = ""; await load();
  } catch (error) { node("notice").textContent = error.message; }
};
load(); setInterval(load, 15000);
