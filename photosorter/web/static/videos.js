"use strict";
// v0.6 Videos tab: YouTube-style grid (poster, duration, hover flip-book), its own search and filters, select,
// add to album and delete like photos. Opening a video goes to the player page (/watch) with this list as Up next.
const {$, esc, api} = PS;
const SELECTS = ["place", "person", "album"];
let dates = null, grid = null, ctrl = null, timer = 0;
const say = t => { $("status").textContent = t; };

function fill(sel, items, labelKey) {
  for (const it of items) { const o = document.createElement("option"); o.value = it.value ?? "";
    o.textContent = `${it[labelKey] ?? it.value ?? "(none)"}${it.n != null ? ` (${it.n})` : ""}`; sel.appendChild(o); }
}
function params() {
  const p = new URLSearchParams({media: "video"});
  if ($("q").value.trim()) p.set("q", $("q").value.trim());
  for (const k of SELECTS) if ($(k).value) p.set(k, $(k).value);
  if ($("hide_live").checked) p.set("hide_live", "true");
  if ($("duplicates").checked) p.set("duplicates", "true");
  const d = dates.get();
  if (d.date_from) p.set("date_from", d.date_from);
  if (d.date_to) p.set("date_to", d.date_to);
  return p;
}
async function fetchPage(offset, limit, after) {
  if (offset === 0) { if (ctrl) ctrl.abort(); ctrl = new AbortController(); }
  const p = params(); p.set("limit", limit);
  if (after) p.set("after", after); else p.set("offset", offset);
  const r = await (await fetch("/api/search?" + p, {signal: ctrl.signal})).json();
  if (offset === 0) say(`${r.total.toLocaleString()} video${r.total === 1 ? "" : "s"}` + (r.clip && $("q").value ? " (best matches first)" : ""));
  return r;
}
async function fetchIds() { const p = params(); p.set("ids_only", "true"); return (await api("/api/search?" + p)).ids; }
async function search() {
  const u = params(); u.delete("media");
  history.replaceState(null, "", "?" + u);
  try { await grid.load(); } catch (e) { if (e.name !== "AbortError") say("Search failed"); return; }
  updateBar();
}
let updateBar = () => {};

async function init() {
  const f = await api("/api/facets");
  $("total").textContent = `${f.videos.toLocaleString()} videos` + (f.live_videos ? ` (${f.live_videos.toLocaleString()} Live Photo clips)` : "");
  $("nofmpeg").hidden = f.ffmpeg;
  fill($("place"), f.places); fill($("person"), f.people, "label"); fill($("album"), f.albums || [], "label");
  dates = PS.dateFilter($("dates"), f.years, search, "vdf");
  grid = new PS.PhotoGrid($("grid"), {fetchPage, fetchIds, say, label: "Videos", more: $("more"), videoLayout: true,
    onSelection: () => updateBar(), onOpenVideo: it => { location.href = PS.watchUrl(grid, it.id); }});
  updateBar = PS.selectionBar($("selbar"), grid, say);
  const u = new URLSearchParams(location.search);
  if (u.has("q")) $("q").value = u.get("q");
  for (const k of SELECTS) if (u.get(k)) {
    const el = $(k);
    if (![...el.options].some(o => o.value === u.get(k))) { const o = document.createElement("option"); o.value = u.get(k); o.textContent = u.get(k); el.appendChild(o); }
    el.value = u.get(k);
  }
  if (u.has("hide_live")) $("hide_live").checked = u.get("hide_live") === "true";
  else if (u.toString()) $("hide_live").checked = false;
  $("duplicates").checked = u.get("duplicates") === "true";
  if (u.get("date_from") || u.get("date_to")) dates.set(u.get("date_from"), u.get("date_to"), "custom");
  search();
}
$("filters").addEventListener("submit", e => { e.preventDefault(); search(); });
$("q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 250); });
for (const k of [...SELECTS, "hide_live", "duplicates"]) $(k).addEventListener("change", search);
$("clear").onclick = () => {
  $("q").value = ""; for (const k of SELECTS) $(k).value = ""; $("duplicates").checked = false; $("hide_live").checked = true;
  dates.reset(); search(); say("Filters cleared");
};
init();
