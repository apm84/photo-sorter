"use strict";
const {$, esc, api} = PS;
const say = t => { $("status").textContent = t; };
let current = null, grid = null, dates = null, updateBar = () => {};
// v0.7: one selection for albums (same select circle as photos; Shift-click selects a range) + one Actions menu
let list = [], asel = new Set(), alast = null;
const ACTIONS = [["sync", "Sync with CalendarPeace"], ["unsync", "Stop syncing"], ["delete", "Delete..."]];

function albumBar() {
  let bar = $("albums-selbar");
  if (!bar) {
    bar = document.createElement("div"); bar.id = "albums-selbar"; bar.className = "selbar";
    bar.innerHTML = `<span class="selbar-count" aria-live="polite"></span>
      <button type="button" class="asel-all">Select all albums</button>
      <select class="asel-action" aria-label="Actions for the selected albums" hidden><option value="">Actions...</option>
        ${ACTIONS.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select>
      <button type="button" class="asel-clear" hidden>Clear selection</button>`;
    $("albums").before(bar);
    bar.querySelector(".asel-all").onclick = () => { list.forEach(a => asel.add(a.id)); paintSel(); };
    bar.querySelector(".asel-clear").onclick = () => { asel.clear(); paintSel(); };
    bar.querySelector(".asel-action").onchange = e => { const v = e.target.value; e.target.value = ""; if (v) albumAction(v); };
  }
  const n = asel.size;
  bar.classList.toggle("active", n > 0);
  bar.querySelector(".selbar-count").textContent = n ? `${PS.plural(n, "album")} selected` : "";
  bar.querySelector(".asel-action").hidden = bar.querySelector(".asel-clear").hidden = !n;
  bar.querySelector(".asel-all").disabled = !list.length;
}
function paintSel() {
  for (const w of $("albums").querySelectorAll(".album-wrap")) {
    const on = asel.has(+w.dataset.id);
    w.classList.toggle("selected", on); w.querySelector(".card-sel input").checked = on;
  }
  $("albums").classList.toggle("selecting", asel.size > 0);
  albumBar();
}
async function albumAction(action) {
  const ids = [...asel], names = list.filter(a => asel.has(a.id)).map(a => a.name);
  if (!ids.length) return;
  if (action === "delete" && !confirm(`Delete ${PS.plural(ids.length, "album")} (${names.join(", ")})?\n\nOnly the albums and their folders of links are deleted. Your photos stay in your library, and originals are never touched.`)) return;
  try {
    const r = await api("/api/albums/action", {album_ids: ids, action, confirm: action === "delete"});
    say(action === "sync" ? `${PS.plural(r.changed, "album")} will be sent to CalendarPeace on the next Family sync`
      : action === "unsync" ? `Stopped syncing ${PS.plural(r.changed, "album")}; their photos leave the Pi on the next Family sync (unless another synced album has them)`
      : `Deleted ${PS.plural(r.deleted.length, "album")}. Photos were not touched.`);
    asel.clear(); loadList();
  } catch (e) { say(e.message); alert(e.message); }
}

// v0.9: sort albums and suggestions by name/newest, number of photos, or relevance / quality (albumscore.py)
const SORTS = {albums: [["name", "Name"], ["count", "Number of photos (high to low)"], ["score", "Relevance / quality"]],
               suggested: [["newest", "Newest"], ["count", "Number of photos (high to low)"], ["score", "Relevance / quality"]]};
function sortControl(kind, head, onChange) {
  const id = `sort-${kind}`;
  if ($(id)) return $(id);
  const l = document.createElement("label"); l.className = "sort-ctl";
  l.innerHTML = `Sort <select id="${id}">${SORTS[kind].map(([v, t]) => `<option value="${v}">${esc(t)}</option>`).join("")}</select>`;
  head.appendChild(l);
  const sel = $(id);
  const saved = localStorage.getItem(`ps-${id}`);
  if (saved && SORTS[kind].some(([v]) => v === saved)) sel.value = saved;
  sel.onchange = () => { localStorage.setItem(`ps-${id}`, sel.value); onChange(); };
  return sel;
}
function sortList(arr, how) {
  if (how === "count") arr.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  else if (how === "score") arr.sort((a, b) => (b.score ?? -1) - (a.score ?? -1) || b.count - a.count);
  return arr;
}
function countLine(a, how) {
  return `${PS.plural(a.count, "photo")}${how === "score" && a.score != null ? ` · score ${a.score}` : ""}`;
}

