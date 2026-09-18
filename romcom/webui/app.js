"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const GOOD = new Set(["VERIFIED", "NORMALIZED", "INSTALLED", "TESTED"]);
const ACTIVE = new Set(["QUEUED", "DOWNLOADING", "DOWNLOADED", "EXTRACTED", "FOUND"]);
const LIFECYCLE_FALLBACK = ["CATALOGED","MISSING","FOUND","QUEUED","DOWNLOADING","DOWNLOADED","EXTRACTED","VERIFIED","NORMALIZED","INSTALLED","TESTED","FAILED","MANUAL","EXCLUDED"];
let LIFECYCLE = LIFECYCLE_FALLBACK;

function statusClass(s) {
  if (GOOD.has(s)) return "good";
  if (ACTIVE.has(s)) return "active";
  if (s === "FAILED") return "critical";
  if (s === "MISSING" || s === "MANUAL") return "serious";
  return "";
}
const badge = s => `<span class="badge ${statusClass(s)}">${esc(s)}</span>`;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function fmtBytes(n) {
  let x = Number(n || 0);
  if (!x) return "—";
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (x < 1024) return `${x.toFixed(x < 10 && u !== "B" ? 1 : 0)} ${u}`;
    x /= 1024;
  }
  return `${x.toFixed(1)} PB`;
}

function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), err ? 8000 : 4000);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* non-JSON error body */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}
const post = (path, body) => api(path, {
  method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})
});

/* ---------- Tabs ---------- */
const loaders = { dashboard: loadDashboard, library: loadLibrary, acquire: loadAcquire, activity: loadActivity, import: loadImport };
const loaded = {};

function showTab(name) {
  if (!loaders[name]) name = "dashboard";
  $$(".tab").forEach(x => x.classList.toggle("active", x.dataset.tab === name));
  $$(".tabpane").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  loaders[name]();
}
$$(".tab").forEach(b => b.addEventListener("click", () => { location.hash = b.dataset.tab; }));
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

/* ---------- Dashboard ---------- */
async function loadDashboard() {
  try {
    const s = await api("/api/summary");
    const pct = s.wanted ? (100 * s.satisfied / s.wanted) : 0;
    $("#stat-tiles").innerHTML = `
      <div class="tile"><div class="v">${s.cataloged.toLocaleString()}</div><div class="l">Cataloged</div></div>
      <div class="tile"><div class="v">${s.wanted.toLocaleString()}</div><div class="l">Wanted</div></div>
      <div class="tile"><div class="v">${s.satisfied.toLocaleString()}</div><div class="l">Satisfied</div><div class="d">${pct.toFixed(1)}% of wanted</div></div>
      <div class="tile"><div class="v">${s.active_jobs}</div><div class="l">Active downloads</div></div>`;
    const systems = [...s.by_system].sort((a, b) => (b.wanted - a.wanted) || (b.total - a.total));
    $("#system-meters").innerHTML = `<div class="meter-row head">
        <span></span><span></span><span class="nums">wanted</span><span class="nums">cataloged</span></div>` +
      systems.map(x => {
        const p = x.wanted ? 100 * x.satisfied / x.wanted : 0;
        return `<div class="meter-row">
          <span class="name" title="${esc(x.system)}">${esc(x.system)}</span>
          <span class="meter"><i style="width:${p.toFixed(1)}%"></i></span>
          <span class="nums">${x.wanted ? `${x.satisfied}/${x.wanted}` : "—"}</span>
          <span class="nums">${x.total.toLocaleString()}</span></div>`;
      }).join("");
    $("#status-table").innerHTML = Object.entries(s.by_status).sort()
      .map(([k, v]) => `<tr><td>${badge(k)}</td><td class="r">${v}</td></tr>`).join("");
  } catch (e) { toast("Summary failed: " + e.message, true); }

  api("/api/catalog-status").then(rows => {
    $("#catalog-list").innerHTML = rows.length ? rows.map(r => `
      <div class="check ${r.loaded ? "ok" : "bad"}">
        <span class="mark">${r.loaded ? "✓" : "✕"}</span>
        <span class="n">${esc(r.source)}</span>
        <span class="d">${esc(r.system)} — ${r.loaded ? r.count + " items" : "not imported"}</span></div>`).join("")
      : `<span class="sub">No enabled catalogs in catalogs.yaml</span>`;
  }).catch(e => { $("#catalog-list").textContent = e.message; });

  // Doctor pings external services; render independently so the rest of the page never waits on it.
  $("#doctor-list").textContent = "Running checks…";
  api("/api/doctor").then(checks => {
    $("#doctor-list").innerHTML = checks.map(c => `
      <div class="check ${c.ok ? "ok" : "bad"}">
        <span class="mark">${c.ok ? "✓" : "✕"}</span>
        <span class="n">${esc(c.name)}</span><span class="d">${esc(c.detail)}</span></div>`).join("");
    const bad = checks.filter(c => !c.ok).length;
    const chip = $("#health-chip");
    chip.textContent = bad ? `${bad} check${bad > 1 ? "s" : ""} failing` : "all systems go";
    chip.className = "chip " + (bad ? "bad" : "ok");
  }).catch(e => {
    $("#doctor-list").textContent = e.message;
    $("#health-chip").textContent = "health unknown"; $("#health-chip").className = "chip";
  });
}

