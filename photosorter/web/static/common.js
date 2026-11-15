"use strict";
// Shared helpers for every Photo Sorter page: API calls, natural dates, the photo grid (select circle,
// show-in-folder icon, shift-click ranges), the date range filter, the photo viewer and "Add to album".
// v0.6: video cards (duration badge, hover flip-book from the scrub sprite), videos in the viewer, and
// "Delete" on any selection (sorted copies to the Recycle Bin, originals untouched, one-click Undo).
// Everything is served locally; no third-party scripts.
const PS = (() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  async function api(url, body) {
    const r = await fetch(url, body === undefined ? {} : {method: "POST",
      headers: {"X-PhotoSorter": "1", "Content-Type": "application/json"}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    return j;
  }
  // AP-style months: "Nov. 13, 2026", "Sept. 2, 2025", "May 4, 2024"
  const MONTHS = ["Jan.", "Feb.", "March", "April", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."];
  function fmtDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    if (!m) return "No date";
    return `${MONTHS[+m[2] - 1]} ${+m[3]}, ${m[1]}`;
  }
  function fmtBytes(b) {
    if (b == null) return "";
    if (b >= 1073741824) return (b / 1073741824).toFixed(1) + " GB";
    if (b >= 1048576) return (b / 1048576).toFixed(1) + " MB";
    if (b >= 1024) return Math.round(b / 1024) + " KB";
    return b + " bytes";
  }
  const plural = (n, word, many) => `${n.toLocaleString()} ${n === 1 ? word : (many || word + "s")}`;
  function fmtDur(sec) {
    if (sec == null || !isFinite(sec)) return "";
    sec = Math.max(0, Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = String(sec % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  }
  const isVideo = it => it && it.media === "video";
  const PLAY_ICON = `<svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>`;
  const FOLDER_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false"><path fill="currentColor"
    d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.3l2 2h8.7A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>`;
  const hasInterest = "interestForElement" in HTMLButtonElement.prototype;

  async function showInFolder(id, say) {
    try { const r = await api(`/api/open/${id}`, {}); say && say(`In folder: ${r.path}`); }
    catch (e) { say && say(e.message); }
  }

  // ---- date range filter: presets + From/To ---------------------------------------------------------------
  function iso(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
  function presetRange(v) {
    const now = new Date(), y = now.getFullYear();
    if (v === "this-year") return [`${y}-01-01`, `${y}-12-31`];
    if (v === "last-year") return [`${y - 1}-01-01`, `${y - 1}-12-31`];
    if (v === "last-12") { const s = new Date(now); s.setFullYear(y - 1); s.setDate(s.getDate() + 1); return [iso(s), iso(now)]; }
    const m = /^y(\d{4})$/.exec(v || "");
    if (m) return [`${m[1]}-01-01`, `${m[1]}-12-31`];
    return ["", ""];
  }
  /** Renders into `box`; `years` = [{value:"2026", n}], onChange() is called after any change. */
  function dateFilter(box, years, onChange, idPrefix = "df") {
    box.classList.add("datefilter");
    box.innerHTML = `
      <label>When <select id="${idPrefix}-preset">
        <option value="">Any time</option><option value="this-year">This year</option>
        <option value="last-year">Last year</option><option value="last-12">Last 12 months</option>
        <optgroup label="Year">${(years || []).map(y => `<option value="y${esc(y.value)}">${esc(y.value)}${y.n != null ? ` (${y.n})` : ""}</option>`).join("")}</optgroup>
        <option value="custom">Custom range</option></select></label>
      <label>From <input type="date" id="${idPrefix}-from"></label>
      <label>To <input type="date" id="${idPrefix}-to"></label>`;
    const pre = $(`${idPrefix}-preset`), from = $(`${idPrefix}-from`), to = $(`${idPrefix}-to`);
    const sync = () => { from.max = to.value || ""; to.min = from.value || ""; };
    pre.addEventListener("change", () => {
      if (pre.value !== "custom") { [from.value, to.value] = presetRange(pre.value); }
      sync(); onChange();
    });
    for (const inp of [from, to]) inp.addEventListener("change", () => {
      pre.value = from.value || to.value ? "custom" : ""; sync(); onChange();
    });
    return {
      get: () => ({date_from: from.value, date_to: to.value}),
      active: () => !!(from.value || to.value),
      set(f, t, preset) {
        from.value = f || ""; to.value = t || "";
        pre.value = preset && [...pre.options].some(o => o.value === preset) ? preset : (f || t ? "custom" : "");
        sync();
      },
      preset: () => pre.value,
      reset() { pre.value = ""; from.value = ""; to.value = ""; sync(); },
    };
  }

  // ---- viewer (native <dialog>) ----------------------------------------------------------------------------
  let viewer = null;
  function getViewer() {
    if (viewer) return viewer;
    const d = document.createElement("dialog");
    d.className = "viewer"; d.id = "viewer"; d.setAttribute("aria-labelledby", "viewer-title"); d.setAttribute("closedby", "any");
    d.innerHTML = `
      <div class="viewer-bar">
        <h2 id="viewer-title">Photo</h2><span class="viewer-pos" id="viewer-pos"></span>
        <a id="viewer-full" target="_blank" rel="noopener">Open full size</a>
        <a id="viewer-watch" hidden>Open in player</a>
        <button type="button" id="viewer-folder">Show in folder</button>
        <form method="dialog"><button value="close" aria-label="Close viewer" title="Close (Esc)" autofocus>&times;</button></form>
      </div>
      <div class="viewer-stage">
        <button type="button" class="viewer-nav prev" id="viewer-prev" aria-label="Previous photo" title="Previous (Left arrow)">&#8249;</button>
        <img id="viewer-img" alt="">
        <video id="viewer-video" controls playsinline preload="metadata" hidden></video>
        <p id="viewer-wait" class="viewer-wait" hidden></p>
        <button type="button" class="viewer-nav next" id="viewer-next" aria-label="Next photo" title="Next (Right arrow)">&#8250;</button>
      </div>
      <p class="viewer-status" id="viewer-status" role="status"></p>`;
    document.body.appendChild(d);
    // light-dismiss fallback for browsers without closedby
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      d.addEventListener("click", e => {
        if (e.target !== d) return;
        const r = d.getBoundingClientRect();
        if (r.top <= e.clientY && e.clientY <= r.bottom && r.left <= e.clientX && e.clientX <= r.right) return;
        d.close();
      });
    }
    const st = {d, grid: null, i: 0};
    $("viewer-prev").onclick = () => step(-1);
    $("viewer-next").onclick = () => step(1);
    $("viewer-folder").onclick = () => { const it = st.grid.items[st.i]; showInFolder(it.id, t => { $("viewer-status").textContent = t; }); };
    d.addEventListener("keydown", e => {
      if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
      else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    });
    d.addEventListener("close", () => { stopVideo(); const g = st.grid; if (g) g.focusCard(st.i); });
    let poll = 0;
    function stopVideo() { clearTimeout(poll); const v = $("viewer-video"); v.pause(); v.removeAttribute("src"); v.load(); }
    async function showVideo(it, seq) {
      const v = $("viewer-video"), wait = $("viewer-wait");
      let info;
      try { info = await api(`/api/video/${it.id}`); } catch (e) { wait.textContent = e.message; wait.hidden = false; return; }
      if (seq !== st.seq) return;
      if (info.ready) {
        wait.hidden = true; $("viewer-img").hidden = true; v.hidden = false;
        v.poster = `/preview/${it.id}.jpg${it.v ? "?v=" + it.v : ""}`;
        v.src = info.stream; v.play().catch(() => {});
      } else if (info.preparing) {
        wait.hidden = false; wait.textContent = "Making a copy your browser can play (this video is " +
          (info.vcodec || "an unusual format").toUpperCase() + "). It plays here in a moment...";
        poll = setTimeout(() => showVideo(it, seq), 2000);
      } else {
        wait.hidden = false;
        wait.textContent = info.ffmpeg ? `This video can't be played here${info.error ? " (" + info.error + ")" : ""}. Use Open full size.`
                                       : "Video tools (ffmpeg) are missing, so only the picture is shown.";
      }
    }
    async function step(dlt) {
      const g = st.grid; if (!g) return;
      let j = st.i + dlt;
      if (j >= g.items.length && g.hasMore()) await g.loadMore();
      if (j < 0 || j >= g.items.length) return;
      show(j);
    }
    function show(j) {
      const g = st.grid, it = g.items[j]; st.i = j; st.seq = (st.seq || 0) + 1;
      stopVideo();
      $("viewer-video").hidden = true; $("viewer-wait").hidden = true; $("viewer-img").hidden = false;
      $("viewer-watch").hidden = !isVideo(it);
      if (isVideo(it)) { $("viewer-watch").href = watchUrl(g, it.id); showVideo(it, st.seq); }
      $("viewer-img").src = `/preview/${it.id}.jpg${it.v ? "?v=" + it.v : ""}`;
      const nx = g.items[j + 1];      // warm the cache for the next photo
      if (nx) new Image().src = `/preview/${nx.id}.jpg${nx.v ? "?v=" + nx.v : ""}`;
      $("viewer-img").alt = `${isVideo(it) ? "Video" : "Photo"} from ${fmtDate(it.taken_at)}`;
      $("viewer-title").textContent = fmtDate(it.taken_at) + (it.place ? ` \u00b7 ${it.place}` : "");
      $("viewer-pos").textContent = `${j + 1} of ${g.total.toLocaleString()}`;
      $("viewer-full").href = isVideo(it) ? `/video/${it.id}/original` : `/full/${it.id}`;
      $("viewer-prev").disabled = j === 0;
      $("viewer-next").disabled = j >= g.items.length - 1 && !g.hasMore();
      $("viewer-status").textContent = "";
    }
    st.open = (grid, j) => { st.grid = grid; show(j); if (!d.open) d.showModal(); };
    viewer = st;
    return st;
  }

  // ---- photo grid -----------------------------------------------------------------------------------------
  /** opts: {fetchPage(offset, limit, after) -> {total, items, next}, fetchIds() -> [ids], onSelection(set),
   *         say(text), label (aria name for the grid), more (fallback "Load more" button: infinite scroll
   *         watches it and loads the next page before it scrolls into view)} */
  const PAGE = 120;
  class PhotoGrid {
    constructor(box, opts) {
      this.box = box; this.o = opts; this.items = []; this.total = 0; this.sel = new Set(); this.last = null;
      this.seq = 0; this.more = null; this.next = null;
      box.classList.add("grid"); box.setAttribute("role", "list"); if (opts.label) box.setAttribute("aria-label", opts.label);
      box.addEventListener("click", e => this.onClick(e));
      // v0.6: hovering a video card flips through its scrub sprite (a silent, instant preview; nothing plays)
      box.addEventListener("pointerover", e => { const f = e.target.closest(".card.video"); if (f && f !== this.hov) this.hover(f); });
      box.addEventListener("pointerleave", () => this.hover(null));
      box.addEventListener("pointerout", e => { if (this.hov && !this.hov.contains(e.relatedTarget)) this.hover(null); });
      // files that can't be previewed (damaged, a video named .jpg): show a plain tile instead of a broken image
      box.addEventListener("error", e => { if (e.target.tagName === "IMG") e.target.closest(".card-open")?.classList.add("noimg"); }, true);
      if (opts.more) {
        const btn = opts.more;
        btn.addEventListener("click", () => this.loadMore());
        this.io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) this.fill(); }, {rootMargin: "1200px 0px"});
        this.io.observe(btn.parentElement);
      }
    }
    hover(fig) {
      if (this.hov) { clearInterval(this.hovT); this.hov.querySelector(".card-flip")?.remove(); }
      this.hov = fig;
      if (!fig || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const it = this.items[+fig.dataset.i];
      if (!it || !it.sprite_n || !it.tile_w) return;
      const flip = document.createElement("span"); flip.className = "card-flip"; flip.setAttribute("aria-hidden", "true");
      const cols = it.sprite_cols, n = it.sprite_n, tw = it.tile_w, th = it.tile_h;
      const btn = fig.querySelector(".card-open"); btn.appendChild(flip);
      const W = btn.clientWidth, H = btn.clientHeight, sc = Math.max(W / tw, H / th);
      flip.style.backgroundImage = `url(/video/${it.id}/sprite.jpg)`;
      flip.style.backgroundSize = `${cols * tw * sc}px ${Math.ceil(n / cols) * th * sc}px`;
      let k = 0;
      const step = () => { const x = k % cols, y = Math.floor(k / cols);
        flip.style.backgroundPosition = `${-(x * tw * sc - (W - tw * sc) / 2)}px ${-(y * th * sc - (H - th * sc) / 2)}px`;
        k = (k + Math.max(1, Math.floor(n / 24))) % n; };
      step(); this.hovT = setInterval(step, 300);
    }
    hasMore() { return this.items.length < this.total; }
    syncMore() { if (this.o.more) this.o.more.hidden = !this.hasMore(); }
    /** keep loading while the end of the grid is near the viewport (tall screens, fast scrolling) */
    async fill() {
      const sentinel = this.o.more && this.o.more.parentElement;
      for (let guard = 0; guard < 20 && this.hasMore() && this.box.offsetParent !== null; guard++) {
        await this.loadMore();
        if (!sentinel || sentinel.getBoundingClientRect().top > innerHeight + 1200) break;
      }
    }
    async load() {
      const my = ++this.seq;
      const r = await this.o.fetchPage(0, PAGE, "");
      if (my !== this.seq) return null;
      this.items = []; this.total = r.total; this.next = r.next || null; this.box.replaceChildren(); this.last = null;
      this.append(r.items);
      this.syncMore();
      if (this.o.more) requestAnimationFrame(() => this.fill());
      return r;
    }
    async loadMore() {
      if (!this.hasMore()) return;
      if (!this.more) this.more = (async () => {
        const my = this.seq;
        try {
          const r = await this.o.fetchPage(this.items.length, PAGE, this.next || "");
          if (my === this.seq) {
            this.next = r.next || null;
            if (!r.items.length) this.total = this.items.length;   // the list shrank meanwhile
            this.append(r.items);
          }
        } finally { this.more = null; this.syncMore(); }
      })();
      return this.more;
    }
    append(items) {
      const frag = document.createDocumentFragment();
      for (const it of items) {
        const i = this.items.length; this.items.push(it);
        const date = fmtDate(it.taken_at), vid = isVideo(it), what = vid ? "video" : "photo";
        const fig = document.createElement("figure");
        fig.className = "card" + (vid ? " video" : "") + (this.o.videoLayout ? " vcard" : "");
        fig.dataset.i = i; fig.setAttribute("role", "listitem");
        fig.classList.toggle("selected", this.sel.has(it.id));
        const dur = vid ? fmtDur(it.duration) : "";
        const cap = this.o.videoLayout
          ? `<figcaption class="card-date"><span class="vtitle">${esc(date)}${it.place ? " \u00b7 " + esc(it.place) : ""}</span>
             <span class="vsub">${esc(it.filename || "")}${it.live ? " \u00b7 Live Photo" : ""}</span></figcaption>`
          : `<figcaption class="card-date">${esc(date)}</figcaption>`;
        fig.innerHTML = `<button type="button" class="card-open" aria-label="${vid ? "Play" : "Open"} ${what} from ${esc(date)}${it.place ? ", " + esc(it.place) : ""}${dur ? ", " + dur : ""}">
            <img ${i < 12 ? 'fetchpriority="high"' : 'loading="lazy"'} decoding="async" src="/thumb/${it.id}.jpg${it.v ? "?v=" + it.v : ""}" alt="">
            ${vid ? `<span class="card-dur">${PLAY_ICON}${esc(dur)}</span>` : ""}</button>
          <label class="card-sel"><input type="checkbox" ${this.sel.has(it.id) ? "checked" : ""}
            aria-label="Select ${what} from ${esc(date)}"></label>
          <button type="button" class="card-folder" aria-label="Show in folder" ${hasInterest ? 'interestfor="tip-folder"' : 'title="Show in folder"'}>${FOLDER_ICON}</button>
          ${cap}`;
        frag.appendChild(fig);
      }
      this.box.appendChild(frag);
      this.box.classList.toggle("selecting", this.sel.size > 0);
    }
    onClick(e) {
      const fig = e.target.closest(".card"); if (!fig) return;
      const i = +fig.dataset.i, it = this.items[i];
      const cb = e.target.closest(".card-sel input");
      if (cb) {
        const on = cb.checked;
        if (e.shiftKey && this.last != null) {
          const [a, b] = [Math.min(this.last, i), Math.max(this.last, i)];
          for (let k = a; k <= b; k++) this.setSel(k, on);
        } else this.setSel(i, on);
        this.last = i; this.changed();
        return;
      }
      if (e.target.closest(".card-folder")) { showInFolder(it.id, this.o.say); return; }
      if (e.target.closest(".card-open")) {
        if (this.sel.size && (e.ctrlKey || e.metaKey || e.shiftKey)) {   // with a selection, modifier-click selects
          this.setSel(i, !this.sel.has(it.id)); this.last = i; this.changed(); return;
        }
        if (isVideo(it) && this.o.onOpenVideo) { this.o.onOpenVideo(it, i); return; }
        getViewer().open(this, i);
      }
    }
    setSel(i, on) {
      const it = this.items[i]; if (!it) return;
      if (on) this.sel.add(it.id); else this.sel.delete(it.id);
      const fig = this.box.querySelector(`.card[data-i="${i}"]`);
      if (fig) { fig.classList.toggle("selected", on); fig.querySelector(".card-sel input").checked = on; }
    }
    changed() { this.box.classList.toggle("selecting", this.sel.size > 0); this.o.onSelection && this.o.onSelection(this.sel); }
    async selectAll() {
      const ids = await this.o.fetchIds();
      for (const id of ids) this.sel.add(id);
      this.items.forEach((it, i) => this.setSel(i, this.sel.has(it.id)));
      this.changed();
    }
    clearSelection() { this.sel.clear(); this.items.forEach((_, i) => this.setSel(i, false)); this.last = null; this.changed(); }
    focusCard(i) { const b = this.box.querySelector(`.card[data-i="${i}"] .card-open`); if (b) b.focus(); }
  }

  /** Player page link; the grid's videos (in order) become its Up next list. */
  function watchUrl(grid, id) {
    try {
      const ids = grid.items.filter(isVideo).map(x => x.id);
      sessionStorage.setItem("ps-upnext", JSON.stringify({ids: ids.slice(0, 1000), ctx: location.pathname + location.search}));
    } catch {}
    return `/watch?id=${id}`;
  }

  // ---- delete selected (v0.6) ---------------------------------------------------------------------------------
  let delDlg = null;
  function deleteDialog() {
    if (delDlg) return delDlg;
    const d = document.createElement("dialog");
    d.id = "delete-dialog"; d.setAttribute("aria-labelledby", "delete-dialog-title");
    d.innerHTML = `<form method="dialog">
        <h2 id="delete-dialog-title">Delete</h2>
        <p id="delete-dialog-text"></p>
        <p class="status">Only the copies in your sorted library are removed. Your original files are never touched,
          and you can undo this.</p>
        <p id="delete-dialog-err" class="error" role="alert"></p>
        <div class="row"><button value="cancel" autofocus>Cancel</button><button value="ok" class="danger" id="delete-dialog-ok">Delete</button></div>
      </form>`;
    document.body.appendChild(d);
    delDlg = d;
    return d;
  }
  /** Confirm, then delete the ids' sorted copies (Recycle Bin). Resolves to the API result or null. */
  async function deleteFiles(ids, say) {
    if (!ids.length) return null;
    const d = deleteDialog();
    let recycle = true;
    try { recycle = (await api("/api/cleanup/summary")).recycle_bin; } catch {}
    $("delete-dialog-title").textContent = `Delete ${plural(ids.length, "item")}?`;
    $("delete-dialog-text").textContent = `The ${ids.length === 1 ? "copy goes" : "copies go"} to the ` +
      (recycle ? "Windows Recycle Bin." : "app's trash folder (_index\\_trash).");
    $("delete-dialog-err").textContent = "";
    d.returnValue = ""; d.showModal();
    const ok = await new Promise(res => d.addEventListener("close", () => res(d.returnValue === "ok"), {once: true}));
    if (!ok) return null;
    try {
      const r = await api("/api/files/delete", {file_ids: ids, confirm: true});
      say && say(`Deleted ${plural(r.files, "item")}` + (r.removed_copies ? ` (${fmtBytes(r.bytes)} ${r.recycle_bin ? "in the Recycle Bin" : "in the trash folder"})` : "") + ". Undo is next to the selection buttons.");
      return r;
    } catch (e) { say && say(`Delete failed: ${e.message}`); return null; }
  }
  async function undoLast(say) {
    try { const r = await api("/api/cleanup/undo", {}); say && say(r.problems && r.problems.length ? `Undone, with problems: ${r.problems.join("; ")}` : "Undone: the copies are back."); return r; }
    catch (e) { say && say(`Undo failed: ${e.message}`); return null; }
  }

  // ---- selection bar + "Add to album" dialog ----------------------------------------------------------------
  let albumDlg = null;
  function albumDialog() {
    if (albumDlg) return albumDlg;
    const d = document.createElement("dialog");
    d.id = "album-dialog"; d.setAttribute("aria-labelledby", "album-dialog-title");
    d.innerHTML = `<form method="dialog" class="album-form">
        <h2 id="album-dialog-title">Add to album</h2>
        <p id="album-dialog-count" class="status"></p>
        <label>Album <select id="album-dialog-pick"></select></label>
        <label id="album-dialog-newwrap">New album name <input id="album-dialog-name" maxlength="120" autocomplete="off"></label>
        <p class="status">Albums are folders in your sorted library (Albums\\&lt;name&gt;) that point to the same photos,
          so they use almost no extra space and photos stay in their normal folders.</p>
        <p id="album-dialog-err" class="error" role="alert"></p>
        <div class="row"><button type="button" id="album-dialog-cancel">Cancel</button><button value="ok" class="primary" id="album-dialog-ok">Add</button></div>
      </form>`;
    document.body.appendChild(d);
    const pick = $("album-dialog-pick"), name = $("album-dialog-name");
    $("album-dialog-cancel").onclick = () => d.close("cancel");
    pick.onchange = () => { $("album-dialog-newwrap").hidden = pick.value !== "new"; if (pick.value === "new") name.focus(); };
    albumDlg = d;
    return d;
  }
  /** Ask for an album, add ids to it; resolves to the API result or null. */
  async function addToAlbum(ids, say) {
    const d = albumDialog();
    const {albums} = await api("/api/albums");
    const pick = $("album-dialog-pick");
    pick.innerHTML = `<option value="new">New album...</option>` +
      albums.map(a => `<option value="${a.id}">${esc(a.name)} (${a.count})</option>`).join("");
    if (albums.length) pick.value = String(albums[0].id);
    $("album-dialog-newwrap").hidden = pick.value !== "new";
    $("album-dialog-name").value = ""; $("album-dialog-err").textContent = "";
    $("album-dialog-count").textContent = `${plural(ids.length, "photo")} selected`;
    d.returnValue = "";
    d.showModal();
    if (pick.value === "new") $("album-dialog-name").focus(); else pick.focus();
    return new Promise(resolve => {
      const form = d.querySelector("form");
      const submit = async e => {
        e.preventDefault();
        const body = {file_ids: ids};
        if (pick.value === "new") {
          const n = $("album-dialog-name").value.trim();
          if (!n) { $("album-dialog-err").textContent = "Type a name for the new album."; return; }
          body.name = n;
        } else body.album = +pick.value;
        try {
          const r = await api("/api/albums/add", body);
          cleanup(); d.close("ok"); say && say(r.message); resolve(r);
        } catch (err) { $("album-dialog-err").textContent = err.message; }
      };
      const onClose = () => { cleanup(); resolve(null); };
      const cleanup = () => { form.removeEventListener("submit", submit); d.removeEventListener("close", onClose); };
      form.addEventListener("submit", submit);
      d.addEventListener("close", onClose);
    });
  }

  /** Selection toolbar wired to a grid. extra: [{label, onClick(ids), danger}]. Delete (v0.6) is built in;
   *  opts.afterDelete() runs after a delete or undo (default: reload the grid). */
  function selectionBar(box, grid, say, extra = [], opts = {}) {
    box.classList.add("selbar");
    box.innerHTML = `<span class="selbar-count" id="${box.id}-count" role="status"></span>
      <button type="button" class="sel-all">Select all results</button>
      <button type="button" class="sel-album primary" hidden>Add to album...</button>
      ${extra.map((x, k) => `<button type="button" class="sel-extra ${x.danger ? "danger" : ""}" data-k="${k}" hidden>${esc(x.label)}</button>`).join("")}
      <button type="button" class="sel-delete danger" hidden>Delete...</button>
      <button type="button" class="sel-clear" hidden>Clear selection</button>
      <button type="button" class="sel-undo" hidden>Undo delete</button>`;
    const after = opts.afterDelete || (() => grid.load());
    const undoBtn = box.querySelector(".sel-undo");
    const update = () => {
      const n = grid.sel.size;
      box.querySelector(".selbar-count").textContent = n ? `${plural(n, "photo")} selected` : "";
      for (const b of box.querySelectorAll(".sel-album, .sel-clear, .sel-extra, .sel-delete")) b.hidden = !n;
      box.querySelector(".sel-all").disabled = !grid.total;
      box.classList.toggle("active", n > 0);
    };
    box.querySelector(".sel-all").onclick = async () => { await grid.selectAll(); say && say(`Selected all ${plural(grid.sel.size, "photo")}`); };
    box.querySelector(".sel-clear").onclick = () => { grid.clearSelection(); say && say("Selection cleared"); };
    box.querySelector(".sel-album").onclick = async () => { const r = await addToAlbum([...grid.sel], say); if (r) grid.clearSelection(); };
    for (const b of box.querySelectorAll(".sel-extra")) b.onclick = () => extra[+b.dataset.k].onClick([...grid.sel]);
    box.querySelector(".sel-delete").onclick = async () => {
      const r = await deleteFiles([...grid.sel], say);
      if (!r) return;
      grid.clearSelection(); undoBtn.hidden = false; undoBtn.dataset.action = r.action_id;
      await after(); update();
    };
    undoBtn.onclick = async () => { undoBtn.hidden = true; if (await undoLast(say)) { await after(); update(); } };
    return update;
  }

  // tooltip for the show-in-folder icons (interest invokers; title attribute otherwise)
  document.addEventListener("DOMContentLoaded", () => {
    if (hasInterest && !$("tip-folder")) {
      const t = document.createElement("div");
      t.id = "tip-folder"; t.setAttribute("popover", "hint"); t.className = "tip"; t.textContent = "Show in folder";
      document.body.appendChild(t);
    }
  });

  return {$, esc, api, fmtDate, fmtBytes, fmtDur, plural, dateFilter, presetRange, PhotoGrid, selectionBar, addToAlbum,
          showInFolder, getViewer, deleteFiles, undoLast, watchUrl, isVideo, PLAY_ICON};
})();

// v0.4: family sync status in every page header
(() => {
  const chip = document.getElementById("syncchip");
  if (!chip) return;
  async function tick() {
    try {
      const r = await fetch("/api/sync/status");
      const s = await r.json();
      chip.hidden = !s.configured;
      chip.textContent = s.line;
      setTimeout(tick, s.worker_running ? 5000 : 30000);
    } catch { setTimeout(tick, 60000); }
  }
  tick();
})();

// v0.6: the Videos tab in every page header (added at run time, so the existing page templates stay as they are)
(() => {
  const nav = document.querySelector("header nav");
  if (!nav || nav.querySelector('a[href="/videos"]')) return;
  const a = document.createElement("a");
  a.href = "/videos"; a.textContent = "Videos";
  const search = nav.querySelector('a[href="/"]');
  if (search) search.after(a); else nav.prepend(a);
})();
