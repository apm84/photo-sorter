"use strict";
const {$, esc, api} = PS;
const say = t => { $("status").textContent = t; };
let current = null, grid = null, dates = null, updateBar = () => {};

async function loadList() {
  const {albums} = await api("/api/albums");
  const box = $("albums"); box.replaceChildren();
  if (!albums.length) { box.innerHTML = `<p class="status empty">No albums yet.</p>`; return; }
  for (const a of albums) {
    const el = document.createElement("a");
    el.className = "named-card album-card"; el.href = `/albums?album=${a.id}`; el.setAttribute("role", "listitem");
    el.innerHTML = `${a.cover ? `<img loading="lazy" src="/thumb/${a.cover}.jpg" alt="">` : `<span class="noface" aria-hidden="true">&#9633;</span>`}
      <span class="named-name">${esc(a.name)}</span><span class="named-count">${PS.plural(a.count, "photo")}</span>`;
    el.onclick = e => { if (e.ctrlKey || e.metaKey || e.shiftKey) return; e.preventDefault(); openAlbum(a.id, true); };
    box.appendChild(el);
  }
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