/* ---------- Library ---------- */
const lib = { offset: 0, limit: 200, total: 0 };

async function initFacets() {
  try {
    const f = await api("/api/facets");
    LIFECYCLE = f.statuses || LIFECYCLE_FALLBACK;
    $("#f-system").innerHTML = `<option value="">All systems</option>` +
      f.systems.map(s => `<option>${esc(s)}</option>`).join("");
    $("#f-status").innerHTML = `<option value="">All statuses</option>` +
      LIFECYCLE.map(s => `<option>${esc(s)}</option>`).join("");
    $("#imp-system").innerHTML = `<option value="">Auto-detect system</option>` +
      (f.all_systems || f.systems).map(s => `<option>${esc(s)}</option>`).join("");
  } catch (e) { toast("Facets failed: " + e.message, true); }
}

function libQuery() {
  const p = new URLSearchParams({limit: lib.limit, offset: lib.offset, view: $("#f-view").value});
  if ($("#f-q").value.trim()) p.set("q", $("#f-q").value.trim());
  if ($("#f-system").value) p.set("system", $("#f-system").value);
  if ($("#f-status").value) p.set("status", $("#f-status").value);
  return p;
}

function itemRow(r) {
  const series = r.series ? `<div class="sub">${esc(r.series)}${r.series_number ? " #" + r.series_number : ""}</div>` : "";
  const statusSel = `<select class="status-edit" data-id="${esc(r.id)}">` +
    LIFECYCLE.map(s => `<option ${s === r.status ? "selected" : ""}>${esc(s)}</option>`).join("") + `</select>`;
  return `<tr data-id="${esc(r.id)}">
    <td><div>${esc(r.title)}</div>${series}<div class="sub">${esc(r.id)}</div></td>
    <td>${esc(r.system || "—")}</td>
    <td class="r">${r.year || "—"}</td>
    <td>${badge(r.status)} ${statusSel}</td>
    <td class="c"><input type="checkbox" class="flag" data-field="authorized" data-id="${esc(r.id)}" ${r.authorized ? "checked" : ""}></td>
    <td class="c"><input type="checkbox" class="flag" data-field="wanted" data-id="${esc(r.id)}" ${r.wanted ? "checked" : ""}></td>
    <td><button class="small primary act-search" data-id="${esc(r.id)}" data-title="${esc(r.title)}" ${r.authorized ? "" : "disabled title='Mark authorized first'"}>Search</button></td>
  </tr>`;
}

