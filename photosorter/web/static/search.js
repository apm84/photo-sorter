"use strict";
const {$, esc, api} = PS;
const SELECTS = ["type", "month", "state", "place", "person", "tag", "album"];
const CHECKS = ["duplicates", "unknown_date"];
let dates = null, grid = null, ctrl = null, timer = 0, liveTimer = 0, lastClip = false;
const say = t => { $("status").textContent = t; };

function fill(sel, items, labelKey) {
  for (const it of items) { const o = document.createElement("option"); o.value = it.value ?? "";
    o.textContent = `${it[labelKey] ?? it.value ?? "(none)"} (${it.n})`; sel.appendChild(o); }
}
function params() {
  const p = new URLSearchParams();
  if ($("q").value.trim()) p.set("q", $("q").value.trim());
  for (const k of SELECTS) if ($(k).value) p.set(k, $(k).value);
  for (const k of CHECKS) if ($(k).checked) p.set(k, "true");
  const d = dates.get();
  if (d.date_from) p.set("date_from", d.date_from);
  if (d.date_to) p.set("date_to", d.date_to);
  return p;
}
function urlParams() {
  const p = params();
  const pre = dates.preset();
  if (pre && pre !== "custom") { p.delete("date_from"); p.delete("date_to"); p.set("when", pre); }
  return p;
}
async function fetchPage(offset, limit, after) {
  if (offset === 0) { if (ctrl) ctrl.abort(); ctrl = new AbortController(); }   // a new search cancels the old one
  const p = params(); p.set("limit", limit);
  if (after) p.set("after", after); else p.set("offset", offset);
  const r = await (await fetch("/api/search?" + p, {signal: ctrl.signal})).json();
  lastClip = r.clip;
  if (offset === 0) {
    clearTimeout(liveTimer);                  // announce the count, but not on every keystroke
    liveTimer = setTimeout(() => say(`${r.total.toLocaleString()} matches` + (r.clip && $("q").value ? " (best matches first)" : "") +
      (r.person != null ? " - photos of this person" : "") + (r.album != null ? " - photos in this album" : "")), 400);
  }
  return r;
}
async function fetchIds() {
  const p = params(); p.set("ids_only", "true");
  return (await api("/api/search?" + p)).ids;
}
async function search() {
  history.replaceState(null, "", "?" + urlParams());
  try { await grid.load(); }
  catch (e) { if (e.name !== "AbortError") say("Search failed"); return; }
  updateBar();
}
const searchSoon = () => { clearTimeout(timer); timer = setTimeout(search, 200); };

function clearFilters() {
  $("q").value = "";
  for (const k of SELECTS) $(k).value = "";
  for (const k of CHECKS) $(k).checked = false;
  dates.reset();
  closeSuggest();
  search();
  say("Filters cleared");
  $("q").focus();
}

