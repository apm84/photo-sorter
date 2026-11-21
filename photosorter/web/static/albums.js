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

async function loadList() {
  const {albums} = await api("/api/albums");
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
    el.innerHTML = `${a.cover ? `<img loading="lazy" src="/thumb/${a.cover}.jpg" alt="">` : `<span class="noface" aria-hidden="true">&#9633;</span>`}
      <span class="named-name">${esc(a.name)}</span><span class="named-count">${PS.plural(a.count, "photo")}</span>`;
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
  $("list-view").hidden = true; $("album-view").hidden = false;
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
  $("album-view").hidden = true; $("list-view").hidden = false;
  loadList();
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
function route() { const id = +new URLSearchParams(location.search).get("album"); if (id) openAlbum(id, false); else showList(false); }
route();
