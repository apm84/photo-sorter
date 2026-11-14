"use strict";
const {$, esc} = PS;
const api = async (url, body) => {
  const r = await fetch(url, body === undefined ? {} : {method: "POST",
    headers: {"X-PhotoSorter": "1", "Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
};
const say = t => { $("live").textContent = t; };

let ids = [], pos = 0, group = null, keep = new Set(), keeper = null, busy = false, recycle = true, confirmKind = null;

function fmtSize(b) { return b == null ? "?" : b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB"; }
function gps(m) {
  if (m.lat == null) return "none";
  return (m.place ? esc(m.place) + " " : "") + `<span class="status">(${m.lat.toFixed(4)}, ${m.lon.toFixed(4)})</span>`;
}
const LOC = {library: "in your library", duplicates: "in Duplicates folder", "not copied": "not copied (identical copy)"};

function quality(m) {
  return [
    (m.width || 0) * (m.height || 0),
    m.date_source === "exif" ? 1 : 0,
    (m.camera_make || m.camera_model) ? 1 : 0,
    m.lat != null ? 1 : 0,
    m.size || 0,
    m.rep_id === m.id ? 1 : 0,
    -(m.src_path || "").length
  ];
}
function better(a, b) {
  const ka = quality(a), kb = quality(b);
  for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] > kb[i] ? a : b;
  return a;
}
function bestKept() {
  const kept = group.members.filter(m => keep.has(m.id));
  return kept.length ? kept.reduce(better).id : null;
}

async function refresh(keepPos = true) {
  const [s, g] = await Promise.all([api("/api/cleanup/summary"), api("/api/cleanup/groups?filter=" + $("filter").value)]);
  ids = g.ids;
  if (!keepPos) pos = 0;
  pos = Math.min(pos, Math.max(ids.length - 1, 0));
  $("prog").max = Math.max(s.total, 1); $("prog").value = s.reviewed;
  loadStats();
  $("progtext").textContent = `${s.reviewed} of ${s.total} groups reviewed` + (s.needs_review ? ` - ${s.needs_review} need a second look` : "");
  $("undo").disabled = !s.last_action;
  $("undo").textContent = s.last_action ? `Undo (${s.last_action.label})` : "Undo";
  $("all").disabled = !s.pending;
  recycle = s.recycle_bin;
  $("mode").textContent = (s.batch_running ? "A sorting batch is running: decisions are paused until it finishes. " : "") +
    (s.recycle_bin ? "Removed copies go to the Recycle Bin. Originals are never touched." :
    "Recycle Bin not available: removed copies go to _index\\_trash in your sorted folder. Originals are never touched.");
  await show();
}

async function show() {
  const box = $("group");
  if (!ids.length) {
    group = null;
    box.innerHTML = `<div class="cu-done"><h2>All done</h2><p>Every duplicate group has been reviewed.</p></div>`;
    return;
  }
  group = await api(`/api/cleanup/group/${ids[pos]}`);
  keep = new Set(group.members.filter(m => m.proposed === "keep").map(m => m.id));
  keeper = keep.has(group.recommended_keeper_id) ? group.recommended_keeper_id : bestKept();
  render();
  const rec = group.recommended_keeper_id, i = rec == null ? -1 : idx(rec);
  say(`Group ${group.id}, ${group.members.length} photos.` + (i >= 0 ? ` Recommended keeper is photo ${i + 1}.` : " No photo is being kept."));
}
const idx = id => group.members.findIndex(m => m.id === id);

function render() {
  const g = group;
  const changed = !sameAsProposal();
  const none = keep.size === 0;
  const tiles = g.members.map((m, i) => {
    const k = keep.has(m.id), isK = m.id === keeper;
    return `<article class="cu-tile ${k ? "keep" : "remove"} ${isK ? "keeper" : ""}" data-id="${m.id}">
      <a href="/preview/${m.id}.jpg" target="_blank" rel="noopener"><img src="/preview/${m.id}.jpg" alt="Photo ${i + 1}: ${esc(m.filename)}" loading="lazy"></a>
      <div class="cu-tile-head"><span class="num">${i + 1}</span>
        <span class="cu-badge">${k ? (isK ? "★ Keeper" : "Keep") : "Remove"}</span>
        <span class="cu-toggle" role="group" aria-label="Keep or remove photo ${i + 1}">
          <button type="button" class="keep-btn" aria-pressed="${k}" title="Keep this photo (${i + 1})">Keep</button>
          <button type="button" class="remove-btn" aria-pressed="${!k}" title="Remove this photo (${i + 1})">Remove</button>
        </span>
        ${k && !isK ? `<button class="mk" title="Make this the keeper">Make keeper</button>` : ""}
        ${!k ? `<button class="mk" title="Keep this one instead">Keep this instead</button>` : ""}
      </div>
      <dl>
        <dt>Size</dt><dd>${m.width ?? "?"}&times;${m.height ?? "?"} &middot; ${fmtSize(m.size)}</dd>
        <dt>Date</dt><dd>${esc(m.taken_at ? m.taken_at.replace("T", " ") : "none")}${m.date_source ? ` <span class="status">(${esc(m.date_source)})</span>` : ""}</dd>
        <dt>Camera</dt><dd>${esc([m.camera_make, m.camera_model].filter(Boolean).join(" ") || "none")}</dd>
        <dt>GPS</dt><dd>${gps(m)}</dd>
        <dt>Where</dt><dd>${LOC[m.location] || ""}${m.exact_of_keeper ? " &middot; identical to keeper" : ""}${m.review ? ` &middot; reviewed: ${esc(m.review)}` : ""}</dd>
        <dt>Copy</dt><dd class="path">${esc(m.dest_path || "-")}</dd>
        <dt>Original</dt><dd class="path">${esc(m.src_path)}</dd>
      </dl></article>`;
  }).join("");
  const recLine = g.recommended_keeper_id != null
    ? `<p class="cu-why"><b>Why photo ${idx(g.recommended_keeper_id) + 1} was picked:</b> ${esc(g.reasons.join("; ") || "best overall quality")}</p>`
    : `<p class="cu-why">No photo is being kept in this group.</p>`;
  $("group").innerHTML = `
    <div class="cu-head"><h2>Group ${g.id} <span class="status">${g.kind === "exact" ? "identical copies" : "look-alikes"} &middot;
      ${g.members.length} photos &middot; ${pos + 1} of ${ids.length}${g.reviewed ? " &middot; already reviewed" : ""}</span></h2>
      ${g.suggested_keeper_id ? `<p class="cu-note">Needs review: ${esc(g.note || "a better copy was added later")}</p>` : ""}
      ${recLine}
      <p class="cu-summary" id="summary">Keeping ${keep.size}, removing ${g.members.length - keep.size}</p></div>
    <div class="cu-tiles">${tiles}</div>
    <div class="row cu-actions">
      <button class="${none ? "danger" : "primary"}" id="apply">${none ? "Delete all" : (changed ? "Apply my choice" : "Approve")} <kbd>Enter</kbd></button>
      <button id="keepall">Keep all (not duplicates) <kbd>A</kbd></button>
      <button class="danger" id="delall">Delete all <kbd>D</kbd></button>
      <button id="reset" ${changed ? "" : "hidden"}>Back to recommendation</button>
      <button id="skip">Skip <kbd>S</kbd></button><button id="prev" ${pos ? "" : "disabled"}>Previous <kbd>P</kbd></button>
    </div>`;
  for (const t of document.querySelectorAll(".cu-tile")) {
    const id = +t.dataset.id;
    t.querySelector(".keep-btn").onclick = () => setKeep(id, true);
    t.querySelector(".remove-btn").onclick = () => setKeep(id, false);
    const mk = t.querySelector(".mk");
    if (mk) mk.onclick = () => makeKeeper(id, !keep.has(id));
  }
  $("apply").onclick = apply; $("keepall").onclick = keepAll; $("delall").onclick = askDeleteAll;
  $("skip").onclick = () => move(1);
  $("prev").onclick = () => move(-1); $("reset").onclick = () => show();
}

function sameAsProposal() {
  const p = group.members.filter(m => m.proposed === "keep").map(m => m.id);
  const rec = keep.has(group.recommended_keeper_id) ? group.recommended_keeper_id : keeper;
  return p.length === keep.size && p.every(id => keep.has(id)) && keeper === rec;
}
function setKeep(id, want) {
  if (want) {
    if (keep.has(id)) return;
    keep.add(id);
    if (keeper == null) keeper = id;
  } else {
    if (!keep.has(id)) return;
    keep.delete(id);
    if (keeper === id) keeper = bestKept();
  }
  render(); say(`Photo ${idx(id) + 1}: ${keep.has(id) ? "keep" : "remove"}. Keeping ${keep.size}, removing ${group.members.length - keep.size}.`);
}
function toggle(id) { setKeep(id, !keep.has(id)); }
function makeKeeper(id, only) {
  if (only) keep = new Set([id]); else keep.add(id);
  keeper = id; render(); say(`Photo ${idx(id) + 1} is now the keeper`);
}
function keepAll() { keep = new Set(group.members.map(m => m.id)); if (keeper == null) keeper = bestKept(); render(); apply(); }
function move(d) { if (!ids.length) return; pos = Math.max(0, Math.min(ids.length - 1, pos + d)); show(); }

function binName() { return recycle ? "Recycle Bin" : "_index\\_trash folder"; }
function askDeleteAll() {
  if (busy || !group) return;
  const n = group.members.length, copies = group.members.filter(m => m.dest_path).length;
  const rest = n - copies;
  confirmKind = "delete";
  $("confirm-title").textContent = "Delete all photos in this group?";
  $("confirm-ok").textContent = "Delete all";
  $("confirm-text").textContent = `This removes all ${n} photo${n === 1 ? "" : "s"} in the group and keeps none. ` +
    `${copies} ${copies === 1 ? "copy goes" : "copies go"} to the ${binName()}` +
    (rest ? `; ${rest} ${rest === 1 ? "was" : "were"} never copied and will just be marked removed` : "") +
    ". Originals are never touched.";
  $("confirm").showModal();
}

async function apply() {
  if (busy || !group) return;
  if (keep.size === 0) { askDeleteAll(); return; }
  busy = true;
  try {
    const r = await api(`/api/cleanup/group/${group.id}`, {keep: [...keep], keeper});
    say(`Group ${group.id} done.`);
    if ($("filter").value === "all") pos++;
    await refresh();
  } catch (e) { say("Could not apply: " + e.message); alert("Could not apply: " + e.message); }
  finally { busy = false; }
}

async function doDeleteAll() {
  if (busy || !group) return;
  busy = true;
  try {
    await api(`/api/cleanup/group/${group.id}/delete-all`, {confirm: true});
    say(`Deleted all photos in group ${group.id}.`);
    if ($("filter").value === "all") pos++;
    await refresh();
  } catch (e) { say("Could not delete: " + e.message); alert("Could not delete: " + e.message); }
  finally { busy = false; }
}

$("undo").onclick = async () => {
  if (busy) return; busy = true;
  try { const r = await api("/api/cleanup/undo", {}); say(`Undid ${r.label}` + (r.problems.length ? ` (${r.problems.length} problems)` : ""));
        if (r.problems.length) alert(r.problems.join("\n")); await refresh(); }
  catch (e) { say(e.message); } finally { busy = false; }
};
$("filter").onchange = () => refresh(false);
$("all").onclick = async () => {
  confirmKind = "all";
  $("confirm-title").textContent = "Approve all remaining recommendations?";
  $("confirm-ok").textContent = "Approve all";
  $("confirm-text").textContent = "Working it out...";
  $("confirm").showModal();
  const p = await api("/api/cleanup/preview-all");
  $("confirm-text").textContent = `This applies my recommendation to ${p.groups} groups: ${p.remove} duplicates marked removed ` +
    `(${p.copies_to_remove} copies moved to the ${p.recycle_bin ? "Recycle Bin" : "_index\\_trash folder"}, the rest were never copied)` +
    (p.promote ? `, and ${p.promote} better copies moved into your library.` : ".");
};
$("confirm").addEventListener("close", async () => {
  const kind = confirmKind;
  confirmKind = null;
  const ok = $("confirm").returnValue === "ok";
  $("confirm").returnValue = "";
  if (!ok) return;
  if (kind === "delete") { await doDeleteAll(); return; }
  if (kind === "trash") { await emptyTrash(); return; }
  if (kind !== "all") return;
  busy = true;
  try { const r = await api("/api/cleanup/approve-all", {confirm: true});
        say(`Approved ${r.groups} groups.`); if (r.errors.length) alert(r.errors.join("\n")); await refresh(false); }
  catch (e) { alert(e.message); } finally { busy = false; }
});

document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey || $("confirm").open) return;
  if ($("panel-review").hidden || e.target.closest("[role=tab]")) return;
  if (e.target.closest("input, select, textarea")) return;
  if (!group && e.key.toLowerCase() !== "u") return;
  const k = e.key;
  if (k === "Enter") { if (e.target.closest("button, a")) return; e.preventDefault(); apply(); }
  else if (k === "ArrowRight" || k === "ArrowLeft") {
    e.preventDefault();
    const n = group.members.length, i = Math.max(idx(keeper), 0);
    makeKeeper(group.members[(i + (k === "ArrowRight" ? 1 : -1) + n) % n].id, true);
  }
  else if (/^[1-9]$/.test(k) && +k <= group.members.length) toggle(group.members[+k - 1].id);
  else if (k === "a" || k === "A") keepAll();
  else if (k === "d" || k === "D") { e.preventDefault(); askDeleteAll(); }
  else if (k === "s" || k === "S") move(1);
  else if (k === "p" || k === "P") move(-1);
  else if (k === "u" || k === "U") $("undo").click();
});

