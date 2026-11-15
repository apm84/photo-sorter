"use strict";
// v0.6 player page: plays the video (as is, or the H.264 copy for formats browsers can't play), scrubbing with
// thumbnail previews from the sprite sheet, people found in it (click to jump), Up next with autoplay.
const {$, esc, api, fmtDate, fmtDur, fmtBytes} = PS;
const v = $("video");
let cur = null, list = [], info = null, poll = 0, nextTimer = 0, seeking = false;
const say = t => { $("status").textContent = t; };

function loadList() {
  try { const s = JSON.parse(sessionStorage.getItem("ps-upnext") || "null"); if (s && Array.isArray(s.ids)) return s.ids; } catch {}
  return null;
}
async function ensureList(id) {
  list = loadList();
  if (!list || !list.includes(id)) {
    const r = await api("/api/search?media=video&hide_live=true&ids_only=true");
    list = r.ids.includes(id) ? r.ids : [id, ...r.ids];
  }
}
function nextId() { const i = list.indexOf(cur); return i >= 0 && i + 1 < list.length ? list[i + 1] : null; }

async function renderUpNext() {
  const i = list.indexOf(cur);
  const ids = list.slice(i + 1, i + 21);
  const ol = $("upnext");
  if (!ids.length) { ol.innerHTML = `<li class="status">Nothing after this one.</li>`; return; }
  const items = await Promise.all(ids.map(id => api(`/api/file/${id}`).catch(() => null)));
  ol.innerHTML = items.filter(Boolean).map(it => `<li><a href="/watch?id=${it.id}" data-id="${it.id}" class="upnext-item">
      <span class="upnext-thumb"><img loading="lazy" src="/thumb/${it.id}.jpg" alt=""><span class="card-dur">${esc(fmtDur(it.duration))}</span></span>
      <span class="upnext-text"><span class="vtitle">${esc(fmtDate(it.taken_at))}${it.place ? " \u00b7 " + esc(it.place) : ""}</span>
      <span class="vsub">${esc(it.filename || "")}</span></span></a></li>`).join("");
}

function setWait(text) { $("wait").hidden = !text; $("wait").textContent = text || ""; }

async function load(id, push) {
  clearTimeout(poll); cancelNext();
  cur = id;
  if (push) history.pushState({id}, "", `/watch?id=${id}`);
  let r;
  try { r = await api(`/api/video/${id}`); } catch (e) { setWait(`Can't open this video: ${e.message}`); return; }
  if (cur !== id) return;
  info = r;
  document.title = `Photo Sorter - ${fmtDate(r.taken_at)}`;
  $("title").textContent = fmtDate(r.taken_at) + (r.place ? ` \u00b7 ${r.place}` : "");
  $("meta").textContent = [r.filename, r.width && r.height ? `${r.width}\u00d7${r.height}` : "", fmtDur(r.duration),
    fmtBytes(r.size), [r.camera_make, r.camera_model].filter(Boolean).join(" "), r.live ? "Live Photo clip" : "",
    r.tags && r.tags.length ? r.tags.slice(0, 5).join(", ") : ""].filter(Boolean).join(" \u00b7 ");
  $("people").innerHTML = (r.people || []).map(p => `<button type="button" class="chip" data-t="${p.t ?? 0}"
    title="Jump to where they appear">${esc(p.label)}</button>`).join("");
  $("original").href = `/video/${id}/original`;
  $("undo").hidden = true;
  v.poster = `/preview/${id}.jpg`;
  if (r.ready) {
    setWait("");
    if (v.getAttribute("src") !== r.stream) { v.src = r.stream; }
    v.play().catch(() => {});
  } else {
    v.removeAttribute("src"); v.load();
    if (r.preparing) {
      setWait(`Making a copy your browser can play (this video is ${(r.vcodec || "an unusual format").toUpperCase()}). ` +
              "It starts by itself in a moment.");
      poll = setTimeout(() => load(id, false), 2000);
    } else setWait(r.ffmpeg ? `This video can't be played here${r.error ? ` (${r.error})` : ""}. Use Open original.`
                            : "Video tools (ffmpeg) are missing, so videos can't be played here. Use Open original.");
  }
  renderUpNext();
}

// ---- controls ----
function updTime() {
  const d = v.duration || (info && info.duration) || 0;
  $("ctime").textContent = `${fmtDur(v.currentTime)} / ${fmtDur(d)}`;
  const pct = d ? (v.currentTime / d) * 100 : 0;
  if (!seeking) { $("seekfill").style.width = pct + "%"; $("seekknob").style.left = pct + "%"; }
  const s = $("seek"); s.setAttribute("aria-valuemax", String(Math.round(d))); s.setAttribute("aria-valuenow", String(Math.round(v.currentTime)));
  s.setAttribute("aria-valuetext", fmtDur(v.currentTime));
  if (v.buffered.length && d) $("seekbuf").style.width = (v.buffered.end(v.buffered.length - 1) / d) * 100 + "%";
}
function updPlay() { $("play").innerHTML = v.paused ? "&#9654;" : "&#10074;&#10074;"; $("play").setAttribute("aria-label", v.paused ? "Play (K)" : "Pause (K)"); }
function updMute() { $("mute").innerHTML = v.muted || v.volume === 0 ? "&#128263;" : "&#128266;"; $("mute").setAttribute("aria-label", v.muted ? "Unmute (M)" : "Mute (M)"); }
const toggle = () => { if (!v.getAttribute("src")) return; v.paused ? v.play().catch(() => {}) : v.pause(); };
v.addEventListener("timeupdate", updTime); v.addEventListener("progress", updTime); v.addEventListener("loadedmetadata", updTime);
v.addEventListener("play", updPlay); v.addEventListener("pause", updPlay); v.addEventListener("volumechange", updMute);
v.addEventListener("click", toggle);
$("play").onclick = toggle;
$("mute").onclick = () => { v.muted = !v.muted; };
$("volume").oninput = e => { v.volume = +e.target.value; v.muted = v.volume === 0; };
$("fullscreen").onclick = () => { document.fullscreenElement ? document.exitFullscreen() : $("player").requestFullscreen().catch(() => {}); };
$("nextbtn").onclick = () => { const n = nextId(); if (n) load(n, true); };
$("autoplay").checked = localStorage.getItem("ps-autoplay") !== "0";
$("autoplay").onchange = e => localStorage.setItem("ps-autoplay", e.target.checked ? "1" : "0");