async function loadList() {
  const how = sortControl("albums", $("list-view").querySelector(".gallery-head"), loadList).value;
  const {albums} = await api(`/api/albums${how === "score" ? "?scores=1" : ""}`);
  sortList(albums, how);
  list = albums; const ids = new Set(albums.map(a => a.id));
  for (const id of [...asel]) if (!ids.has(id)) asel.delete(id);
  const box = $("albums"); box.replaceChildren();
  albumBar();
  if (!albums.length) { box.innerHTML = `<p class="status empty">No albums yet.</p>`; return; }
  albums.forEach((a, i) => {
    const w = document.createElement("div");
    w.className = "album-wrap"; w.dataset.id = a.id; w.setAttribute("role", "listitem");
    const el = document.createElement("a");
    el.className = "named-card album-card"; el.href = `/albums?album=${a.id}`;
    el.innerHTML = `${a.cover ? `<img loading="lazy" decoding="async" src="/thumb/${a.cover}.jpg" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noface',textContent:'\u25A1'}))">` : `<span class="noface" aria-hidden="true">&#9633;</span>`}
      <span class="named-name">${esc(a.name)}</span><span class="named-count">${countLine(a, how)}</span>`;
    if (a.score_text) el.title = a.score_text;
    el.onclick = e => {
      if (asel.size && (e.ctrlKey || e.metaKey || e.shiftKey)) { e.preventDefault(); toggle(i, e.shiftKey); return; }
      if (e.ctrlKey || e.metaKey || e.shiftKey) return; e.preventDefault(); openAlbum(a.id, true); };
    const sel = document.createElement("label");
    sel.className = "card-sel"; sel.innerHTML = `<input type="checkbox" aria-label="Select ${esc(a.name)}">`;
    sel.querySelector("input").onclick = e => { e.preventDefault(); toggle(i, e.shiftKey); };
    w.append(el, sel);
    if (a.synced) {
      const b = document.createElement("span"); b.className = "album-synced"; b.textContent = "Synced";
      b.title = "Sent to CalendarPeace by Family sync"; w.appendChild(b);
    }
    box.appendChild(w);
  });
  paintSel();
}
function toggle(i, range) {
  const on = !asel.has(list[i].id);
  if (range && alast != null) { const [a, b] = alast < i ? [alast, i] : [i, alast]; for (let k = a; k <= b; k++) on ? asel.add(list[k].id) : asel.delete(list[k].id); }
  else on ? asel.add(list[i].id) : asel.delete(list[i].id);
  alast = i; paintSel();
}
function params(extra) {
  const p = new URLSearchParams({album: current.id, ...dates.get(), ...extra});
  for (const [k, v] of [...p]) if (v === "") p.delete(k);
  return p;
}
async function openAlbum(id, push) {
  let a;
  try { a = await api(`/api/albums/${id}`); } catch { showList(push); return; }
  current = a;
  if (push) history.pushState({album: id}, "", `/albums?album=${id}`);
  document.title = `Photo Sorter - ${a.name}`;
  $("list-view").hidden = true; $("album-view").hidden = false; if ($("sugg-view")) $("sugg-view").hidden = true;
  $("album-title").textContent = a.name;
  if (a.synced) { const b = document.createElement("span"); b.className = "album-synced inline"; b.textContent = "Synced"; $("album-title").appendChild(b); }
  dates = PS.dateFilter($("album-dates"), a.years, reload, "adf");
  if (!grid) {
    grid = new PS.PhotoGrid($("album-grid"), {
      fetchPage: (offset, limit, after) => api(`/api/search?${params(after ? {after, limit} : {offset, limit})}`), more: $("album-more"),
      fetchIds: async () => (await api(`/api/search?${params({ids_only: true})}`)).ids,
      say, label: "Photos in this album", onSelection: () => updateBar()});
    updateBar = PS.selectionBar($("album-selbar"), grid, say, [{label: "Remove from album", danger: true, onClick: removeSel}]);
  } else grid.clearSelection();
  await reload();
  $("back").focus();
}
async function reload() {
  const r = await grid.load();
  if (r) $("album-count").textContent = PS.plural(r.total, "photo") + (dates.active() ? " in this period" : "");
  updateBar();
}
async function removeSel(ids) {
  if (!ids.length) return;
  const r = await api(`/api/albums/${current.id}/remove`, {file_ids: ids});
  say(`Removed ${PS.plural(r.removed, "photo")} from the album (the photos themselves are not deleted)`);
  grid.clearSelection(); reload();
}
function showList(push) {
  current = null;
  if (push) history.pushState({}, "", "/albums");
  document.title = "Photo Sorter - Albums";
  $("album-view").hidden = true; $("list-view").hidden = false; if ($("sugg-view")) $("sugg-view").hidden = true;
  loadList(); loadSuggestions();
}
function askName(title, value) {
  const d = $("name-dlg");
  $("name-title").textContent = title; $("name-input").value = value || ""; $("name-err").textContent = "";
  d.showModal();
  return new Promise(resolve => {
    const form = d.querySelector("form");
    const submit = e => { e.preventDefault(); const v = $("name-input").value.trim();
      if (!v) { $("name-err").textContent = "Type a name."; return; } done(); d.close("ok"); resolve(v); };
    const close = () => { done(); resolve(null); };
    const done = () => { form.removeEventListener("submit", submit); d.removeEventListener("close", close); };
    form.addEventListener("submit", submit); d.addEventListener("close", close);
  });
}
$("name-cancel").onclick = () => $("name-dlg").close("cancel");
$("new").onclick = async () => {
  const n = await askName("New album");
  if (!n) return;
  try { const a = await api("/api/albums", {name: n}); say(`Created "${a.name}"`); loadList(); } catch (e) { say(e.message); alert(e.message); }
};
$("rename").onclick = async () => {
  const n = await askName("Rename album", current.name);
  if (!n || n === current.name) return;
  try { const a = await api(`/api/albums/${current.id}/rename`, {name: n}); current = {...current, ...a};
        $("album-title").textContent = a.name; say(`Renamed to "${a.name}" (folder Albums\\${a.folder})`); }
  catch (e) { say(e.message); alert(e.message); }
};
$("openfolder").onclick = async () => { const r = await api(`/api/albums/${current.id}/open`, {}); say(`Folder: ${r.path}`); };
$("delete").onclick = () => {
  $("del-text").textContent = `"${current.name}" has ${PS.plural(grid.total, "photo")}.`;
  $("del-dlg").returnValue = ""; $("del-dlg").showModal();
};
$("del-dlg").addEventListener("close", async () => {
  const ok = $("del-dlg").returnValue === "ok"; $("del-dlg").returnValue = "";
  if (!ok) return;
  try { await api(`/api/albums/${current.id}/delete`, {confirm: true}); say(`Deleted album "${current.name}". Photos were not touched.`); showList(true); }
  catch (e) { say(e.message); }
});
$("back").onclick = () => showList(true);
window.addEventListener("popstate", route);
function route() {
  const q = new URLSearchParams(location.search), id = +q.get("album"), sid = +q.get("suggestion");
  if (id) openAlbum(id, false); else if (sid) openSuggestion(sid, false); else showList(false);
}

// ---- v0.8 suggested albums: events found in the library (photos only; never screenshots or memes) --------
let sugg = null, sgrid = null, supdate = () => {};
function suggSection() {
  if ($("suggested")) return;
  const sec = document.createElement("section");
  sec.className = "suggested-section"; sec.setAttribute("aria-labelledby", "suggested-title");
  sec.innerHTML = `<div class="gallery-head"><h2 id="suggested-title" class="page-title">Suggested albums</h2>
      <button type="button" id="find-sugg">Find suggestions</button><span class="status" id="sugg-status" role="status"></span></div>
    <p class="status">Events, outings and gatherings found from dates, places, the people in them and what the photos show.
      Screenshots, memes, documents and graphics are always left out. Open one to take out photos that don't belong,
      rename it, then <b>Keep</b> it as an album or <b>Dismiss</b> it (dismissed ones don't come back).</p>
    <div class="named albums" id="suggested" role="list" aria-label="Suggested albums"></div>`;
  $("list-view").appendChild(sec);
  $("find-sugg").onclick = async () => {
    $("find-sugg").disabled = true; $("sugg-status").textContent = "Looking for events...";
    try {
      const r = await api("/api/suggestions/find", {});
      $("sugg-status").textContent = r.added ? `${PS.plural(r.added, "new suggestion")}` : "No new suggestions";
      loadSuggestions();
    } catch (e) { $("sugg-status").textContent = e.message; }
    finally { $("find-sugg").disabled = false; }
  };
  const view = document.createElement("section");
  view.id = "sugg-view"; view.hidden = true; view.setAttribute("aria-labelledby", "sugg-title");
  view.innerHTML = `<div class="gallery-head">
      <button type="button" id="sugg-back">&larr; All albums</button>
      <h2 id="sugg-title" class="page-title"></h2><span class="status" id="sugg-count"></span>
      <button type="button" id="sugg-rename">Rename</button>
      <button type="button" id="sugg-keep" class="primary">Keep as album</button>
      <button type="button" id="sugg-dismiss">Dismiss</button></div>
    <p class="status">Suggested album: select photos that don't belong and choose Actions &gt; Remove from suggestion.</p>
    <div id="sugg-selbar" class="selbar"></div>
    <div id="sugg-grid"></div>
    <div class="loadmore"><button id="sugg-more" hidden>Load more</button></div>`;
  $("album-view").after(view);
  $("sugg-back").onclick = () => showList(true);
  $("sugg-rename").onclick = async () => {
    const n = await askName("Rename suggestion", sugg.name);
    if (!n || n === sugg.name) return;
    try { sugg = await api(`/api/suggestions/${sugg.id}/rename`, {name: n}); $("sugg-title").textContent = sugg.name; say(`Renamed to "${sugg.name}"`); }
    catch (e) { say(e.message); alert(e.message); }
  };
  $("sugg-keep").onclick = async () => {
    try {
      const r = await api(`/api/suggestions/${sugg.id}/keep`, {name: sugg.name});
      say(`Kept as album "${r.album.name}". ${r.message}`); openAlbum(r.album.id, true);
    } catch (e) { say(e.message); alert(e.message); }
  };
  $("sugg-dismiss").onclick = async () => {
    if (!confirm(`Dismiss "${sugg.name}"? It won't be suggested again. No photos are changed.`)) return;
    try { await api(`/api/suggestions/${sugg.id}/dismiss`, {}); say(`Dismissed "${sugg.name}"`); showList(true); }
    catch (e) { say(e.message); }
  };
}
async function loadSuggestions() {
  suggSection();
  const how = sortControl("suggested", $("suggested").parentElement.querySelector(".gallery-head"), loadSuggestions).value;
  let list = [];
  try { list = sortList((await api(`/api/suggestions${how === "score" ? "?scores=1" : ""}`)).suggestions, how); } catch { return; }
  const box = $("suggested"); box.replaceChildren();
  if (!list.length) { box.innerHTML = `<p class="status empty">No suggestions right now. Press Find suggestions to look again.</p>`; return; }
  for (const s of list) {
    const el = document.createElement("a");
    el.className = "named-card album-card suggestion-card"; el.href = `/albums?suggestion=${s.id}`; el.setAttribute("role", "listitem");
    el.innerHTML = `${s.cover ? `<img loading="lazy" decoding="async" src="/thumb/${s.cover}.jpg" alt="" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noface',textContent:'\u25A1'}))">` : `<span class="noface" aria-hidden="true">&#9633;</span>`}
      <span class="named-name">${esc(s.name)}</span><span class="named-count">${countLine(s, how)}</span>`;
    if (s.score_text) el.title = s.score_text;
    el.onclick = e => { if (e.ctrlKey || e.metaKey || e.shiftKey) return; e.preventDefault(); openSuggestion(s.id, true); };
    box.appendChild(el);
  }
}
function suggBar() {
  const bar = $("sugg-selbar");
  bar.innerHTML = `<span class="selbar-count" aria-live="polite"></span>
    <button type="button" class="ssel-all">Select all</button>
    <select class="ssel-action" aria-label="Actions for the selected photos" hidden><option value="">Actions...</option>
      <option value="remove">Remove from suggestion</option><option value="album">Add to album...</option></select>
    <button type="button" class="ssel-clear" hidden>Clear selection</button>`;
  bar.querySelector(".ssel-all").onclick = () => sgrid.selectAll();
  bar.querySelector(".ssel-clear").onclick = () => sgrid.clearSelection();
  bar.querySelector(".ssel-action").onchange = async e => {
    const v = e.target.value; e.target.value = "";
    const ids = [...sgrid.sel]; if (!ids.length) return;
    if (v === "album") {        // v0.9: existing or new album (hard links, like every album)
      const r = await PS.addToAlbum(ids, say);
      if (r) sgrid.clearSelection();
      return;
    }
    if (v !== "remove") return;
    try {
      const r = await api(`/api/suggestions/${sugg.id}/remove`, {file_ids: ids});
      sugg = {...sugg, count: r.count};
      say(`Took ${PS.plural(ids.length, "photo")} out of the suggestion (the photos themselves are not changed)`);
      sgrid.clearSelection(); suggReload();
    } catch (err) { say(err.message); }
  };
  return () => {
    const n = sgrid.sel.size;
    bar.classList.toggle("active", n > 0);
    bar.querySelector(".selbar-count").textContent = n ? `${PS.plural(n, "photo")} selected` : "";
    bar.querySelector(".ssel-action").hidden = bar.querySelector(".ssel-clear").hidden = !n;
  };
}
async function suggReload() {
  const r = await sgrid.load();
  if (r) $("sugg-count").textContent = PS.plural(r.total, "photo");
  supdate();
}
async function openSuggestion(id, push) {
  suggSection();
  try { sugg = await api(`/api/suggestions/${id}`); } catch { showList(push); return; }
  if (sugg.state !== "open") { if (sugg.album_id) openAlbum(sugg.album_id, push); else showList(push); return; }
  if (push) history.pushState({suggestion: id}, "", `/albums?suggestion=${id}`);
  document.title = `Photo Sorter - ${sugg.name}`;
  $("list-view").hidden = true; $("album-view").hidden = true; $("sugg-view").hidden = false;
  $("sugg-title").textContent = sugg.name;
  const params = extra => { const p = new URLSearchParams({suggestion: sugg.id, ...extra}); for (const [k, v] of [...p]) if (v === "") p.delete(k); return p; };
  if (!sgrid) {
    sgrid = new PS.PhotoGrid($("sugg-grid"), {
      fetchPage: (offset, limit, after) => api(`/api/search?${params(after ? {after, limit} : {offset, limit})}`), more: $("sugg-more"),
      fetchIds: async () => (await api(`/api/search?${params({ids_only: true})}`)).ids,
      say, label: "Photos in this suggestion", onSelection: () => supdate()});
    supdate = suggBar();
  } else sgrid.clearSelection();
  await suggReload();
  $("sugg-back").focus();
}
route();