// ---- review stats + clean-up tasks tab (v0.3) ------------------------------------------------------------
async function loadStats() {
  try {
    const p = await api("/api/cleanup/preview-all");
    $("st-groups").textContent = p.groups.toLocaleString();
    $("st-files").textContent = p.copies_to_remove.toLocaleString();
    $("st-bytes").textContent = PS.fmtBytes(p.bytes_to_free) || "0 bytes";
    $("badge-review").textContent = p.groups ? String(p.groups) : "";
  } catch (e) { /* summary still shows */ }
}
let tasks = [];
async function loadTasks() {
  const r = await api("/api/cleanup/tasks");
  tasks = r.tasks;
  const todo = tasks.filter(t => !t.done && !t.info);
  const files = tasks.filter(t => !t.done && !t.info && t.bytes != null).reduce((a, t) => a + t.files, 0);
  const bytes = tasks.filter(t => !t.done && t.id !== "exact").reduce((a, t) => a + (t.bytes || 0), 0);
  $("tt-files").textContent = files.toLocaleString();
  $("tt-bytes").textContent = PS.fmtBytes(bytes) || "0 bytes";
  $("badge-tasks").textContent = todo.length ? String(todo.length) : "";
  $("tasks").innerHTML = tasks.map((t, i) => `<li class="task ${t.done ? "done" : ""} ${t.info ? "info" : ""}">
      <div class="task-main"><h3>${esc(t.title)}</h3><p>${esc(t.detail)}</p></div>
      <div class="task-nums"><b>${t.files.toLocaleString()}</b> ${t.unit || (t.files === 1 ? "file" : "files")}${t.bytes ? ` &middot; <b>${PS.fmtBytes(t.bytes)}</b>` : ""}</div>
      <div class="task-act">${t.action ? (t.action.kind === "link" ? `<a href="${esc(t.action.href)}">${esc(t.action.label)}</a>`
        : `<button type="button" data-i="${i}" class="${t.action.confirm ? "danger" : t.action.kind === "tab" ? "primary" : ""}">${esc(t.action.label)}</button>`)
        : (t.done ? `<span class="status">${t.info ? "Info" : "Done"}</span>` : "")}</div></li>`).join("");
  for (const b of $("tasks").querySelectorAll("button[data-i]")) b.onclick = () => runTask(tasks[+b.dataset.i]);
}
async function runTask(t) {
  const a = t.action;
  if (a.kind === "tab") { selectTab($("tab-review")); return; }
  if (a.confirm) {
    confirmKind = "trash";
    $("confirm-title").textContent = "Empty the app's trash folder?";
    $("confirm-ok").textContent = a.confirm === "recycle" ? "Send to Recycle Bin" : "Delete permanently";
    $("confirm-text").textContent = `${PS.plural(t.files, "file")} (${PS.fmtBytes(t.bytes)}) in _index\\_trash ` +
      (a.confirm === "recycle" ? "will go to the Windows Recycle Bin." :
       "will be deleted permanently. Undo can still restore them by copying your untouched originals again.");
    $("confirm").showModal();
    return;
  }
  try { const r = await api(a.api, {}); say(r.path ? `Opened ${r.path}` : "Done"); }
  catch (e) { say(e.message); alert(e.message); }
}
async function emptyTrash() {
  try { const r = await api("/api/cleanup/empty-trash", {confirm: true});
        say(`Trash folder emptied (${PS.plural(r.files, "file")}, ${PS.fmtBytes(r.bytes)})`); }
  catch (e) { alert(e.message); }
  loadTasks();
}
// tabs: selection follows focus; arrow keys / Home / End move between tabs (roving tabindex)
const tabs = [$("tab-review"), $("tab-tasks")];
function selectTab(tab, focus = true) {
  for (const t of tabs) {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1;
    $(t.id.replace("tab-", "panel-")).hidden = !on;
  }
  if (focus) tab.focus();
  if (tab.id === "tab-tasks") loadTasks();
  history.replaceState(null, "", tab.id === "tab-tasks" ? "#tasks" : location.pathname);
}
$("tablist").addEventListener("click", e => { const t = e.target.closest("[role=tab]"); if (t) selectTab(t); });
$("tablist").addEventListener("keydown", e => {
  const i = tabs.indexOf(document.activeElement); if (i < 0) return;
  let j = null;
  if (e.key === "ArrowRight") j = (i + 1) % tabs.length;
  else if (e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
  else if (e.key === "Home") j = 0; else if (e.key === "End") j = tabs.length - 1;
  if (j != null) { e.preventDefault(); selectTab(tabs[j]); }
});
if (location.hash === "#tasks") selectTab($("tab-tasks"), false);
loadTasks().catch(() => {});
refresh();

// ---- learning from your choices (v0.4) -------------------------------------------------------------------
async function loadLearning() {
  try {
    const r = await PS.api("/api/cleanup/learning");
    document.getElementById("learn-text").textContent = r.active
      ? `Learned from ${r.decisions} of your decisions. Suggestions now follow what you usually keep.`
      : `${r.decisions} of ${r.min_decisions} decisions so far. Suggestions use the built-in quality order until then.`;
    document.getElementById("learn-lessons").innerHTML = (r.lessons || []).map(l => `<li>You usually keep ${PS.esc(l)}</li>`).join("");
  } catch (e) { document.getElementById("learn-text").textContent = e.message; }
}
document.getElementById("learn-reset").addEventListener("click", async () => {
  if (!confirm("Forget everything Clean up learned from your choices? Suggestions go back to the built-in quality order. No photos are touched.")) return;
  try { await PS.api("/api/cleanup/learning/reset", {confirm: true}); } catch (e) { alert(e.message); }
  loadLearning();
});
document.getElementById("tab-tasks").addEventListener("click", loadLearning);
if (location.hash === "#tasks") loadLearning();