async function loadLibrary(append = false) {
  if (!append) lib.offset = 0;
  try {
    const d = await api("/api/items?" + libQuery());
    lib.total = d.total;
    const html = d.items.map(itemRow).join("");
    const body = $("#lib-table tbody");
    if (append) body.insertAdjacentHTML("beforeend", html); else body.innerHTML = html;
    const shown = body.children.length;
    $("#lib-count").textContent = `${shown.toLocaleString()} of ${d.total.toLocaleString()} items`;
    $("#lib-more").hidden = shown >= d.total;
    if (!d.total && $("#f-view").value !== "all") {
      // The filters (not the catalog) are hiding everything — say so and offer the way out.
      const p = libQuery(); p.set("view", "all"); p.set("limit", "1");
      const all = await api("/api/items?" + p);
      if (all.total) body.innerHTML = `<tr><td colspan="7" class="sub" style="padding:20px">
        No <b>${esc($("#f-view").selectedOptions[0].text)}</b> items match — but ${all.total.toLocaleString()}
        cataloged items do. Imported catalogs start as not-wanted; switch to
        <button class="small ghost" id="lib-showall">All items</button> and use the bulk action to flag what you want.</td></tr>`;
    }
  } catch (e) { toast("Library failed: " + e.message, true); }
}

$("#lib-table").addEventListener("click", e => {
  if (e.target.id === "lib-showall") { $("#f-view").value = "all"; loadLibrary(); }
});

$("#bulk-apply").addEventListener("click", async () => {
  const v = $("#bulk-action").value;
  if (!v) { toast("Choose a bulk action first", true); return; }
  if (!lib.total) { toast("No items match the current filters", true); return; }
  const [field, value] = v.split(":");
  const label = $("#bulk-action").selectedOptions[0].text.toLowerCase();
  if (!confirm(`${$("#bulk-action").selectedOptions[0].text} for ALL ${lib.total.toLocaleString()} items matching the current filters?`)) return;
  try {
    const filters = {q: $("#f-q").value.trim(), system: $("#f-system").value, status: $("#f-status").value, view: $("#f-view").value};
    const r = await post("/api/items/bulk", {field, value, filters});
    toast(`${r.updated.toLocaleString()} items: ${label}`);
    loadLibrary();
  } catch (e) { toast(e.message, true); }
});

