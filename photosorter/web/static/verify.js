// Verify before deleting originals (v0.4). Read-only over the originals.
(() => {
  const {$, esc, api, fmtBytes} = PS;
  let offset = 0, timer = null, last = null;
  const ORDER = ["safe_copy", "safe_deduped", "missing", "near_only"];
  // v0.6: files deleted on purpose with Delete on a selection
  ORDER.splice(2, 0, "safe_deleted");
  if ($("bucket") && ![...$("bucket").options].some(o => o.value === "safe_deleted")) {
    const o = document.createElement("option"); o.value = "safe_deleted"; o.textContent = "Deleted on purpose";
    $("bucket").insertBefore(o, $("bucket").querySelector('option[value="safe_copy"]'));
  }

  function render(s) {
    last = s;
    const sum = s.summary, run = s.running, p = s.progress;
    const banner = $("banner");
    banner.textContent = run ? "Checking..." : sum.banner;
    banner.className = "banner " + (run ? "" : sum.all_safe ? "ok" : sum.finished_at ? "warn" : "");
    $("buckets").innerHTML = ORDER.map(k => {
      const b = sum.buckets[k];
      return `<div class="${b.safe ? "safe" : "unsafe"}"><b>${b.files.toLocaleString()}</b><span>${esc(b.label)}</span><span>${fmtBytes(b.bytes)}</span></div>`;
    }).join("");
    $("start").hidden = run; $("stop").hidden = !run;
    $("start").textContent = sum.finished_at ? "Check again" : "Check my originals";
    $("rescue").disabled = run || !sum.attention;
    $("csv").hidden = !sum.finished_at;
    if (run && p) {
      const what = p.phase === "library" ? "Reading your library" : p.phase === "originals" ? "Reading your originals" : "Starting";
      $("progress").textContent = `${what}: ${p.done.toLocaleString()} of ${p.total.toLocaleString()} files (${fmtBytes(p.bytes)} read). Runs at low priority; you can keep using the PC.`;
    } else if (p && p.phase === "error") {
      $("progress").textContent = "The check stopped with an error: " + p.error;
    } else if (p && p.phase === "stopped") {
      $("progress").textContent = "Stopped. Check again picks up where it left off (finished files aren't read twice).";
    } else {
      $("progress").textContent = sum.finished_at ? `Last checked ${new Date(sum.finished_at).toLocaleString()}.` : "";
    }
    const src = run ? s.sources : (sum.sources.length ? sum.sources : s.sources);
    $("sources").textContent = src.length ? "Original folders: " + src.join(", ") : "No original folders known yet. Sort some photos first.";
    $("advice").textContent = sum.advice;
    if (s.rescue) {
      $("open-rescued").hidden = false;
    }
  }

  async function poll() {
    clearTimeout(timer);
    const s = await api("/api/verify/status");
    const wasRunning = last && last.running;
    render(s);
    if (wasRunning && !s.running) loadRows(true);
    timer = setTimeout(poll, s.running ? 1500 : 15000);
  }

  async function loadRows(reset) {
    if (reset) { offset = 0; $("rows").innerHTML = ""; }
    const r = await api(`/api/verify/results?bucket=${$("bucket").value}&offset=${offset}&limit=200`);
    offset += r.results.length;
    $("rows").insertAdjacentHTML("beforeend", r.results.map(x =>
      `<tr><td class="path">${esc(x.path)}</td><td>${x.size == null ? "" : fmtBytes(x.size)}</td><td>${esc(x.reason || "")}${x.match_path ? `<br><span class="status">Library: ${esc(x.match_path)}</span>` : ""}</td></tr>`).join(""));
    $("more").hidden = r.results.length < 200;
    if (!offset) $("rows").innerHTML = `<tr><td colspan="3" class="status">Nothing here.</td></tr>`;
  }

  $("start").onclick = async () => { try { await api("/api/verify/start", {}); } catch (e) { alert(e.message); } poll(); };
  $("stop").onclick = async () => { await api("/api/verify/stop", {}); poll(); };
  $("bucket").onchange = () => loadRows(true);
  $("more").onclick = () => loadRows(false);
  $("open-rescued").onclick = () => api("/api/verify/open-rescued", {});
  $("rescue").onclick = () => {
    const b = last.summary.buckets;
    const n = b.missing.files + b.near_only.files;
    $("rescue-text").textContent = `Copy ${n.toLocaleString()} files (${fmtBytes(b.missing.bytes + b.near_only.bytes)}) into the Rescued folder?`;
    $("rescue-dlg").showModal();
  };
  $("rescue-dlg").addEventListener("close", async () => {
    if ($("rescue-dlg").returnValue !== "ok") return;
    $("progress").textContent = "Copying...";
    try {
      const r = await api("/api/verify/rescue", {confirm: true});
      $("progress").textContent = `Copied ${r.copied.toLocaleString()} files (${fmtBytes(r.bytes)}) to ${r.folder}` +
        (r.skipped ? `; ${r.skipped} were already there` : "") + (r.errors.length ? `; ${r.errors.length} could not be copied: ${r.errors[0]}` : "") +
        ". Run the check again to see them counted as safe.";
      $("open-rescued").hidden = false;
    } catch (e) { $("progress").textContent = e.message; }
  });
  poll().then(() => loadRows(true));
})();
