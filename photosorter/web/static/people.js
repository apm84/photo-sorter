"use strict";
const {$, esc, api} = PS;
const post = (url, body) => api(url, body ?? {});
const say = t => { $("status").textContent = t; };
let people = [], gallery = null, gdates = null, current = null, updateBar = () => {};

// ---- named people grid ------------------------------------------------------------------------------------
async function loadNamed() {
  const all = await api("/api/people/named");
  const box = $("named");
  box.replaceChildren();
  if (!all.length) {
    box.innerHTML = `<p class="status empty">No named people yet. Open <b>Face groups &amp; settings</b> below and type a name
      under a face group.</p>`;
    return;
  }
  for (const p of all) {
    const a = document.createElement("a");
    a.className = "named-card"; a.href = `/people?person=${p.id}`; a.setAttribute("role", "listitem");
    a.innerHTML = `${p.cover_face ? `<img loading="lazy" src="/face/${p.cover_face}.jpg" alt="">` : `<span class="noface" aria-hidden="true">?</span>`}
      <span class="named-name">${esc(p.name)}</span><span class="named-count">${PS.plural(p.photos, "photo")}</span>`;
    a.onclick = e => { if (e.ctrlKey || e.metaKey || e.shiftKey) return; e.preventDefault(); openPerson(p.id, true); };
    box.appendChild(a);
  }
}

// ---- one person's gallery -----------------------------------------------------------------------------------
async function openPerson(pid, push) {
  const info = await api(`/api/people/${pid}/info`).catch(() => null);
  if (!info) { showNamed(push); return; }
  current = info;
  if (push) history.pushState({person: pid}, "", `/people?person=${pid}`);
  document.title = `Photo Sorter - ${info.name || "Person " + pid}`;
  $("named-view").hidden = true; $("gallery-view").hidden = false;
  $("gallery-title").textContent = info.name || `Person ${pid}`;
  const cover = await api("/api/people/named").then(l => (l.find(x => x.id === pid) || {}).cover_face).catch(() => null);
  $("gallery-face").hidden = !cover; if (cover) $("gallery-face").src = `/face/${cover}.jpg`;
  gdates = PS.dateFilter($("gallery-dates"), info.years, reloadGallery, "gdf");
  if (!gallery) {
    gallery = new PS.PhotoGrid($("gallery-grid"), {
      fetchPage: (offset, limit, after) => api(`/api/search?${galleryParams(after ? {after, limit} : {offset, limit})}`), more: $("gallery-more"),
      fetchIds: async () => (await api(`/api/search?${galleryParams({ids_only: true})}`)).ids,
      say, label: "Photos of this person", onSelection: () => updateBar()});
    updateBar = PS.selectionBar($("gallery-selbar"), gallery, say);
  } else gallery.clearSelection();
  await reloadGallery();
  $("back").focus();
}
function galleryParams(extra) {
  const p = new URLSearchParams({person: current.id, ...gdates.get(), ...extra});
  for (const [k, v] of [...p]) if (v === "") p.delete(k);
  return p;
}
async function reloadGallery() {
  const r = await gallery.load();
  if (r) $("gallery-count").textContent = PS.plural(r.total, "photo") + (gdates.active() ? " in this period" : "");
  updateBar();
}
function showNamed(push) {
  current = null;
  if (push) history.pushState({}, "", "/people");
  document.title = "Photo Sorter - People";
  $("gallery-view").hidden = true; $("named-view").hidden = false;
  loadNamed();
}
$("back").onclick = () => showNamed(true);
window.addEventListener("popstate", () => route());
function route() {
  const pid = +new URLSearchParams(location.search).get("person");
  if (pid) openPerson(pid, false); else showNamed(false);
}

