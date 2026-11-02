"use strict";
// The connections view: where Daimon reaches you, the accounts it can see, and every tool it can use.
// Also defines the flows the setup checklist opens directly: openTelegram(), connectAddon(key), openLink().
(() => {
  const V = () => $("#v-connections");
  let C = null, bot = null, tgGen = 0;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const enc = encodeURIComponent;
  const alive = root => root.isConnected && $("#drawer").classList.contains("open");
  const LV = [["auto", "Just run"], ["notify", "Tell me"], ["ask", "Ask first"]];

  const css = document.createElement("style");
  css.textContent = `
    .cx { font: 14px/1.5 var(--sans); }
    .cx .filters, #v-connections .filters { order: 0; flex: none; flex-basis: auto; margin: 0; padding: 0; flex-wrap: wrap; overflow: visible; }
    .cx .filter, #v-connections .filter { min-height: 32px; }
    .cx .step { display: flex; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--line); }
    .cx .step .n { width: 22px; height: 22px; border-radius: 50%; border: 1px solid var(--line-2); display: grid; place-items: center; font-size: 12px; flex: none; color: var(--muted); }
    .cx .step.done .n { background: var(--ok-dim); color: var(--ok); border-color: transparent; }
    .cx .step.off { opacity: .55; }
    .cx .step > div { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
    .cx .step b { font-weight: 500; }
    .cx p { margin: 0; } .cx .m { color: var(--muted); font-size: 12.5px; } .cx .ok { color: var(--ok); } .cx .er { color: var(--err); }
    .cx .help { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; font-size: 12.5px; color: var(--muted); }
    .cx .help ol { margin: 6px 0 0; padding-left: 20px; } .cx .help li { margin: 3px 0; }
    .cx .help a, .cx a.l { color: var(--info); }
    .cx .bigbox { background: var(--panel-2); border: 1px solid var(--line-2); border-radius: 12px; padding: 14px; text-align: center; margin-bottom: 12px; }
    .cx .bigbox .v { font-size: 56px; line-height: 1.2; } .cx .bigbox .v.code { font: 600 28px/1.3 var(--mono); letter-spacing: .12em; }
    .cx .tl { display: flex; flex-direction: column; gap: 6px; padding: 10px 0; border-bottom: 1px solid var(--line); }
    .cx .tl .h { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-family: var(--mono); font-size: 12.5px; }
    .cx .tl .m { font-family: var(--sans); }
    .cx .line { display: flex; gap: 10px; align-items: center; padding: 8px 0; }
    .cx .btns .btn { flex: none; }
    .cx .btns { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
    .cx .danger { color: var(--err); }
    .cx .joblog.er { color: var(--err); }
    .cx .tg { display: inline-flex; gap: 6px; align-items: center; }
    #v-connections .sec-note { font-size: 12.5px; color: var(--muted); padding: 6px 0 2px; }
    #v-connections .row-item { cursor: default; } #v-connections .row-item[data-a] { cursor: pointer; }
    #v-connections .defs { display: flex; flex-direction: column; gap: 10px; padding: 12px 0; }
    #v-connections .defs > div { display: flex; gap: 10px 14px; align-items: center; flex-wrap: wrap; }
    #v-connections .defs .t { width: 150px; font-size: 13px; }
    #v-connections .mono { font-family: var(--mono); }
    @media (max-width: 820px) { #v-connections .row-item { flex-wrap: wrap; } #v-connections .row-item .main { flex: 1 1 200px; } #v-connections .defs .t { width: 100%; } }`;
  document.head.append(css);

  // ---- little builders ------------------------------------------------------------------------------
  const seg = (items, cur, a, extra = "") => `<div class="filters">${items.map(([v, l]) =>
    `<button type="button" class="filter${v === cur ? " on" : ""}" data-a="${a}" data-v="${esc(v)}" ${extra} aria-pressed="${v === cur}">${esc(l)}</button>`).join("")}</div>`;
  const inp = (f, name, label, o = {}) => `<label>${label}<input class="field" name="${name}" value="${esc(f[name] ?? "")}" type="${o.type || "text"}"
    placeholder="${esc(o.ph || "")}" autocomplete="${o.type === "password" ? "new-password" : "off"}" autocapitalize="off" spellcheck="false"></label>`;
  const sel = (f, name, label, opts, o = {}) => `<label>${label}<select class="field" name="${name}"${o.redraw ? ` data-redraw="1"` : ""}>${opts.map(([v, l]) =>
    `<option value="${esc(v)}"${v === f[name] ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
  const linkify = s => esc(s).replace(/https?:\/\/[^\s<]+/g, u => {
    const t = u.match(/(?:&#39;|&quot;|[.,;)])+$/)?.[0] || "", h = u.slice(0, u.length - t.length);
    return `<a href="${h}" target="_blank" rel="noopener">${h}</a>${t}`;
  });
  const ext = (href, text) => `<a class="l" href="${esc(href)}" target="_blank" rel="noopener">${text}</a>`;
  const step = (n, done, off, body) => `<div class="step${done ? " done" : ""}${off ? " off" : ""}"><span class="n">${done ? "✓" : n}</span><div>${body}</div></div>`;

  // A drawer with a root element that owns its clicks (root._a = {name: fn(button)}) and its inputs (root._f).
  function drawer(title, meta) {
    openDrawer(title, meta || "", `<div class="cx"></div>`);
    const root = $("#dBody .cx");
    root.onclick = e => { const b = e.target.closest("[data-a]"); if (b && root._a?.[b.dataset.a]) { e.preventDefault(); root._a[b.dataset.a](b, e); } };
    root.oninput = e => { const n = e.target.name; if (n && root._f) root._f[n] = e.target.value; };
    return root;
  }
  const busy = async (b, fn) => { b.disabled = true; try { return await fn(); } catch (e) { fail(e); } finally { b.disabled = false; } };

  // ---- data -------------------------------------------------------------------------------------------
  async function load() {
    C = await api("/api/connections");
    const bad = C.servers.filter(s => s.error).length, n = $("#navConnections");
    if (n) { n.textContent = bad || ""; n.style.color = bad ? "var(--err)" : ""; }
    render();
    return C;
  }
  const ensure = async () => C || load();
  const getBot = async () => { try { bot = (await api("/api/telegram", { action: "bot" })).bot || null; } catch { bot = null; } return bot; };

  // ---- the page ---------------------------------------------------------------------------------------
  function restartNote(pending) {
    return pending?.length ? `<div class="approval" style="margin-top:14px"><div class="h">Restart to finish</div>
      <p style="margin-top:6px;font-size:13px">${esc(pending.join(", "))} will change after a restart.</p>
      <div class="restart-bar"><button class="btn primary" data-a="restart">Restart now</button></div></div>` : "";
  }
  async function restart(b) {
    await busy(b, restartAgent);
  }

  function render() {
    const el = V(), c = C;
    if (!el || !c) return;
    const keep = $(".scroll", el)?.scrollTop || 0, t = c.telegram, ready = t.token && t.chat;
    const net = (() => { try { return netLabel(); } catch { return "this computer"; } })();
    const tg = ready ? (t.running ? ["", "connected"] : ["warn", "restart to start it"]) : t.token ? ["warn", "waiting for your first message"] : ["idle", "not set up"];
    const row = (dot, title, sub, acts, a = "") => `<div class="row-item" ${a}><span class="dot ${dot}"></span><div class="main"><div class="title">${title}</div><div class="sub">${sub}</div></div><div class="acts">${acts}</div></div>`;
    const servers = c.servers.map(s => row(s.disabled ? "idle" : s.error ? "err" : "",
      `<span class="mono">${esc(s.name)}</span>${s.addon ? ` <span class="tag">add-on</span>` : ""}`,
      s.disabled ? `off · ${esc(oneLine(s.target, 60))}` : s.error ? `<span style="color:var(--err)">${esc(oneLine(s.error, 90))}</span>`
        : `${esc(oneLine(s.target, 60))} · ${s.tools} tools${s.asks ? ` · ${s.asks} ask first` : ""}`,
      `<span class="faint">›</span>`, `data-a="server" data-n="${esc(s.name)}"`)).join("");
    el.innerHTML = `<div class="view-head"><h2>Connections</h2><span class="muted">where it reaches you, the accounts it can see, and every tool it can use</span>
        <span class="spacer"></span><button class="btn" data-a="addServer">+ Add a server</button></div>
      <div class="scroll"><div class="list">${restartNote(c.pending)}
      <div class="gh">Where it reaches you</div>
      ${row(tg[0], "Telegram", `${tg[1]}${t.token ? ` · <span id="cxBot">${bot ? "@" + esc(bot) : "your bot"}</span> · sends notices and errors` : " · get notices and talk to it from your phone"}`,
        `${ready ? `<button class="btn sm" data-a="tgTest">Send a test</button>` : ""}<button class="btn sm${t.token ? "" : " primary"}" data-a="tg">${t.token ? "Manage" : "Set up"}</button>`)}
      ${row("", "Web page", esc(net) + " · this page, on your computer and your phone", `<button class="btn sm" data-a="link">Link a device</button>`)}
      <div class="gh">Add-ons</div>
      ${c.addons.map(a => row(a.error ? "err" : a.added ? "" : "idle", esc(a.title),
        a.error ? `<span style="color:var(--err)">${esc(oneLine(a.error, 90))}</span>` : a.added ? `connected · ${a.tools} tools · ${esc(a.blurb)}` : `not connected · ${esc(a.blurb)}`,
        a.added || a.error ? `<button class="btn sm" data-a="manage" data-k="${esc(a.key)}">Manage</button>` : `<button class="btn sm primary" data-a="connect" data-k="${esc(a.key)}">Connect</button>`)).join("")}
      <div class="gh">Other tools · MCP servers</div>
      ${servers || `<div class="empty">No servers yet.</div>`}
      <div class="defs"><div class="t muted" style="font-size:12px">Defaults for new tools</div>
        <div><span class="t">Read-only tools</span>${seg(LV, c.defaults.read_only, "def", `data-k="read_only"`)}</div>
        <div><span class="t">Everything else</span>${seg(LV, c.defaults.other, "def", `data-k="other"`)}</div></div>
      <div class="gh">Keys &amp; secrets</div>
      <div class="sec-note">Values are never shown here. You can replace one, but not read it back.</div>
      ${c.secrets.map(s => { const miss = !s.set && s.used_by.length;
        return row(miss ? "warn" : s.set ? "" : "idle", `<span class="mono">${esc(s.name)}</span>`,
          `${s.set ? (s.last4 ? "ends in " + esc(s.last4) : "set") : miss ? `<span style="color:var(--warn)">missing</span>` : "not set"}${s.used_by.length ? " · used by " + esc(s.used_by.join(", ")) : " · not used"}`,
          `<button class="btn sm" data-a="secret" data-n="${esc(s.name)}">${s.set ? "Replace" : "Set"}</button>${s.set && !s.used_by.length ? `<button class="btn sm ghost" data-a="secretRm" data-n="${esc(s.name)}">Remove</button>` : ""}`); }).join("")}
      <div style="padding:14px 0"><button class="btn" data-a="secretNew">+ Add a secret</button></div>
      </div></div>`;
    $(".scroll", el).scrollTop = keep;
    if (t.token && bot === null) getBot().then(() => { const s = $("#cxBot"); if (s && bot) s.textContent = "@" + bot; });
  }

  const page = {
    restart,
    addServer: () => addServerDrawer(),
    tg: () => openTelegram(),
    tgTest: b => busy(b, async () => { await api("/api/telegram", { action: "test" }); toast("Sent. Check Telegram."); }),
    link: () => openLink(),
    manage: b => manage(b.dataset.k),
    connect: b => connectAddon(b.dataset.k),
    server: b => openServer(b.dataset.n),
    def: b => busy(b, async () => {
      const d = { ...C.defaults, [b.dataset.k]: b.dataset.v };
      await api("/api/mcp-defaults", d); C.defaults = d; render();
    }),
    secret: b => secretDrawer(b.dataset.n),
    secretNew: () => secretDrawer(""),
    secretRm: b => confirm(`Remove ${b.dataset.n}? Nothing uses it.`) && busy(b, async () => {
      await api("/api/secrets", { name: b.dataset.n, remove: true }); toast("Removed"); await load();
    }),
  };
  document.addEventListener("click", e => {
    const b = e.target.closest("#v-connections [data-a]");
    if (b && page[b.dataset.a]) page[b.dataset.a](b);
  });

  // ---- Telegram ---------------------------------------------------------------------------------------
  const EVENTS = [["notify", "Notices"], ["error", "Errors"], ["task", "Tasks finishing"], ["routine", "Routines"], ["edit", "Changes to itself"]];
  async function openTelegram() {
    const root = drawer("Telegram", `<span class="faint" style="font-size:12px">notices and chat on your phone</span>`);
    root.innerHTML = `<p class="m">Loading…</p>`;
    try { await ensure(); } catch (e) { return fail(e); }
    const t = C.telegram, s = root._f = { token: "", bot: t.token ? await getBot() : null, hasToken: t.token, chat: t.chat, events: [...t.events] };
    const draw = () => {
      const c = s.hasToken, nm = s.bot ? "@" + s.bot : "your bot";
      root.innerHTML = `
        ${step(1, c, false, `<b>Make a bot</b><p class="m">Open ${ext("https://t.me/BotFather", "@BotFather")} in Telegram, send <span class="mono">/newbot</span> and answer its two questions. It gives you a token that looks like <span class="mono">123456789:AA…</span></p>
          <div><a class="btn sm" href="https://t.me/BotFather" target="_blank" rel="noopener">Open @BotFather</a></div>`)}
        ${step(2, c && !s.edit, false, c && !s.edit ? `<b>Connected to <span class="ok">${esc(nm)}</span></b><div><button class="btn sm ghost" data-a="edit">Use a different bot</button></div>`
          : `<b>Paste its token</b>${inp(s, "token", "Bot token", { type: "password", ph: "123456789:AA…" })}<div><button class="btn primary sm" data-a="token">Connect</button></div>`)}
        ${step(3, s.chat, !c, s.chat ? `<b>Linked to your chat</b>` : `<b>Say hi to it</b><p class="m">${c && s.bot ? ext("https://t.me/" + s.bot, "Open " + esc(nm)) + " and send it any message." : "Send your bot any message."}</p>
          ${c ? s.waiting ? `<div class="line"><span class="spin"></span><span class="m">Waiting for your message…</span></div>` : `<div><button class="btn sm" data-a="wait">${s.err ? "Check again" : "Wait for it"}</button>${s.err ? ` <span class="er m">${esc(s.err)}</span>` : ""}</div>` : ""}`)}
        ${step(4, false, !s.chat, `<b>Which notices it sends</b>
          <div class="filters">${EVENTS.map(([k, l]) => `<button type="button" class="filter${s.events.includes(k) ? " on" : ""}" data-a="ev" data-k="${k}" aria-pressed="${s.events.includes(k)}">${l}</button>`).join("")}</div>`)}
        <p class="m" style="margin-top:14px">Telegram changes take effect after a restart.</p>
        ${restartNote(C.pending)}
        ${c ? `<div class="btns"><button class="btn ghost danger" data-a="remove">Disconnect Telegram</button></div>` : ""}`;
    };
    const wait = async () => {
      const me = ++tgGen;
      s.waiting = true; s.err = ""; draw();
      try {
        while (me === tgGen && alive(root) && !s.chat) {
          const r = await api("/api/telegram", { action: "wait" });
          if (r.found) { s.chat = true; C.pending = r.pending; toast("Linked. Daimon sent you a hello."); }
        }
      } catch (e) { s.err = e.message; }
      s.waiting = false; if (alive(root) && me === tgGen) draw();
      load().catch(() => { });
    };
    root._a = {
      edit: () => { s.edit = true; draw(); },
      token: b => busy(b, async () => {
        const r = await api("/api/telegram", { action: "token", token: s.token });
        s.token = ""; s.hasToken = true; s.edit = false; s.bot = bot = r.bot; C.pending = r.pending; C.telegram.token = true;
        draw(); wait(); load().catch(() => { });
      }),
      wait,
      ev: b => {
        const k = b.dataset.k; s.events = s.events.includes(k) ? s.events.filter(x => x !== k) : [...s.events, k]; draw();
        api("/api/telegram", { action: "events", events: s.events }).then(r => { C.pending = r.pending; draw(); }, fail);
      },
      restart: b => restart(b),
      remove: b => confirm("Disconnect Telegram? Daimon stops messaging you there.") && busy(b, async () => {
        tgGen++; await api("/api/telegram", { action: "remove" }); bot = null; closeDrawer(); toast("Telegram disconnected"); await load();
      }),
    };
    draw();
    if (s.hasToken && !s.chat) wait();
  }

  async function openLink() {
    const root = drawer("Link a device", `<span class="faint" style="font-size:12px">open Daimon on your phone</span>`);
    root.innerHTML = `<p class="m">Making the link…</p>`;
    try {
      const l = await api("/api/link");
      root.innerHTML = `<p>Scan this with your phone's camera, or send yourself the link.</p>
        ${l.qr ? `<div class="qr" style="margin:14px 0">${l.qr}</div>` : ""}
        <div class="urlbox"><span>${esc(l.link)}</span><button class="btn sm" data-a="copy">Copy</button></div>
        <p class="m" style="margin-top:10px">Keep this link private: anyone who has it can open Daimon.${/localhost|127\.0\.0\.1/.test(l.url) ? " It only works on this computer until you change where the page listens (Settings)." : ""}</p>`;
      root._a = { copy: () => copy(l.link) };
    } catch (e) { root.innerHTML = `<p class="er">${esc(e.message)}</p>`; }
  }

  // ---- add-ons ----------------------------------------------------------------------------------------
  const KINDS = [["google", "Google"], ["microsoft", "Microsoft"], ["apple", "Apple"], ["fastmail", "Fastmail"], ["yahoo", "Yahoo"], ["caldav", "Other (CalDAV)"], ["ics", "A calendar link"]];
  const PASS_HELP = { apple: "Use an app-specific password from appleid.apple.com, not your Apple ID password.",
    fastmail: "Use an app password (Fastmail settings → Privacy & Security → App passwords).", yahoo: "Use an app password (Yahoo account security → Generate app password)." };
  const calFields = f => ({
    google: `<div class="help">Google needs an OAuth client of your own (free, once):<ol>
        <li>At ${ext("https://console.cloud.google.com", "console.cloud.google.com")}, make a project, then APIs &amp; Services → Library → turn on <b>Google Calendar API</b>.</li>
        <li>OAuth consent screen: External, and add yourself as a test user (or publish it, so sign-ins don't expire weekly).</li>
        <li>Credentials → Create credentials → OAuth client ID → <b>Desktop app</b>. Copy its client ID and secret here.</li></ol></div>
      ${inp(f, "client_id", "Client ID")}${inp(f, "client_secret", "Client secret", { type: "password" })}
      <p class="m">Google's sign-in comes back to the computer running Daimon, so open the link it gives you on that computer.</p>`,
    microsoft: `<div class="help">Microsoft needs an app registration of your own (free, once):<ol>
        <li>At ${ext("https://entra.microsoft.com", "entra.microsoft.com")}: App registrations → New registration. Accounts: "any organizational directory and personal Microsoft accounts". No redirect address.</li>
        <li>Authentication → Advanced settings → Allow public client flows: <b>Yes</b>.</li>
        <li>Copy its Application (client) ID here.</li></ol></div>
      ${inp(f, "client_id", "Application (client) ID")}${sel(f, "tenant", "Accounts that can sign in", TENANTS, { redraw: 1 })}
      ${TENANTS.some(([v]) => v === f.tenant) && f.tenant !== "id" ? "" : inp(f, "tenant_id", "Tenant ID", { ph: "your organization's directory (tenant) ID" })}
      <p class="m">It then shows a short code. You type that code on a Microsoft page, on any device.</p>`,
    caldav: `${inp(f, "url", "Server address", { ph: "https://cloud.example.com/remote.php/dav" })}${inp(f, "username", "Username")}${inp(f, "password", "Password", { type: "password" })}`,
    ics: `${inp(f, "url", "Calendar link", { ph: "https://… .ics or webcal://…" })}${inp(f, "title", "Title (optional)")}<p class="m">A link like this only lets Daimon read the calendar.</p>`,
    ...Object.fromEntries(["apple", "fastmail", "yahoo"].map(k => [k, `${inp(f, "username", "Username (usually your email)")}${inp(f, "password", "App password", { type: "password" })}<p class="m">${PASS_HELP[k]}</p>`])),
  })[f.kind];
  const TENANTS = [["common", "Any Microsoft account"], ["consumers", "Personal accounts only"],
    ["organizations", "Work or school accounts only"], ["id", "One organization (a tenant ID)…"]];
  const need = { google: ["client_id", "client_secret"], microsoft: ["client_id", "tenant"], caldav: ["url", "username", "password"], ics: ["url", "title"], apple: ["username", "password"], fastmail: ["username", "password"], yahoo: ["username", "password"] };

  function connectAddon(key) {
    const root = drawer(key === "calendar" ? "Add a calendar" : key === "messages" ? "Connect text messages" : "Connect Canvas");
    const f = root._f = { kind: "google", name: "", tenant: "common" }, back = () => connectAddon(key);
    const start = async (b, fields, hint) => busy(b, async () => {
      const job = await api("/api/addons/" + key, { fields });
      for (const k of Object.keys(f)) if (k !== "kind") f[k] = "";  // secrets don't hang around
      watch(root, job, { hint, again: back });
    });
    const forms = {
      calendar: () => {
        root.innerHTML = `<div class="form"><div class="m">Which calendar?</div>${seg(KINDS, f.kind, "kind")}${calFields(f)}
          ${inp(f, "name", "Call it (optional)", { ph: f.kind })}</div>
          <div class="btns"><button class="btn primary" data-a="go">Connect</button></div>`;
        root._a = { kind: b => { f.kind = b.dataset.v; forms.calendar(); },
          go: b => start(b, { kind: f.kind, name: f.name, ...Object.fromEntries(need[f.kind].map(k => [k, f[k]])),
            ...(f.kind === "microsoft" && f.tenant === "id" ? { tenant: (f.tenant_id || "").trim() } : {}) }, f.kind) };
        root.onchange = e => { if (e.target.dataset.redraw) forms.calendar(); };  // e.g. a tenant ID box to show
      },
      canvas: () => {
        root.innerHTML = `<div class="form"><p class="m">Daimon reads what's due and your grades. It never changes anything in Canvas.</p>
          ${inp(f, "url", "Canvas address", { ph: "https://school.instructure.com" })}
          <div class="help">Make a token in Canvas: <b>Account → Settings → + New access token</b>. Paste it below.</div>
          ${inp(f, "token", "Access token", { type: "password" })}</div>
          <div class="btns"><button class="btn primary" data-a="go">Connect</button></div>`;
        root._a = { go: b => start(b, { url: f.url, token: f.token }) };
      },
      messages: () => {
        root.innerHTML = `<div class="form"><p class="m">Google Messages pairs through your Google account. To copy what it needs:</p>
          <div class="help"><ol>
            <li>In a private window, open developer tools (F12) → Network. Then go to ${ext("https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config", "this sign-in page")} and sign in. Use only one Google account in that window.</li>
            <li>It lands on messages.google.com/web/config. Right-click the <b>config</b> request in the Network list (reload if it isn't there) → Copy → <b>Copy as cURL (bash)</b>. Firefox works as is. In Chrome the cookies can be tied to the device, so Firefox is safer.</li>
            <li>Paste it below. Then close that private window without signing out, because signing out kills the cookies.</li></ol></div>
          <label>What you copied<textarea class="field mono" name="cookies" spellcheck="false" placeholder="curl 'https://messages.google.com/web/config' …"></textarea></label></div>
          <p class="m" style="margin-top:10px">Then your phone shows an emoji to tap in Google Messages, under Device pairing.</p>
          <div class="btns"><button class="btn primary" data-a="go">Pair</button></div>`;
        root._a = { go: b => start(b, { cookies: f.cookies }) };
      },
    };
    forms[key]?.();
  }

  // Shows a running setup job: its output, a Cancel button, and what to do next (an emoji to tap, a code to type).
  async function watch(root, job, o) {
    root.innerHTML = `<div data-r="special"></div><div class="joblog" data-r="log"></div><div class="btns" data-r="state"></div>`;
    const q = n => root.querySelector(`[data-r=${n}]`);
    let cancelled = false;
    root._a = { cancel: () => { cancelled = true; api("/api/jobs/" + job.id + "/cancel", {}).catch(fail); }, close: () => closeDrawer(), again: () => o.again() };
    const paint = j => {
      const log = q("log"), end = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
      log.innerHTML = j.lines.map(linkify).join("\n") || "Starting…";
      if (end) log.scrollTop = log.scrollHeight;
      const lines = j.lines.join("\n"), emoji = [...lines.matchAll(/Tap this emoji on your phone:\s*(.+)/g)].pop()?.[1],
        code = [...lines.matchAll(/enter the code\s+([A-Z0-9-]{6,})/gi)].pop()?.[1];
      q("special").innerHTML = j.done && j.ok ? "" : emoji ? `<div class="bigbox"><div class="m">Tap this emoji in Google Messages on your phone</div><div class="v">${esc(emoji)}</div></div>`
        : code ? `<div class="bigbox"><div class="m">Type this code on the Microsoft page</div><div class="v code">${esc(code)}</div></div>` : "";
      q("state").innerHTML = !j.done ? `<span class="spin"></span><span class="m">${cancelled ? "Stopping…" : "Working…"}</span><button class="btn sm" data-a="cancel" style="margin-left:auto">Cancel</button>`
        : j.ok ? `<span class="ok">✓ Done</span><button class="btn primary" data-a="close" style="margin-left:auto">Close</button>`
          : `<span class="er">That didn't work</span><button class="btn" data-a="again" style="margin-left:auto">Try again</button>`;
    };
    let j = job;
    for (;;) {
      paint(j);
      if (j.done || !alive(root)) break;
      await sleep(1000);
      if (!alive(root)) break;
      try { j = await api("/api/jobs/" + j.id); } catch (e) { j = { ...j, done: true, ok: false, lines: [...j.lines, e.message] }; }
    }
    if (j.done) load().catch(() => { });
  }

  async function manage(key) {
    const ad = (C?.addons || []).find(a => a.key === key), root = drawer(ad?.title || key, `<span class="faint" style="font-size:12px">${esc(ad?.blurb || "")}</span>`);
    root.innerHTML = `<h6>Status</h6><div class="joblog">Asking it…</div>`;
    let text = "";
    try { text = (await api(`/api/addons/${key}/status`)).text; } catch (e) { text = e.message; }
    if (!alive(root)) return;
    const accounts = key === "calendar" ? [...text.matchAll(/^([\w.-]+) \(([^,)]+),/gm)].map(m => ({ name: m[1], kind: m[2] })) : [];
    const draw = () => {
      root.innerHTML = `<h6 style="margin-top:0">Status</h6><div class="joblog">${linkify(text)}</div>
        ${key === "calendar" ? `<h6>Accounts</h6>${accounts.map(a => `<div class="line"><span class="mono">${esc(a.name)}</span><span class="tag">${esc(a.kind)}</span><button class="btn sm ghost danger" data-a="rm" data-n="${esc(a.name)}" style="margin-left:auto">Remove</button></div>`).join("")
          || `<p class="m">No accounts found in that.</p>`}
          <div class="form" style="margin-top:8px">${inp(root._f, "rmName", "Remove an account by name")}</div>
          <div class="btns"><button class="btn sm" data-a="rmGo">Remove</button></div>` : ""}
        <div class="btns">${key === "calendar" ? `<button class="btn primary" data-a="redo">+ Add a calendar</button>`
          : `<button class="btn primary" data-a="redo">${key === "messages" ? "Pair again" : "Log in again"}</button>`}</div>`;
    };
    root._f = { rmName: "" };
    const remove = (b, name) => name && confirm(`Remove the account ${name}? Its calendars aren't touched.`) && busy(b, async () => {
      const r = await api("/api/addons/calendar/remove", { name }); toast(esc(r.text || "Removed")); manage(key); load().catch(() => { });
    });
    root._a = { redo: () => connectAddon(key), rm: b => remove(b, b.dataset.n), rmGo: b => remove(b, root._f.rmName.trim()) };
    draw();
  }

  // ---- MCP servers ------------------------------------------------------------------------------------
  async function openServer(name) {
    const root = drawer(name, `<span class="faint" style="font-size:12px">a set of tools</span>`);
    root.innerHTML = `<p class="m">Loading…</p>`;
    let d, all = false;
    const fetchIt = async () => { d = await api("/api/servers/" + enc(name)); };
    try { await fetchIt(); } catch (e) { root.innerHTML = `<p class="er">${esc(e.message)}</p>`; return; }
    const draw = () => {
      const shown = all ? d.tools : d.tools.slice(0, 8), kv = o => Object.entries(o || {}).map(([k, v]) => `<div class="line" style="padding:2px 0"><span class="mono">${esc(k)}</span><span class="faint mono">${esc(v)}</span></div>`).join("");
      $("#dMeta").innerHTML = d.disabled ? `<span class="status-tag cancelled">off</span>` : d.error ? `<span class="status-tag failed">not working</span>` : `<span class="status-tag done">connected</span>`;
      root.innerHTML = `<div class="line" style="padding-top:0"><span class="muted">${d.disabled ? "Off" : "On"}</span><button class="sw${d.disabled ? "" : " on"}" role="switch" aria-checked="${!d.disabled}" aria-label="On or off" data-a="toggle" style="margin-left:auto"></button></div>
        <h6>${d.url ? "Web address" : "Command on this computer"}</h6>
        <div class="urlbox"><span>${esc(d.url || [d.command, ...d.args].join(" "))}</span></div>
        ${Object.keys(d.headers).length ? `<h6>Headers</h6>${kv(d.headers)}` : ""}${Object.keys(d.env).length ? `<h6>Environment</h6>${kv(d.env)}` : ""}
        ${d.error ? `<h6>Error</h6><div class="joblog er">${esc(d.error)}</div>` : ""}
        <h6>Tools · ${d.tools.length}</h6>
        ${d.tools.length ? shown.map(t => `<div class="tl"><div class="h">${esc(t.name)}${t.read_only ? ` <span class="tag">read-only</span>` : ""}</div>
          ${t.about ? `<div class="m">${esc(t.about)}</div>` : ""}${seg(LV, t.level, "lv", `data-t="${esc(t.name)}"`)}</div>`).join("")
          + (d.tools.length > 8 && !all ? `<div class="btns"><button class="btn sm" data-a="all">Show all ${d.tools.length}</button></div>` : "")
          : `<p class="m">${d.disabled ? "Turn it on to see its tools." : "No tools yet. They show up once it connects."}</p>`}
        <h6>Workers that can use it</h6>
        ${d.workers.length ? `<div class="filters">${d.workers.map(w => `<span class="tag">${esc(w)}</span>`).join("")}</div>` : `<p class="m">None.</p>`}
        <div class="btns" style="margin-top:22px"><button class="btn ghost danger" data-a="rm">Remove this server</button></div>`;
    };
    root._a = {
      all: () => { all = true; draw(); },
      toggle: b => busy(b, async () => {
        await api("/api/servers/" + enc(name), { action: "toggle", on: d.disabled });
        toast(d.disabled ? "On. Its tools appear on the agent's next step." : "Off"); await fetchIt(); draw(); load().catch(() => { });
      }),
      lv: b => busy(b, async () => {
        await api("/api/servers/" + enc(name), { action: "consent", tool: b.dataset.t, level: b.dataset.v });
        d.tools.find(t => t.name === b.dataset.t).level = b.dataset.v; draw(); load().catch(() => { });
      }),
      rm: b => confirm(`Remove ${name}? Its tools go away.${d.url || d.command ? "" : ""}`) && busy(b, async () => {
        await api("/api/servers/" + enc(name), { action: "remove" }); closeDrawer(); toast("Removed"); await load();
      }),
    };
    draw();
  }

  function addServerDrawer() {
    const root = drawer("Add a server", `<span class="faint" style="font-size:12px">give the agent more tools</span>`);
    const f = root._f = { name: "", mode: "url", url: "", hname: "Authorization", hsecret: "", hval: "", cmd: "", args: "", env: "" };
    const draw = () => {
      root.innerHTML = `<div class="form">${inp(f, "name", "Name", { ph: "github" })}
        <div class="m">How does it run?</div>${seg([["url", "Web address"], ["cmd", "A command on this computer"]], f.mode, "mode")}
        ${f.mode === "url" ? `${inp(f, "url", "Address", { ph: "https://example.com/mcp" })}
          <div class="help">Only if it needs a key: a header name and its value. For a token, write <span class="mono">Bearer</span> and the token. The value is stored as a secret and never shown again.</div>
          ${inp(f, "hname", "Header name")}
          ${sel(f, "hsecret", "Header value", [["", "Paste a new value"], ...(C?.secrets || []).filter(s => s.set).map(s => [s.name, `Saved secret: ${s.name}`])], { redraw: 1 })}
          ${f.hsecret ? "" : inp(f, "hval", "New value", { type: "password" })}`
          : `${inp(f, "cmd", "Command", { ph: "npx" })}
          <label>Arguments, one per line<textarea class="field mono" name="args" spellcheck="false" placeholder="-y&#10;@modelcontextprotocol/server-memory">${esc(f.args)}</textarea></label>
          <label>Environment, one NAME=value per line (optional)<textarea class="field mono" name="env" spellcheck="false" placeholder="API_KEY=…">${esc(f.env)}</textarea></label>`}
        </div><p class="m" style="margin-top:10px">Its tools appear on the agent's next step. New tools follow the defaults for new tools, until you change them.</p>
        <div class="btns"><button class="btn primary" data-a="add">Add server</button></div>`;
    };
    root._a = {
      mode: b => { f.mode = b.dataset.v; draw(); },
      add: b => busy(b, async () => {
        const body = { name: f.name.trim() };
        if (f.mode === "url") {
          body.url = f.url.trim();
          const h = f.hname.trim() || "Authorization";  // a saved secret goes in by reference; tokens want "Bearer "
          const v = f.hsecret ? `${/^authorization$/i.test(h) ? "Bearer " : ""}\${${f.hsecret}}` : f.hval.trim();
          if (v) body.headers = { [h]: v };
        }
        else {
          body.command = f.cmd.trim(); body.args = f.args.split("\n").map(x => x.trim()).filter(Boolean);
          body.env = Object.fromEntries(f.env.split("\n").map(l => l.trim()).filter(l => l.includes("=")).map(l => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]));
        }
        await api("/api/servers", body); f.hval = ""; closeDrawer(); toast("Added. Its tools appear on the agent's next step."); await load();
      }),
    };
    root.onchange = e => { if (e.target.dataset.redraw) draw(); };  // a saved secret needs no value box
    draw();
  }

  // ---- secrets ----------------------------------------------------------------------------------------
  function secretDrawer(name) {
    const root = drawer(name ? `Replace ${name}` : "Add a secret", `<span class="faint" style="font-size:12px">stored on this computer</span>`);
    const f = root._f = { name, value: "" };
    root.innerHTML = `<div class="form">${name ? `<p class="m">The current value is never shown. Paste the new one.</p>` : `<p class="m">A name in CAPITALS (like <span class="mono">GITHUB_TOKEN</span>) and its value. Anything that refers to <span class="mono">\${NAME}</span> picks it up.</p>`}
      <label>Name<input class="field mono" name="name" value="${esc(name)}" ${name ? "readonly" : ""} placeholder="MY_SECRET" autocomplete="off" autocapitalize="characters" spellcheck="false"></label>
      ${inp(f, "value", "Value", { type: "password" })}</div>
      <p class="m" style="margin-top:10px">Values are never shown on this page, only the last four characters.</p>
      <div class="btns"><button class="btn primary" data-a="save">Save</button></div>`;
    root._a = { save: b => busy(b, async () => {
      await api("/api/secrets", { name: f.name.trim(), value: f.value }); f.value = ""; closeDrawer(); toast("Saved"); await load();
    }) };
    $("input[name=value]", root).focus();
  }

  Object.assign(window, { openTelegram, connectAddon, openLink });
  LOAD.connections = () => load().catch(fail);
  CHANGED.push(what => { if (what === "connections" || what === "settings") load().catch(() => { }); });
  load().catch(() => { });
})();
