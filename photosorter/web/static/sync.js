// Family sync settings + status (v0.4).
(() => {
  const {$, api, fmtBytes} = PS;
  const BOOLS = ["include_full", "include_screenshots", "window_enabled", "auto_after_run", "enabled", "verify_tls"];
  let timer = null, filled = false, cur = null;

  function render(s) {
    cur = s;
    $("line").textContent = s.line;
    $("line").className = "banner " + (s.state.phase === "error" ? "warn" : s.state.last_ok_at ? "ok" : "");
    $("pause").textContent = s.paused ? "Resume" : "Pause";
    $("now").disabled = !s.configured;
    const st = s.state, bits = [];
    if (st.photos != null) bits.push(`${st.photos.toLocaleString()} photos, ${st.people} people, ${st.albums} albums in the catalog`);
    if (st.phase === "sending" && st.total_items) bits.push(`${st.remaining_items.toLocaleString()} of ${st.total_items.toLocaleString()} files left (about ${fmtBytes(st.remaining_bytes || 0)})`);
    if (st.bytes_sent) bits.push(`${fmtBytes(st.bytes_sent)} sent this run`);
    if (st.phase === "done" && st.sent_files != null) bits.push(`last run sent ${st.sent_files.toLocaleString()} files${st.removed_on_pi ? `, removed ${st.removed_on_pi} from the Pi` : ""}${st.failed_files ? `, ${st.failed_files} couldn't be prepared` : ""}`);
    bits.push(s.worker_running ? "background sync is running" : "background sync is not running (Sync now starts it)");
    if (s.autostart) bits.push("starts when you sign in");
    $("detail").textContent = bits.join(" · ");
    $("paired").textContent = s.paired ? "Paired. Paste a new code only if you made a new one on the calendar." : "Not paired yet.";
    if (!filled) {
      filled = true;
      const c = s.config;
      $("pi_url").value = c.pi_url;
      for (const k of BOOLS) $(k).checked = !!c[k];
      $("max_mbps").value = c.max_mbps;
      $("window_start").value = c.window_start; $("window_end").value = c.window_end;
      $("interval_minutes").value = c.interval_minutes;
    }
  }

  async function poll() {
    clearTimeout(timer);
    try { render(await api("/api/sync/status")); } catch (e) { $("line").textContent = e.message; }
    timer = setTimeout(poll, cur && cur.worker_running ? 2000 : 10000);
  }

  $("cfg").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {pi_url: $("pi_url").value.trim(), max_mbps: Number($("max_mbps").value),
      window_start: $("window_start").value, window_end: $("window_end").value,
      interval_minutes: Number($("interval_minutes").value)};
    for (const k of BOOLS) body[k] = $(k).checked;
    if ($("token").value.trim()) body.token = $("token").value.trim();
    try {
      render(await api("/api/sync/config", body));
      $("token").value = "";
      $("saved").textContent = "Saved.";
    } catch (err) { $("saved").textContent = err.message; }
  });
  $("now").onclick = async () => { try { render(await api("/api/sync/now", {})); } catch (e) { alert(e.message); } poll(); };
  $("pause").onclick = async () => { render(await api("/api/sync/pause", {paused: !(cur && cur.paused)})); };
  $("test").onclick = async () => {
    $("detail").textContent = "Testing...";
    const r = await api("/api/sync/test", {});
    $("detail").textContent = r.message;
  };
  poll();
})();