// ---- face groups & settings (inside the collapsible section) ------------------------------------------------
function faceImg(f, removable) {
  return `<span class="face" data-face="${f}"><img loading="lazy" decoding="async" width="80" height="80" src="/face/${f}.jpg" alt="face">` +
    (removable ? `<button class="rm" aria-label="Not this person" title="Not this person">&times;</button>` : "") + `</span>`;
}
async function loadGroups() {
  const all = await api("/api/people" + ($("showhidden").checked ? "?hidden=1" : ""));
  people = all;
  $("settings-sum").textContent = `(${PS.plural(all.length, "group")})`;
  $("names").innerHTML = [...new Set(all.filter(p => p.name).map(p => p.name))].map(n => `<option value="${esc(n)}">`).join("");
  const box = $("people"); box.replaceChildren();
  if (!all.length) box.textContent = "No face groups yet. Run the sorter with face recognition on.";
  const opts = all.map(p => `<option value="${p.id}">${esc(p.name || "Person " + p.id)} (${p.face_count})</option>`).join("");
  groupQueue = all.slice(); groupOpts = opts;
  renderGroups();
  loadUnassigned();
}
// face groups are drawn 30 at a time as you scroll (hundreds of groups x 12 faces would be slow to draw at once)
let groupQueue = [], groupOpts = "";
function renderGroups(n = 30) {
  const box = $("people"), all = people;
  for (const p of groupQueue.splice(0, n)) {
    const d = document.createElement("section"); d.className = "person";
    d.innerHTML = `<h4>${esc(p.name || "Person " + p.id)} <span class="status">${p.face_count} faces &middot;
        <a href="/people?person=${p.id}">photos</a>${p.hidden ? " &middot; hidden" : ""}</span></h4>
      <div class="faces">${p.faces.map(f => faceImg(f, true)).join("")}</div>
      ${p.face_count > p.faces.length ? `<button class="more">Show all ${p.face_count} faces</button>` : ""}
      <form class="row"><input name="n" list="names" placeholder="Who is this?" aria-label="Name" value="${esc(p.name || "")}"><button>Save</button></form>
      <div class="row"><select class="into" aria-label="Merge into"><option value="">Merge into...</option></select>
      <button class="merge">Merge</button><button class="hide">${p.hidden ? "Unhide" : "Hide"}</button></div>`;
    d.querySelector("form").onsubmit = async e => {
      e.preventDefault();
      const name = d.querySelector("input").value.trim();
      const same = name && all.find(o => o.id !== p.id && (o.name || "").toLowerCase() === name.toLowerCase());
      if (same && confirm(`"${same.name}" already exists. Merge this group into it?`)) await post(`/api/people/${p.id}/merge`, {into: same.id});
      else await post(`/api/people/${p.id}/name`, {name});
      say(name ? `Saved ${name}` : "Name cleared"); refreshAll();
    };
    d.querySelector(".merge").onclick = async () => {
      const into = +d.querySelector(".into").value;
      if (into && into !== p.id) { await post(`/api/people/${p.id}/merge`, {into}); say("Merged"); refreshAll(); }
    };
    d.querySelector(".hide").onclick = async () => { await post(`/api/people/${p.id}/${p.hidden ? "unhide" : "hide"}`); refreshAll(); };
    const more = d.querySelector(".more");
    if (more) more.onclick = async () => {
      const r = await api(`/api/people/${p.id}/faces?limit=500`);
      d.querySelector(".faces").innerHTML = r.faces.map(f => faceImg(f.id, true)).join(""); more.remove(); wire(d);
    };
    const into = d.querySelector(".into");   // the long list of groups is only built when the menu is used
    const fillInto = () => { if (into.options.length < 2) into.insertAdjacentHTML("beforeend", groupOpts); };
    into.addEventListener("focus", fillInto); into.addEventListener("pointerdown", fillInto);
    wire(d);
    box.appendChild(d);
  }
  $("groups-more").hidden = !groupQueue.length;
}
$("groups-more").onclick = () => renderGroups();
new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && groupQueue.length && $("settings").open) renderGroups(); },
  {rootMargin: "800px 0px"}).observe($("groups-more").parentElement);
