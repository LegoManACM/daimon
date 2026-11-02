"use strict";
// The "Finish setup" view: what's left after the fast-track setup, one step at a time.
(() => {
  const css = document.createElement("style");
  css.textContent = `
#v-setup .n { width: 24px; height: 24px; border-radius: 50%; flex: none; display: grid; place-items: center; font-size: 12px; font-weight: 600; font-family: var(--mono); background: var(--panel-2); border: 1px solid var(--line-2); color: var(--muted); }
#v-setup .n.ok { background: var(--ok-dim); border-color: transparent; color: var(--ok); }
#v-setup .n.now { background: var(--ember); border-color: var(--ember); color: #1a1208; }
#v-setup .bar { width: 140px; flex: none; }
#v-setup .wrap { max-width: 760px; }
#v-setup .next { display: flex; gap: 14px; align-items: center; margin: 10px 0 4px; flex-wrap: wrap; }
#v-setup .next .main { flex: 1 1 220px; min-width: 0; }
#v-setup .next .acts, #v-setup .row-item .acts { display: flex; gap: 8px; flex-wrap: wrap; }
#v-setup .row-item { flex-wrap: wrap; }
#v-setup .row-item .main { flex: 1 1 200px; }
#v-setup p { margin: 0; }
#v-setup .row-item.done { flex-wrap: nowrap; align-items: center; }
#v-setup .row-item.done .main { flex: 1; }
#v-setup .row-item.done .acts { width: auto; }
#v-setup .note { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
#v-setup .foot { display: flex; gap: 8px; flex-wrap: wrap; margin: 28px 0 24px; }
#v-setup .allset { text-align: center; padding: 28px 0 8px; }
#v-setup .allset h3 { margin: 0 0 6px; font-size: 16px; }
#v-setup .allset .acts { justify-content: center; margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }
#v-setup a.btn { display: inline-flex; align-items: center; text-decoration: none; color: inherit; }
#v-setup a.btn.primary { color: #1a1208; }
@media (max-width: 820px) {
  #v-setup .bar { width: 100%; order: 4; }
  #v-setup .btn, #v-setup .btn.sm { min-height: 44px; }
  #v-setup .next .acts, #v-setup .row-item .acts { width: 100%; }
  #v-setup .next .acts .btn, #v-setup .row-item .acts .btn { flex: 1; justify-content: center; }
  #v-setup .row-item.done .acts .btn { flex: none; }
  #v-setup .foot .btn { flex: 1 1 100%; }
}`;
  document.head.append(css);

  let state = null;
  const addon = key => () => (window.connectAddon ? window.connectAddon(key) : go("connections"));

  async function showLink() {
    if (typeof window.openLink === "function") return window.openLink();
    let r; try { r = await api("/api/link"); } catch (e) { return fail(e); }
    openDrawer("Your other devices", "Open this on your phone or laptop to sign in.",
      `${r.qr ? `<div class="qr">${r.qr}</div>` : ""}<div class="urlbox" style="margin-top:12px"><span>${esc(r.link)}</span><button class="btn" data-copy>Copy</button></div>`);
    const b = $("#dBody [data-copy]"); if (b) b.onclick = () => copy(r.link);
    refresh();
  }

  const ACT = {
    model: { label: "Change", run: () => go("settings") },
    web: { label: "Change", run: () => go("settings") },
    telegram: { label: "Connect Telegram", short: "Connect", run: () => (window.openTelegram ? window.openTelegram() : go("connections")) },
    calendar: { label: "Connect calendar", short: "Connect", run: addon("calendar") },
    messages: { label: "Connect texts", short: "Connect", run: addon("messages") },
    canvas: { label: "Connect Canvas", short: "Connect", run: addon("canvas") },
    devices: { label: "Show link", short: "Show link", run: showLink },
  };

  function actions(s, big) {
    if (s.id === "claude") {
      return s.done ? "" : `<a class="btn ${big ? "primary" : "sm"}" href="https://claude.com/claude-code" target="_blank" rel="noopener">Get Claude Code</a>`;
    }
    const a = ACT[s.id];
    return a ? `<button class="btn ${big ? "primary" : "sm"}" data-act="${s.id}">${esc(big ? a.label : a.short || a.label)}</button>` : "";
  }
  const note = s => s.id === "claude" && !s.done
    ? `<p class="note">Then run <span class="mono">claude</span> once to log in; it's ready on the next restart.</p>` : "";

  function render() {
    const root = $("#v-setup"); if (!root) return;
    const r = state;
    if (!r) { root.innerHTML = `<div class="view-head"><h2>Finish setup</h2></div><div class="scroll"><div class="list"><p class="empty">Loading…</p></div></div>`; return; }
    const num = id => r.steps.findIndex(s => s.id === id) + 1;
    const done = r.steps.filter(s => s.done);
    const skipped = r.steps.filter(s => s.skipped);
    const todo = r.steps.filter(s => !s.done && !s.skipped);
    const [next, ...rest] = todo;
    const pct = r.total ? Math.round(100 * r.done / r.total) : 0;
    const row = (s, n, extra) => `<div class="row-item${s.done ? " done" : ""}"><span class="n ${s.done ? "ok" : ""}" aria-hidden="true">${s.done ? "✓" : n}</span><div class="main"><p class="title">${esc(s.title)}</p><p class="sub">${esc(s.sub || "")}</p>${note(s)}</div><div class="acts">${extra}</div></div>`;
    let h = `<div class="view-head"><h2>Finish setup</h2>
      <span class="muted" style="font-size:12.5px">your agent is running · the rest is optional and stays under Connections and Settings</span>
      <div class="spacer"></div><span class="mono faint">${r.done} of ${r.total}</span>
      <div class="bar" role="progressbar" aria-label="Setup progress" aria-valuenow="${r.done}" aria-valuemin="0" aria-valuemax="${r.total}"><i style="width:${pct}%"></i></div></div>
      <div class="scroll"><div class="list wrap">`;
    if (done.length) {
      h += `<p class="gh">Done</p>` + done.map(s => row(s, 0, ["model", "web"].includes(s.id) ? actions(s, false) : "")).join("");
    }
    if (next) {
      h += `<p class="gh">Next</p><div class="approval next"><span class="n now">${num(next.id)}</span>
        <div class="main"><p style="font-weight:500">${esc(next.title)}</p><p class="muted" style="font-size:12.5px">${esc(next.sub || "")}</p>${note(next)}</div>
        <div class="acts"><button class="btn" data-skip="${next.id}">Skip</button>${actions(next, true)}</div></div>`;
      h += rest.map(s => row(s, num(s.id), actions(s, false) + `<button class="btn sm ghost" data-skip="${s.id}">Skip</button>`)).join("");
    } else {
      h += `<div class="allset"><h3>All set</h3><p class="muted">Everything else lives under Connections and Settings, whenever you want it.</p>
        <div class="acts"><button class="btn primary" data-go="connections">Connections</button><button class="btn" data-go="settings">Settings</button></div></div>`;
    }
    if (skipped.length) {
      h += `<p class="gh">Skipped</p>` + skipped.map(s => row(s, num(s.id), `<button class="btn sm" data-unskip="${s.id}">Undo skip</button>`)).join("");
    }
    h += `<p class="gh">Worth a look</p>
      <div class="row-item"><span class="n" aria-hidden="true">·</span><div class="main"><p class="title">Where your notes live</p><p class="sub">In the home folder now. Point it at an Obsidian vault instead?</p></div><div class="acts"><button class="btn sm" data-go="settings">Knowledge settings</button></div></div>
      <div class="row-item"><span class="n" aria-hidden="true">·</span><div class="main"><p class="title">Updates</p><p class="sub">Tells you about new releases. It can install them when idle.</p></div><div class="acts"><button class="btn sm" data-go="settings">Updates</button></div></div>
      <div class="foot"><button class="btn primary" data-go="chat">Say hi to your agent</button><button class="btn ghost" data-later="1">I'll do the rest later</button></div>
      </div></div>`;
    root.innerHTML = h;
  }

  function nav() {
    const link = $("#navSetupLink"), c = $("#navSetup");
    if (!link || !state) return;
    link.style.display = state.finished ? "none" : "";
    if (c) c.textContent = `${state.done}/${state.total}`;
  }

  async function refresh() {
    try { state = await api("/api/setup"); } catch { return state; }
    nav();
    if (S.view === "setup") render();
    return state;
  }
  async function post(body) {
    try { state = await api("/api/setup", body); } catch (e) { fail(e); return false; }
    nav(); render();
    return true;
  }

  $("#v-setup").addEventListener("click", async e => {
    const t = e.target.closest("button"); if (!t) return;
    if (t.dataset.act) ACT[t.dataset.act]?.run();
    else if (t.dataset.skip) post({ skip: t.dataset.skip });
    else if (t.dataset.unskip) post({ unskip: t.dataset.unskip });
    else if (t.dataset.go) go(t.dataset.go);
    else if (t.dataset.later) { if (await post({ dismissed: true })) go("chat"); }
  });

  LOAD.setup = () => { render(); refresh(); };
  CHANGED.push(what => { if (["setup", "connections", "settings", "models"].includes(what)) refresh(); });

  // first run: land on the checklist once
  (async () => {
    await refresh();
    if (!state || state.finished || location.hash.length > 1) return;
    let seen = false;
    try { seen = !!localStorage.getItem("daimon-setup-seen"); } catch { }
    if (seen) return;
    try { localStorage.setItem("daimon-setup-seen", "1"); } catch { }
    go("setup");
  })();
})();