// seek bar with thumbnail previews from the sprite sheet
function posAt(e) { const r = $("seek").getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); }
function showTip(frac) {
  const d = v.duration || (info && info.duration) || 0, t = frac * d, tip = $("seektip");
  tip.hidden = false; $("seektime").textContent = fmtDur(t);
  const sp = info && info.sprite, th = $("seekthumb");
  if (sp) {
    const k = Math.min(sp.n - 1, Math.floor(t / sp.interval));
    th.hidden = false; th.style.width = sp.tw + "px"; th.style.height = sp.th + "px";
    th.style.backgroundImage = `url(${sp.url})`;
    th.style.backgroundPosition = `-${(k % sp.cols) * sp.tw}px -${Math.floor(k / sp.cols) * sp.th}px`;
  } else th.hidden = true;
  const w = $("seek").clientWidth, tw = tip.offsetWidth;
  tip.style.left = Math.min(w - tw / 2, Math.max(tw / 2, frac * w)) + "px";
}
const seek = $("seek");
seek.addEventListener("pointermove", e => { showTip(posAt(e)); if (seeking) { const f = posAt(e); $("seekfill").style.width = f * 100 + "%"; $("seekknob").style.left = f * 100 + "%"; } });
seek.addEventListener("pointerleave", () => { if (!seeking) $("seektip").hidden = true; });
seek.addEventListener("pointerdown", e => { seeking = true; seek.setPointerCapture(e.pointerId); showTip(posAt(e)); });
seek.addEventListener("pointerup", e => {
  if (!seeking) return; seeking = false;
  const d = v.duration || 0; if (d) v.currentTime = posAt(e) * d;
  $("seektip").hidden = true;
});
seek.addEventListener("keydown", e => {
  const d = v.duration || 0; if (!d) return;
  if (e.key === "ArrowRight") { v.currentTime = Math.min(d, v.currentTime + 5); e.preventDefault(); }
  else if (e.key === "ArrowLeft") { v.currentTime = Math.max(0, v.currentTime - 5); e.preventDefault(); }
  else if (e.key === "Home") { v.currentTime = 0; e.preventDefault(); }
  else if (e.key === "End") { v.currentTime = d - 0.1; e.preventDefault(); }
});
document.addEventListener("keydown", e => {
  if (e.target.closest("input, select, textarea, dialog, .seek") || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === " " || k === "k") { e.preventDefault(); toggle(); }
  else if (k === "m") v.muted = !v.muted;
  else if (k === "f") $("fullscreen").click();
  else if (k === "arrowright" || k === "l") v.currentTime = Math.min(v.duration || 0, v.currentTime + (k === "l" ? 10 : 5));
  else if (k === "arrowleft" || k === "j") v.currentTime = Math.max(0, v.currentTime - (k === "j" ? 10 : 5));
  else if (k === "n" && e.shiftKey) $("nextbtn").click();
});

// autoplay next: a short countdown that can be cancelled
function cancelNext() { clearInterval(nextTimer); $("nextbox").hidden = true; }
v.addEventListener("ended", async () => {
  const n = nextId();
  if (!n || !$("autoplay").checked) return;
  let it = null; try { it = await api(`/api/file/${n}`); } catch {}
  $("nexttitle").textContent = it ? fmtDate(it.taken_at) + (it.place ? ` \u00b7 ${it.place}` : "") : "";
  let c = 5; $("nextcount").textContent = c; $("nextbox").hidden = false; $("nextnow").focus();
  nextTimer = setInterval(() => { c--; $("nextcount").textContent = c; if (c <= 0) { cancelNext(); load(n, true); } }, 1000);
});
$("nextcancel").onclick = cancelNext;
$("nextnow").onclick = () => { const n = nextId(); cancelNext(); if (n) load(n, true); };

$("people").addEventListener("click", e => { const b = e.target.closest(".chip"); if (b && v.getAttribute("src")) { v.currentTime = +b.dataset.t; v.play().catch(() => {}); } });
$("upnext").addEventListener("click", e => { const a = e.target.closest("a[data-id]"); if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey) { e.preventDefault(); load(+a.dataset.id, true); } });
$("folder").onclick = () => PS.showInFolder(cur, say);
$("addalbum").onclick = () => PS.addToAlbum([cur], say);
$("delete").onclick = async () => {
  const r = await PS.deleteFiles([cur], say);
  if (!r) return;
  v.pause(); $("undo").hidden = false;
  const n = nextId(); list = list.filter(x => x !== cur);
  if (n) { say("Deleted. Playing the next video; Undo is still available below."); await load(n, true); $("undo").hidden = false; }
};
$("undo").onclick = async () => { $("undo").hidden = true; await PS.undoLast(say); };
window.addEventListener("popstate", e => { const id = (e.state && e.state.id) || +new URLSearchParams(location.search).get("id"); if (id) load(id, false); });

(async () => {
  const id = +new URLSearchParams(location.search).get("id");
  if (!id) { setWait("No video chosen. Pick one on the Videos page."); return; }
  await ensureList(id);
  history.replaceState({id}, "", location.href);
  load(id, false);
})();