function wire(d) {
  for (const b of d.querySelectorAll(".faces .rm")) b.onclick = async () => {
    const span = b.closest(".face"); await post(`/api/faces/${span.dataset.face}/remove`); span.remove(); say("Face removed from this person");
  };
}
async function loadUnassigned() {
  const r = await api("/api/faces/unassigned?limit=60");
  $("unc").textContent = r.total ? `(${r.total}; showing the clearest ${r.faces.length})` : "(none)";
  const box = $("unassigned"); box.replaceChildren();
  for (const f of r.faces) {
    const d = document.createElement("form"); d.className = "person row";
    d.innerHTML = `${faceImg(f.id, false)}<input list="names" placeholder="Name" aria-label="Name for this face"><button>Assign</button>`;
    d.onsubmit = async e => { e.preventDefault(); const name = d.querySelector("input").value.trim(); if (!name) return;
      await post(`/api/faces/${f.id}/assign`, {name}); say(`Assigned to ${name}`); refreshAll(); };
    box.appendChild(d);
  }
}
async function loadBorderline() {
  const r = await api("/api/faces/borderline?limit=24");
  $("border-count").textContent = r.total ? `(${r.total} to check)` : "(nothing to check)";
  const box = $("border"); box.replaceChildren();
  for (const f of r.faces) {
    const d = document.createElement("article"); d.className = "bl"; d.setAttribute("role", "listitem");
    d.innerHTML = `<div class="bl-imgs"><img src="/face/${f.id}.jpg" alt="Face" class="bl-face">
        <button type="button" class="bl-photo" aria-label="Open the photo"><img loading="lazy" src="/thumb/${f.file_id}.jpg" alt=""></button></div>
      <p class="bl-q">Is this <b>${esc(f.name)}</b>?</p>
      <div class="row"><button type="button" class="primary yes">Yes</button><button type="button" class="no">No</button></div>`;
    const answer = async yes => {
      try { await post(`/api/faces/${f.id}/confirm`, {person: f.person, yes}); }
      catch (e) { say(e.message); return; }
      d.remove(); say(yes ? `Added to ${f.name}` : `Noted: not ${f.name}`);
      const left = box.querySelectorAll(".bl").length;
      $("border-count").textContent = `(${Math.max(0, r.total - (r.faces.length - left))} to check)`;
      if (!left) loadBorderline();
      if (yes) loadNamed();
    };
    d.querySelector(".yes").onclick = () => answer(true);
    d.querySelector(".no").onclick = () => answer(false);
    d.querySelector(".bl-photo").onclick = () => window.open(`/full/${f.file_id}`, "_blank", "noopener");
    box.appendChild(d);
  }
}
async function loadParams() {
  const p = await api("/api/people/regroup");
  $("eps").value = p.eps; $("epsv").textContent = (+p.eps).toFixed(2); $("method").value = p.method; $("ms").value = p.min_samples;
  $("lowq").textContent = p.low_quality.toLocaleString();
  $("q-px").value = p.quality.min_px; $("q-score").value = p.quality.min_score; $("q-blur").value = p.quality.min_blur;
}
$("eps").oninput = () => { $("epsv").textContent = (+$("eps").value).toFixed(2); };
$("method").onchange = () => {   // suggested starting strictness per method
  if ($("method").value === "agglomerative" && +$("eps").value > 0.5) $("eps").value = 0.45;
  if ($("method").value === "dbscan" && +$("eps").value < 0.4) $("eps").value = 0.4;
  $("epsv").textContent = (+$("eps").value).toFixed(2);
};
$("regroup").onclick = async () => {
  say("Re-grouping faces...");
  $("regroup").disabled = true;
  try {
    const r = await post("/api/people/regroup", {eps: +$("eps").value, method: $("method").value, min_samples: +$("ms").value,
      min_px: +$("q-px").value, min_score: +$("q-score").value, min_blur: +$("q-blur").value});
    say(`${PS.plural(r.people, "group")}; ${r.low_quality} low-quality faces skipped; ${r.borderline} faces to check`);
    refreshAll(); loadParams();
  } catch (e) { say(e.message); } finally { $("regroup").disabled = false; }
};
$("showhidden").onchange = loadGroups;
$("folders").onclick = async () => { const r = await post("/api/people/build-folders"); say(`${r.links} links added under People\\`); };
let groupsLoaded = false;
$("settings").addEventListener("toggle", () => {   // the (possibly long) group list loads when first opened
  if ($("settings").open && !groupsLoaded) { groupsLoaded = true; loadGroups(); loadBorderline(); loadParams(); }
});
function refreshAll() { loadNamed(); if (groupsLoaded) { loadGroups(); loadBorderline(); } }
if (location.hash === "#settings") $("settings").open = true;
route();
api("/api/faces/borderline?limit=1").then(r => { if (r.total) $("settings-sum").textContent = `(${r.total} faces to check)`; });