let debounce;
$("#f-q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => loadLibrary(), 300); });
["#f-view", "#f-system", "#f-status"].forEach(s => $(s).addEventListener("change", () => loadLibrary()));
$("#lib-more").addEventListener("click", () => { lib.offset += lib.limit; loadLibrary(true); });

$("#lib-table").addEventListener("change", async e => {
  const t = e.target;
  try {
    if (t.matches("input.flag")) {
      const r = await post(`/api/items/${encodeURIComponent(t.dataset.id)}`, {field: t.dataset.field, value: t.checked ? 1 : 0});
      toast(`${r.title}: ${t.dataset.field} ${t.checked ? "on" : "off"}`);
      if (t.dataset.field === "authorized")
        $(`.act-search[data-id="${CSS.escape(t.dataset.id)}"]`).disabled = !t.checked;
    } else if (t.matches("select.status-edit")) {
      const r = await post(`/api/items/${encodeURIComponent(t.dataset.id)}`, {field: "status", value: t.value});
      toast(`${r.title}: status ${r.status}`);
      t.closest("td").querySelector(".badge").outerHTML = badge(r.status);
    }
  } catch (err) { toast(err.message, true); loadLibrary(); }
});

/* ---------- Search drawer & acquire ---------- */
document.addEventListener("click", e => {
  const b = e.target.closest(".act-search");
  if (b) openSearch(b.dataset.id, b.dataset.title);
});

async function openSearch(ident, title) {
  $("#drawer").hidden = false;
  $("#drawer-title").textContent = `Search: ${title}`;
  $("#drawer-body").textContent = "Searching the indexer…";
  try {
    const d = await api(`/api/search/${encodeURIComponent(ident)}`);
    if (!d.results.length) { $("#drawer-body").innerHTML = `<p class="sub">No results found on the indexer.</p>`; return; }
    $("#drawer-body").innerHTML = `<div class="tablewrap"><table>
      <thead><tr><th class="r">Score</th><th class="r">Size</th><th>Release</th><th></th></tr></thead>
      <tbody>${d.results.map((x, i) => `<tr>
        <td class="r">${x.score.toFixed(1)}</td>
        <td class="r">${fmtBytes(x.size)}</td>
        <td>${esc(x.title)}</td>
        <td><button class="small primary act-grab" data-i="${i}">Grab</button></td>
      </tr>`).join("")}</tbody></table></div>`;
    $("#drawer-body").onclick = async ev => {
      const g = ev.target.closest(".act-grab");
      if (!g) return;
      g.disabled = true; g.textContent = "Queueing…";
      const x = d.results[Number(g.dataset.i)];
      try {
        const r = await post("/api/acquire", {ident, url: x.url, title: x.title, size: x.size});
        toast(`Queued: ${r.queued}`);
        g.textContent = "Queued ✓";
        loadActivity(); if (loaded.library) loadLibrary();
      } catch (err) { toast(err.message, true); g.disabled = false; g.textContent = "Grab"; }
    };
  } catch (e) {
    $("#drawer-body").innerHTML = `<p class="sub">${esc(e.message)}</p>`;
  }
}
$("#drawer-close").addEventListener("click", () => { $("#drawer").hidden = true; });
$("#drawer").addEventListener("click", e => { if (e.target.id === "drawer") $("#drawer").hidden = true; });
document.addEventListener("keydown", e => { if (e.key === "Escape") $("#drawer").hidden = true; });

/* ---------- Acquire ---------- */
async function loadAcquire() {
  try {
    const d = await api("/api/plan");
    $("#vol-table tbody").innerHTML = d.volumes.length ? d.volumes.map(v => `<tr>
      <td><div>${esc(v.title)}</div><div class="sub">${esc(v.id)}</div></td>
      <td class="r">${v.covered}</td><td class="r">${v.missing}</td>
      <td class="r">${fmtBytes(v.estimated_bytes)}</td>
      <td class="r">${v.coverage_score.toFixed(2)}</td>
      <td><button class="small primary act-search" data-id="${esc(v.id)}" data-title="${esc(v.title)}">Search</button></td>
    </tr>`).join("") : `<tr><td colspan="6" class="sub">No authorized volumes awaiting download.</td></tr>`;
    $("#next-table tbody").innerHTML = d.items.length ? d.items.map(i => `<tr>
      <td><div>${esc(i.title)}</div><div class="sub">${esc(i.id)}</div></td>
      <td>${esc(i.system || "—")}</td>
      <td>${badge(i.status)}</td>
      <td><button class="small primary act-search" data-id="${esc(i.id)}" data-title="${esc(i.title)}">Search</button></td>
    </tr>`).join("") : `<tr><td colspan="4" class="sub">Nothing to pick up — authorize wanted items in the Library to see them here.</td></tr>`;
  } catch (e) { toast("Plan failed: " + e.message, true); }
}

/* ---------- Activity ---------- */
async function loadActivity() {
  try {
    const jobs = await api("/api/jobs");
    $("#jobs-table tbody").innerHTML = jobs.length ? jobs.map(j => `<tr>
      <td><div>${esc(j.entity_title)}</div><div class="sub">${esc(j.entity_type)} · ${esc(j.nzo_id || "no nzo id")}</div></td>
      <td class="sub">${esc(j.result_title || "")}</td>
      <td class="r">${fmtBytes(j.bytes)}</td>
      <td>${badge(j.status || "UNKNOWN")}</td>
      <td class="sub">${esc(j.queued_at || "—")}</td>
      <td class="sub">${esc(j.completed_at || "—")}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="sub">No downloads yet — grab something from Acquire or the Library.</td></tr>`;
  } catch (e) { toast("Jobs failed: " + e.message, true); }
}

async function doSync(manual = false) {
  const btn = $("#sync-now");
  btn.disabled = true;
  try {
    const r = await post("/api/sync");
    $("#sync-status").textContent = `Last sync ${new Date().toLocaleTimeString()} — ${r.tracked} tracked, ${r.updated} updated`;
    if (manual) toast(`Synced: ${r.tracked} active job(s), ${r.updated} updated`);
    loadActivity();
  } catch (e) {
    $("#sync-status").textContent = `Sync failed: ${e.message}`;
    if (manual) toast("Sync failed: " + e.message, true);
  } finally { btn.disabled = false; }
}
$("#sync-now").addEventListener("click", () => doSync(true));
setInterval(() => {
  if ($("#auto-sync").checked && !document.hidden && $("#tab-activity").classList.contains("active")) doSync();
}, 20000);

/* ---------- Background jobs: import / scan / organize ---------- */
const JOBS = {
  import:   {wrap: "#imp-progress",  bar: "#imp-bar",  cur: "#imp-current",  btn: "#imp-start",  out: "#imp-results",  render: renderImportResult,   doneMsg: "Import finished"},
  scan:     {wrap: "#scan-progress", bar: "#scan-bar", cur: "#scan-current", btn: "#scan-start", out: "#scan-result",  render: renderScanResult,     doneMsg: "Scan finished"},
  organize: {wrap: "#org-progress",  bar: "#org-bar",  cur: "#org-current",  btn: "#org-start",  out: "#org-result",   render: renderOrganizeResult, doneMsg: "Export finished"},
};
const jobTimers = {};

function loadImport() {
  $("#imp-path").value ||= localStorage.getItem("romcom-dat-path") || "";
  $("#scan-path").value ||= localStorage.getItem("romcom-rom-path") || "";
  $("#org-path").value ||= localStorage.getItem("romcom-sd-path") || "";
  for (const kind of Object.keys(JOBS)) pollJob(kind, false);
}

async function startJob(kind, url, body) {
  try {
    await post(url, body);
    const j = JOBS[kind];
    if (j.out) $(j.out).innerHTML = "";
    if (kind === "import") { $("#imp-summary").innerHTML = ""; }
    pollJob(kind, true);
  } catch (e) { toast(e.message, true); }
}

async function pollJob(kind, loop) {
  const j = JOBS[kind];
  clearTimeout(jobTimers[kind]);
  let s;
  try { s = await api(`/api/job/${kind}/status`); } catch { return; }
  if (s.running) {
    $(j.wrap).hidden = false;
    $(j.btn).disabled = true;
    const pct = s.total ? (100 * s.done / s.total) : 0;
    $(j.bar).style.width = pct.toFixed(1) + "%";
    $(j.cur).textContent = s.total ? `${s.done}/${s.total} — ${s.current}` : s.current;
    jobTimers[kind] = setTimeout(() => pollJob(kind, true), 800);
    return;
  }
  $(j.btn).disabled = false;
  $(j.wrap).hidden = true;
  if (!loop && !s.result && !s.error) return;
  if (s.error) {
    if (loop) toast(`${kind} failed: ${s.error}`, true);
    if (j.out) $(j.out).innerHTML = `<p class="sub">${esc(s.error)}</p>`;
    return;
  }
  if (!s.result) return;
  j.render(s.result);
  if (loop) { toast(j.doneMsg); loadDashboard(); }
}

$("#imp-start").addEventListener("click", () => {
  const path = $("#imp-path").value.trim();
  if (!path) { toast("Enter a folder or file path first", true); return; }
  localStorage.setItem("romcom-dat-path", path);
  startJob("import", "/api/import-dats", {path, system: $("#imp-system").value, wanted: $("#imp-wanted").checked});
});

$("#scan-start").addEventListener("click", () => {
  const path = $("#scan-path").value.trim();
  if (!path) { toast("Enter the folder with your ROM files first", true); return; }
  localStorage.setItem("romcom-rom-path", path);
  startJob("scan", "/api/scan", {path, name_match: $("#scan-name").checked, adopt: $("#scan-adopt").checked});
});

$("#org-start").addEventListener("click", () => {
  const path = $("#org-path").value.trim();
  if (!path) { toast("Enter the destination (SD card) path first", true); return; }
  localStorage.setItem("romcom-sd-path", path);
  const systems = $("#org-systems").value.split(",").map(s => s.trim()).filter(Boolean);
  startJob("organize", "/api/organize", {path, systems});
});

function renderScanResult(r) {
  const adopted = r.adopted || 0;
  const undetectable = r.adopt_skipped || 0;
  const usable = r.matched + adopted;
  const sysRows = Object.entries(r.adopted_by_system || {}).sort((a, b) => b[1] - a[1])
    .map(([s, n]) => `<tr><td>${esc(s)}</td><td class="r">${n.toLocaleString()}</td></tr>`).join("");
  const skips = Object.entries(r.skipped_exts || {})
    .map(([e, n]) => `<div class="check bad"><span class="mark">–</span><span class="d">${esc(e)} — ${n.toLocaleString()} file(s), system undetectable</span></div>`).join("");
  $("#scan-result").innerHTML = `<div class="tiles" style="margin-top:12px">
    <div class="tile"><div class="v">${r.files.toLocaleString()}</div><div class="l">Files hashed</div></div>
    <div class="tile"><div class="v">${r.matched.toLocaleString()}</div><div class="l">Matched to catalog</div></div>
    <div class="tile"><div class="v">${r.verified.toLocaleString()}</div><div class="l">Hash-verified</div></div>
    <div class="tile"><div class="v">${adopted.toLocaleString()}</div><div class="l">Cataloged as local</div></div>
    <div class="tile"><div class="v">${undetectable.toLocaleString()}</div><div class="l">Undetectable</div>
      ${undetectable ? `<div class="d">system unknown — see below</div>` : ""}</div></div>`
    + (sysRows ? `<details class="skiplist"><summary>local entries by system</summary>
        <div class="tablewrap"><table><thead><tr><th>System</th><th class="r">Cataloged</th></tr></thead><tbody>${sysRows}</tbody></table></div></details>` : "")
    + (skips ? `<details class="skiplist"><summary>undetectable files by extension</summary><div class="checklist" style="max-height:none">${skips}</div></details>` : "")
    + (usable ? `<p class="sub">${usable.toLocaleString()} file(s) are ready to organize —
        <button class="small primary" id="scan-to-org">Continue to export ↓</button></p>` : "");
}

$("#scan-result").addEventListener("click", e => {
  if (e.target.id === "scan-to-org") {
    document.getElementById("org-card").scrollIntoView({behavior: "smooth", block: "start"});
    $("#org-path").focus();
  }
});

function renderOrganizeResult(r) {
  const rows = Object.entries(r.by_system).sort()
    .map(([s, n]) => `<tr><td>${esc(s)}</td><td class="r">${n.toLocaleString()}</td></tr>`).join("");
  const errs = r.errors.map(x => `<div class="check bad"><span class="mark">✕</span>
    <span class="d">${esc(x.file)} — ${esc(x.error)}</span></div>`).join("");
  $("#org-result").innerHTML = `<div class="tiles" style="margin-top:12px">
      <div class="tile"><div class="v">${r.copied.toLocaleString()}</div><div class="l">Copied</div></div>
      <div class="tile"><div class="v">${r.skipped.toLocaleString()}</div><div class="l">Already there</div></div>
      <div class="tile"><div class="v">${r.missing.toLocaleString()}</div><div class="l">Source missing</div><div class="d">re-scan to fix</div></div>
      <div class="tile"><div class="v">${r.errors.length}</div><div class="l">Errors</div></div></div>`
    + (rows ? `<div class="tablewrap" style="margin-top:12px"><table>
        <thead><tr><th>System folder</th><th class="r">Files</th></tr></thead><tbody>${rows}</tbody></table></div>` : "")
    + (errs ? `<details class="skiplist" open><summary>${r.errors.length} errors</summary><div class="checklist" style="max-height:none">${errs}</div></details>` : "");
}

function renderImportResult(r) {
  $("#imp-summary").innerHTML = `
    <div class="tile"><div class="v">${r.dats}</div><div class="l">DATs read</div><div class="d">${r.files} files</div></div>
    <div class="tile"><div class="v">${r.items.toLocaleString()}</div><div class="l">Items cataloged</div></div>
    <div class="tile"><div class="v">${r.hashes.toLocaleString()}</div><div class="l">Hashes stored</div></div>
    <div class="tile"><div class="v">${r.scummvm_matched.toLocaleString()}</div><div class="l">ScummVM matches</div></div>
    <div class="tile"><div class="v">${r.skipped.length}</div><div class="l">Skipped</div><div class="d">${r.errors.length} errors</div></div>`;
  const rows = r.imported.map(x => `<tr>
    <td><div>${esc(x.dat)}</div><div class="sub">${esc(x.file)}</div></td>
    <td>${esc(x.system)}</td><td>${esc(x.source)}</td>
    <td class="r">${x.system === "scummvm" ? `${x.matched} matched / ${x.unmatched} unmatched` : x.items.toLocaleString()}</td>
    <td class="r">${x.hashes.toLocaleString()}</td></tr>`).join("");
  const skips = r.skipped.map(x => `<div class="check bad"><span class="mark">–</span>
    <span class="d">${esc(x.file)}${x.dat && x.dat !== x.file ? " → " + esc(x.dat) : ""} — ${esc(x.reason)}</span></div>`).join("");
  const errs = r.errors.map(x => `<div class="check bad"><span class="mark">✕</span>
    <span class="d">${esc(x.file)} — ${esc(x.error)}</span></div>`).join("");
  $("#imp-results").innerHTML = (rows ? `<div class="tablewrap"><table>
      <thead><tr><th>DAT</th><th>System</th><th>Source</th><th class="r">Items</th><th class="r">Hashes</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : `<p class="sub">Nothing imported.</p>`)
    + (skips ? `<details class="skiplist"><summary>${r.skipped.length} skipped</summary><div class="checklist" style="max-height:none">${skips}</div></details>` : "")
    + (errs ? `<details class="skiplist" open><summary>${r.errors.length} errors</summary><div class="checklist" style="max-height:none">${errs}</div></details>` : "");
}

/* ---------- File/folder picker ---------- */
let pickerPath = "";
let pickerTarget = "#imp-path";

document.addEventListener("click", e => {
  const b = e.target.closest(".browse");
  if (!b) return;
  pickerTarget = b.dataset.target;
  $("#picker").hidden = false;
  browseTo($(pickerTarget).value.trim() || "");
});
$("#picker-close").addEventListener("click", () => { $("#picker").hidden = true; });
$("#picker").addEventListener("click", e => { if (e.target.id === "picker") $("#picker").hidden = true; });
$("#picker-select").addEventListener("click", () => {
  if (!pickerPath) { toast("Pick a drive first", true); return; }
  $(pickerTarget).value = pickerPath;
  $("#picker").hidden = true;
});

async function browseTo(path) {
  let d;
  try { d = await api("/api/browse?path=" + encodeURIComponent(path)); }
  catch (e) {
    if (path) return browseTo("");   // stale/bad path — fall back to the drive list
    toast(e.message, true); return;
  }
  pickerPath = d.path;
  $("#picker-path").textContent = d.path || "Select a drive";
  $("#picker-select").disabled = !d.path;
  const join = name => d.path ? d.path.replace(/[\\\/]$/, "") + "\\" + name : name;
  $("#picker-list").innerHTML =
    (d.parent !== null ? `<div class="picker-row" data-nav="${esc(d.parent)}" data-up="1"><span class="ic">↑</span><span>..</span></div>` : "") +
    d.dirs.map(n => `<div class="picker-row" data-nav="${esc(join(n))}"><span class="ic">📁</span><span>${esc(n)}</span></div>`).join("") +
    d.files.map(n => `<div class="picker-row file" data-pick="${esc(join(n))}"><span class="ic">📄</span><span>${esc(n)}</span></div>`).join("") ||
    `<div class="picker-row"><span class="sub">Empty folder</span></div>`;
}

$("#picker-list").addEventListener("click", e => {
  const row = e.target.closest(".picker-row");
  if (!row) return;
  if (row.dataset.pick) {
    $(pickerTarget).value = row.dataset.pick;
    $("#picker").hidden = true;
  } else if (row.dataset.nav !== undefined) {
    browseTo(row.dataset.nav);
  }
});

/* ---------- Wrappers to track first-load ---------- */
for (const k of Object.keys(loaders)) {
  const fn = loaders[k];
  loaders[k] = (...a) => { loaded[k] = true; return fn(...a); };
}

initFacets();
showTab(location.hash.slice(1) || "dashboard");