// ---- autosuggest (combobox: arrows / Enter / Esc) ----
let sugg = [], active = -1, sctrl = null;
const q = $("q"), list = $("suggest");
function closeSuggest() { list.hidden = true; q.setAttribute("aria-expanded", "false"); q.removeAttribute("aria-activedescendant"); active = -1; }
function setActive(i) {
  active = i;
  for (const [k, li] of [...list.children].entries()) li.setAttribute("aria-selected", String(k === i));
  if (i >= 0) { q.setAttribute("aria-activedescendant", `sg-${i}`); list.children[i].scrollIntoView({block: "nearest"}); }
  else q.removeAttribute("aria-activedescendant");
}
async function loadSuggest() {
  const text = q.value.trim();
  if (sctrl) sctrl.abort();
  if (text.length < 1) { closeSuggest(); return; }
  sctrl = new AbortController();
  let s;
  try { s = await (await fetch("/api/suggest?q=" + encodeURIComponent(text), {signal: sctrl.signal})).json(); }
  catch { return; }
  sugg = [...s.people.map(x => ({kind: "Person", field: "person", value: String(x.id), label: x.label, n: x.n})),
          ...(s.albums || []).map(x => ({kind: "Album", field: "album", value: String(x.id), label: x.label, n: x.n})),
          ...s.places.map(x => ({kind: "Place", field: "place", value: x.value, label: x.value, n: x.n})),
          ...s.tags.map(x => ({kind: "Tag", field: "tag", value: x.value, label: x.value, n: x.n}))];
  if (!sugg.length) { closeSuggest(); return; }
  list.innerHTML = sugg.map((x, i) => `<li id="sg-${i}" role="option" aria-selected="false" data-i="${i}">
    <span>${esc(x.label)}</span><span class="kind">${x.kind} &middot; ${x.n}</span></li>`).join("");
  list.hidden = false; q.setAttribute("aria-expanded", "true"); active = -1;
}
function pick(i) {
  const x = sugg[i]; if (!x) return;
  const sel = $(x.field);
  if (![...sel.options].some(o => o.value === x.value)) { const o = document.createElement("option"); o.value = x.value; o.textContent = x.label; sel.appendChild(o); }
  sel.value = x.value; q.value = ""; closeSuggest(); search();
  say(`Showing ${x.kind.toLowerCase()}: ${x.label}`);
}
q.addEventListener("input", () => { searchSoon(); clearTimeout(q._st); q._st = setTimeout(loadSuggest, 120); });
q.addEventListener("keydown", e => {
  const open = !list.hidden;
  if (e.key === "ArrowDown") { e.preventDefault(); if (!open) { loadSuggest(); return; } setActive((active + 1) % sugg.length); }
  else if (e.key === "ArrowUp" && open) { e.preventDefault(); setActive(active <= 0 ? sugg.length - 1 : active - 1); }
  else if (e.key === "Enter") { e.preventDefault(); if (open && active >= 0) pick(active); else { closeSuggest(); clearTimeout(timer); search(); } }
  else if (e.key === "Escape" && open) { e.preventDefault(); closeSuggest(); }
});
list.addEventListener("mousedown", e => { const li = e.target.closest("li"); if (li) { e.preventDefault(); pick(+li.dataset.i); } });
q.addEventListener("blur", () => setTimeout(closeSuggest, 100));

let updateBar = () => {};
async function init() {
  const f = await api("/api/facets");
  $("total").textContent = `${f.total.toLocaleString()} files indexed` + (f.clip_search ? " - AI search on" : " - AI search off (keyword only)");
  fill($("type"), f.types); fill($("state"), f.states); fill($("place"), f.places);
  fill($("person"), f.people, "label"); fill($("tag"), f.tags); fill($("album"), f.albums || [], "label");
  for (let m = 1; m <= 12; m++) { const o = document.createElement("option"); o.value = String(m).padStart(2, "0");
    o.textContent = new Date(2000, m - 1, 1).toLocaleString(undefined, {month: "long"}); $("month").appendChild(o); }
  dates = PS.dateFilter($("dates"), f.years, () => { if (dates.active()) $("unknown_date").checked = false; search(); });
  grid = new PS.PhotoGrid($("grid"), {fetchPage, fetchIds, say, label: "Photos", more: $("more"),
    onSelection: () => updateBar()});
  updateBar = PS.selectionBar($("selbar"), grid, say);
  const u = new URLSearchParams(location.search);
  for (const [k, v] of u) {
    if (SELECTS.includes(k) || k === "q") {
      const el = $(k);
      if (k !== "q" && ![...el.options].some(o => o.value === v)) { const o = document.createElement("option"); o.value = v; o.textContent = v; el.appendChild(o); }
      el.value = v;
    } else if (CHECKS.includes(k)) $(k).checked = v === "true";
  }
  if (u.get("when")) dates.set(...PS.presetRange(u.get("when")), u.get("when"));
  else if (u.get("year")) dates.set(...PS.presetRange("y" + u.get("year")), "y" + u.get("year"));   // old links
  else if (u.get("date_from") || u.get("date_to")) dates.set(u.get("date_from"), u.get("date_to"), "custom");
  search();
}
$("filters").addEventListener("submit", e => { e.preventDefault(); search(); });
$("clear").onclick = clearFilters;
for (const k of SELECTS) $(k).addEventListener("change", () => search());
$("duplicates").addEventListener("change", () => search());
$("unknown_date").addEventListener("change", () => { if ($("unknown_date").checked) dates.reset(); search(); });
// the grid loads more automatically near the bottom of the page (Load more stays as a fallback)
init();
