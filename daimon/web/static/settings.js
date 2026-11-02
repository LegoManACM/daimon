"use strict";
// The settings view: config.yaml, models.yaml, provider keys, the background service and the agent's files.
(() => {
  const st = { data: null, svc: null };
  const root = $("#v-settings");
  document.head.insertAdjacentHTML("beforeend", `<style>
#v-settings .mini { flex-wrap: wrap; }
#v-settings .mini .t { min-width: 120px; }
#v-settings .emb { display: flex; gap: 10px; align-items: center; flex: none; }
#v-settings select.field.addr { width: auto; padding: 3px 10px; border-radius: 999px; font-size: 12px; color: var(--muted); }
#v-settings select.field.addr.on { border-color: var(--ember-line); background: var(--ember-dim); color: var(--ember); }
.tagpick { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
#v-settings .mini.model { flex-wrap: nowrap; }  /* one line per model, however many tags: the tags shorten instead */
#v-settings .mini.model > * { flex: none; }
#v-settings .mini.model .t { flex: 0 1 auto; min-width: 60px; }
#v-settings .mini.model .gf { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#v-settings .row-item.block { align-items: flex-start; }
#v-settings .row-item.block > .stack { flex: 1; min-width: 0; }
#v-settings .row-item.block > .main.lab { flex: 0 0 260px; }
#v-settings .field.wide { width: 280px; max-width: 100%; }
#v-settings select.field { max-width: 100%; }
#v-settings .approval { margin-top: 14px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
#v-settings .approval .main { flex: 1; min-width: 200px; }
.drawer textarea.field.file { width: 100%; height: 58vh; min-height: 260px; font: 12.5px/1.5 var(--mono); resize: vertical; white-space: pre; box-sizing: border-box; }
.drawer .btns { display: flex; gap: 8px; margin-top: 14px; }
.drawer .err-line { color: var(--err); font-size: 12.5px; margin-top: 8px; white-space: pre-wrap; }
.drawer .pick { cursor: pointer; }
@media (max-width: 820px) {
  #v-settings .chips { flex-wrap: nowrap; overflow-x: auto; padding-bottom: 2px; scrollbar-width: none; }
  #v-settings .chips::-webkit-scrollbar { display: none; }
  #v-settings .chips .filter { flex: none; }
  #v-settings .row-item.block { flex-direction: column; align-items: stretch; }
  #v-settings .row-item.block > .main.lab { flex: none; }
  #v-settings .field.wide, #v-settings select.field.wide { width: 100%; }
  #v-settings .row-item.setting .filters { max-width: 100%; }
  #v-settings .mini .btn { min-height: 36px; }
  #v-settings .mini.model .gf, #v-settings .mini.model .pr { display: none; }  /* the name needs the room; Edit shows them */
  #v-settings .mini.model .t { flex: 1 1 auto; }
  .drawer textarea.field.file { height: 50vh; font-size: 13px; }
}
</style>`);

  const SHORT = { behaviour: "Behaviour", calls: "Model calls", tools: "Tools & jobs", workers: "Workers", knowledge: "Knowledge", web: "Web page", running: "Running & updates" };
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  const shortId = id => String(id).split("/").slice(1).join("/") || id;
  const provOf = id => String(id).split("/")[0];
  const modelName = id => String(id).split("/").pop();  // claude-haiku-5.5, not openrouter/anthropic/claude-haiku-5.5
  // the provider only when two models in the list share a name
  const twin = id => (st.data?.models?.models || []).filter(m => modelName(m.id) === modelName(id)).length > 1;
  const sha = c => c ? ` <span class="faint mono">· ${esc(c)}</span>` : "";
  const items = () => st.data.sections.flatMap(s => s.items);
  const find = k => items().find(i => i.key === k);

  function shell() {
    root.innerHTML = `<div class="view-head"><h2>Settings</h2><span class="muted" style="font-size:12.5px">config.yaml and models.yaml · every change is a commit</span><div class="spacer"></div>
      <label class="search"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg><input id="setQ" placeholder="Search settings…" aria-label="Search settings"></label></div>
      <div class="view-head" style="padding-top:10px;padding-bottom:10px"><div class="filters chips" id="setChips"></div></div>
      <div class="scroll" id="setScroll"><div class="list"><div id="setPending"></div><div id="setBody"><p class="faint" style="margin-top:16px">Loading…</p></div></div></div>`;
  }

  // ---- rendering ---------------------------------------------------------------------------------------
  function control(it) {
    const k = esc(it.key), v = it.value;
    if (it.kind === "bool") return `<button class="sw${v ? " on" : ""}" role="switch" aria-checked="${!!v}" aria-label="${esc(it.label)}" data-k="${k}"></button>`;
    if (it.kind === "choice") {
      const known = it.options.some(([o]) => o === v);
      const pills = it.options.map(([o, l], i) => `<button class="filter${o === v ? " on" : ""}" data-k="${k}" data-i="${i}">${esc(l)}</button>`);
      if (it.key === "web.listen") {  // or one address of this computer, from its own list
        const addrs = [...new Set([...(st.data.addresses || []), ...(!known && v ? [v] : [])])];
        pills.push(`<select class="field addr${!known && v ? " on" : ""}" data-k="${k}" aria-label="A specific address"><option value="">One address…</option>${addrs.map(a => `<option${a === v ? " selected" : ""}>${esc(a)}</option>`).join("")}</select>`);
      }
      return `<div class="filters" role="group" aria-label="${esc(it.label)}">${pills.join("")}</div>`;
    }
    if (it.kind === "select") return `<select class="field" data-k="${k}" aria-label="${esc(it.label)}">${(it.options.some(([o]) => o === v) ? it.options : [[v, v], ...it.options]).map(([o, l]) => `<option value="${esc(o)}"${o === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
    if (it.key === "knowledge.embeddings") {  // picked from a provider's list, not typed
      const on = v && v !== "none";
      return `<div class="emb"><span class="${on ? "mono" : "faint"}" title="${esc(on ? v : "")}">${esc(on ? modelName(v) : "off")}</span><button class="btn sm" data-embed="1">${on ? "Change" : "Choose a model"}</button></div>`;
    }
    if (it.kind === "text") return `<input class="field wide" data-k="${k}" value="${esc(v ?? "")}" aria-label="${esc(it.label)}" placeholder="${esc(it.default ?? "")}" autocomplete="off">`;
    return `<input class="field num" inputmode="numeric" data-k="${k}" value="${typeof v === "number" ? v : ""}" aria-label="${esc(it.label)}"${it.kind === "limit" ? ` placeholder="no limit"` : ""}>`;
  }
  function item(it) {
    const changed = it.default != null && !same(it.value, it.default);
    const dv = it.default == null ? "" : it.options.find(([o]) => o === it.default)?.[1] ?? (typeof it.default === "object" ? JSON.stringify(it.default) : it.default);
    const dflt = changed && it.kind !== "text" ? ` Default ${dv}.` : "";
    const sub = [it.help, it.unit && it.kind !== "bool" ? `In ${it.unit}.` : ""].filter(Boolean).join(" ") + dflt;
    return `<div class="row-item setting" data-q="${esc(`${it.label} ${it.help} ${it.key}`.toLowerCase())}"><div class="main"><p class="title">${esc(it.label)}${changed ? ` <span class="tag ch">changed</span>` : ""}${it.restart ? ` <span class="tag rs">restart</span>` : ""}</p>${sub ? `<p class="sub">${esc(sub)}</p>` : ""}</div>${control(it)}</div>`;
  }
  function modelOpts(cur) {
    const ms = st.data.models.models;
    return (ms.some(m => m.id === cur) ? ms : [{ id: cur }, ...ms]).map(m => `<option value="${esc(m.id)}"${m.id === cur ? " selected" : ""}>${esc(modelName(m.id) + (twin(m.id) ? " · " + provOf(m.id) : ""))}</option>`).join("");
  }
  function modelsSection() {
    const { models: M, providers: P } = st.data;
    const rows = M.models.map(m => `<div class="mini model" title="${esc(m.id)}"><span class="t mono">${esc(modelName(m.id))}${twin(m.id) ? ` <span class="faint">${esc(provOf(m.id))}</span>` : ""}</span>
      ${m.tier ? `<span class="tier ${esc(m.tier)}">${esc(m.tier)}</span>` : ""}${m.approval === "ask" ? `<span class="tag" title="Asks you each time">asks first</span>` : ""}
      <span class="muted gf" style="font-size:12px" title="${esc((m.good_for || []).join(", "))}">${esc((m.good_for || []).join(", "))}</span>${m.price ? `<span class="faint mono pr" style="font-size:11px">$${esc(m.price.in)}/${esc(m.price.out)}</span>` : ""}
      <button class="btn sm" data-edit="${esc(m.id)}">Edit</button><button class="btn sm ghost" data-rm="${esc(m.id)}" aria-label="Remove ${esc(m.id)}">Remove</button></div>`).join("");
    const keys = P.map(p => `<div class="mini"><span class="dot${p.local || p.set ? "" : " idle"}"></span><span class="t">${esc(p.title)}</span>
      ${p.local ? `<span class="faint" style="font-size:12px">no key needed</span>` : p.set ? `<span class="mono faint">…${esc(p.last4)}</span><button class="btn sm" data-key="${esc(p.key)}">Replace</button>` : `<span class="faint" style="font-size:12px">not set</span><button class="btn sm" data-key="${esc(p.key)}">Add key</button>`}</div>`).join("");
    return `<section data-sec="models"><p class="gh">Models <span class="tag">models.yaml</span></p>
      <div class="row-item setting" data-q="main model answers you"><div class="main"><p class="title">Main model</p><p class="sub">Answers you and runs the main agent. Same as the main pill up top.</p></div><select class="field wide" id="setMain" aria-label="Main model">${modelOpts(M.main)}</select></div>
      <div class="row-item setting" data-q="default for new workers default_for_new_workers"><div class="main"><p class="title">Default for new workers</p><p class="sub"><span class="tag">default_for_new_workers</span></p></div><select class="field wide" id="setDef" aria-label="Default for new workers">${modelOpts(M.default)}</select></div>
      <div class="row-item block" data-q="models workers may use add model price tier"><div class="main lab"><p class="title">Models workers may use</p><p class="sub">Picked from each provider's live list, with prices per million tokens.</p></div><div class="stack">${rows}<button class="btn sm" id="setAddModel" style="margin-top:2px">+ Add a model</button></div></div>
      <div class="row-item block" data-q="provider keys api key anthropic openai"><div class="main lab"><p class="title">Provider keys</p><p class="sub">Set or replace; a key is never shown again.</p></div><div class="stack">${keys}</div></div></section>`;
  }
  function runningExtra() {
    const s = st.svc;
    const svc = !s ? `Checking…` : `<span class="dot${s.service ? "" : " idle"}" style="margin-right:6px"></span>${s.service ? "Running as a background service" : "Not running as a background service"}${(s.lines || []).map(l => `<br>${esc(l.replace(/\s*\(log:.*\)\s*$/, ""))}`).join("")}`;
    return `<div class="row-item setting" data-q="background service restart running"><div class="main"><p class="title">Background service</p><p class="sub">${svc}</p></div><button class="btn" data-restart="1">Restart</button></div>
      <div class="row-item setting" data-q="version update release latest"><div class="main"><p class="title">Version</p><p class="sub">${s ? `You're on ${esc(s.version)}${s.latest ? ` · ${esc(s.latest)} is out` : ""}` : "…"}</p></div>${s?.latest ? `<button class="btn primary" data-update="${esc(s.latest)}">Update now</button>` : ""}</div>`;
  }
  function filesSection() {
    return `<section data-sec="files"><p class="gh">Files</p><div class="row-item setting" data-q="files edit config.yaml models.yaml mcp.json policy self memory context"><div class="main"><p class="title">Edit the files directly</p><p class="sub">Checked before saving; each save is a commit you can undo in Changes.</p></div>
      <div class="filters">${st.data.editable.map(f => `<button class="btn sm mono" data-file="${esc(f)}">${esc(f)}</button>`).join("")}</div></div></section>`;
  }
  function pendingBar() {
    const p = st.data?.pending || [];
    const c = $("#navSettings"); if (c) c.textContent = p.length || "";
    const el = $("#setPending"); if (!el) return;
    el.innerHTML = p.length ? `<div class="approval" role="status"><span class="dot warn"></span><div class="main"><p style="font-size:13px;font-weight:500;margin:0">${p.length === 1 ? "1 change waits" : p.length + " changes wait"} for a restart</p><p class="muted" style="font-size:12.5px;margin:0">${p.map(esc).join(" · ")}</p></div><button class="btn primary" data-restart="1">Restart agent</button></div>` : "";
  }
  function render() {
    if (!st.data || !$("#setBody")) return;
    const sc = $("#setScroll"), top = sc.scrollTop;
    const secs = st.data.sections.map(s => `<section data-sec="${esc(s.id)}"><p class="gh">${esc(s.title)} <span class="tag${s.id === "web" ? " rs" : ""}">${s.id === "web" ? "restart to apply" : "config.yaml"}</span></p>${s.id === "running" ? runningExtra() : ""}${s.items.map(item).join("")}</section>`);
    $("#setBody").innerHTML = modelsSection() + secs.join("") + filesSection();
    const cur = $("#setChips .on")?.dataset.go || "models";
    const chips = [["models", "Models"], ...st.data.sections.map(s => [s.id, SHORT[s.id] || s.title]), ["files", "Files"]];
    $("#setChips").innerHTML = chips.map(([id, l]) => `<button class="filter${id === cur ? " on" : ""}" data-go="${esc(id)}">${esc(l)}</button>`).join("");
    pendingBar(); filter(); sc.scrollTop = top;
  }
  function filter() {
    const q = ($("#setQ")?.value || "").trim().toLowerCase();
    $$("#setBody section").forEach(s => {
      let any = false;
      $$(".row-item", s).forEach(r => { const on = !q || r.dataset.q.includes(q); r.style.display = on ? "" : "none"; any ||= on; });
      s.style.display = any ? "" : "none";
    });
    const none = $("#setNone");
    if (q && !$$("#setBody section").some(s => s.style.display !== "none")) { if (!none) $("#setBody").insertAdjacentHTML("beforeend", `<p class="empty" id="setNone">No setting matches "${esc(q)}".</p>`); }
    else none?.remove();
  }

  // ---- loading -----------------------------------------------------------------------------------------
  const typing = () => { const a = document.activeElement; return a && $("#setBody")?.contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName); };
  async function load(force) {
    if (!$("#setBody")) shell();
    try {
      st.data = await api("/api/settings"); pendingBar();
      if (force || !typing()) render();
    } catch (e) { fail(e); }
    api("/api/service").then(s => { st.svc = s; if (S.view === "settings" && !typing()) render(); }).catch(() => { });
  }
  LOAD.settings = () => load(true);
  if (S.view === "settings") load(true); // the page was opened straight on this view
  let t0;
  CHANGED.push(what => { if (S.view === "settings" && (what === "settings" || what === "models")) { clearTimeout(t0); t0 = setTimeout(() => load(), 200); } });
  // the nav count shows before the view is ever opened
  api("/api/settings").then(d => { if (!st.data) { st.data = d; pendingBar(); } }).catch(() => { });

  // ---- saving a setting --------------------------------------------------------------------------------
  const undo = c => c ? () => api(`/api/changes/${c}/undo`, {}).then(() => load(true)).catch(fail) : null;
  async function save(key, value) {
    try {
      const r = await api("/api/settings", { key, value });
      const it = find(key); if (it) it.value = r.value;
      st.data.pending = r.pending; render();
      toast(`Saved${sha(r.commit)}`, undo(r.commit));
    } catch (e) { fail(e); render(); }
  }
  function commitInput(inp) {
    const it = find(inp.dataset.k); if (!it) return;
    const raw = inp.value.trim();
    if (raw === (it.kind === "text" ? it.value ?? "" : typeof it.value === "number" ? String(it.value) : "")) return;
    save(it.key, raw);
  }
  async function restart() {
    try { await restartAgent(); } catch (e) { fail(e); }
  }

  root.addEventListener("click", async e => {
    const b = e.target.closest("button"); if (!b || !st.data) return;
    const d = b.dataset;
    if (d.go) {
      $$("#setChips .filter").forEach(c => c.classList.toggle("on", c === b));
      $("#setQ").value = ""; filter();
      $(`#setBody section[data-sec="${d.go}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" }); return;
    }
    if (b.classList.contains("sw") && d.k) return save(d.k, !b.classList.contains("on"));
    if (d.i !== undefined && d.k) { const it = find(d.k), v = it.options[+d.i][0]; if (!same(v, it.value)) save(d.k, v); return; }
    if (d.restart) return restart();
    if (d.update) {
      if (confirm(`Update to ${d.update} now? The agent restarts when it's done.`)) try { await api("/api/update", {}); toast(`Updating to ${esc(d.update)}…`); } catch (x) { fail(x); }
      return;
    }
    if (d.file) return editFile(d.file);
    if (d.edit) return editModel(st.data.models.models.find(m => m.id === d.edit));
    if (d.rm) {
      if (!confirm(`Remove ${d.rm} from the list? Workers can't use it afterwards.`)) return;
      try { await api("/api/models/remove", { id: d.rm }); toast(`Removed ${esc(shortId(d.rm))}`); load(true); } catch (x) { fail(x); }
      return;
    }
    if (d.key) return keyDrawer(st.data.providers.find(p => p.key === d.key));
    if (b.id === "setAddModel") return addModel();
    if (d.embed) return embedPicker();
  });
  root.addEventListener("change", async e => {
    const t = e.target;
    if (t.id === "setMain" || t.id === "setDef") {
      const main = t.id === "setMain";
      try {
        const r = await api(main ? "/api/model" : "/api/models/default", { model: t.value });
        if (main) { S.models = { main: r.main, models: r.models }; try { renderTop(); } catch { } }
        toast(`${main ? "Main model" : "New workers"}: <b>${esc(shortId(t.value))}</b>${sha(r.commit)}`);
        load(true);
      } catch (x) { fail(x); load(true); }
    } else if (t.matches("select.field[data-k]")) { if (t.value) save(t.dataset.k, t.value); }
    else if (t.matches("input.field[data-k]")) commitInput(t);
  });
  root.addEventListener("keydown", e => { if (e.key === "Enter" && e.target.matches("input.field[data-k]")) e.target.blur(); });
  root.addEventListener("input", e => { if (e.target.id === "setQ") filter(); });
  // the chip for the section in view
  document.addEventListener("scroll", e => {
    const sc = $("#setScroll"); if (e.target !== sc) return;
    let cur = null;
    $$("#setBody section").filter(s => s.style.display !== "none").forEach(s => { if (s.getBoundingClientRect().top <= sc.getBoundingClientRect().top + 60) cur = s; });
    if (cur) $$("#setChips .filter").forEach(c => c.classList.toggle("on", c.dataset.go === cur.dataset.sec));
  }, true);

  // ---- drawers -----------------------------------------------------------------------------------------
  const btns = ok => `<div class="btns"><button class="btn primary" id="dOk">${ok}</button><button class="btn" onclick="closeDrawer()">Cancel</button></div><p class="err-line" id="dErr"></p>`;
  function modelForm(m, isNew) {
    const p = m.price || {}, n = v => v == null ? "" : v;
    return `<div class="form"><label>Tier<select class="field" id="mTier">${["cheap", "standard", "strong", "free"].map(t => `<option${m.tier === t ? " selected" : ""}>${t}</option>`).join("")}</select></label>
      <div style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)">Good for<div class="tagpick" id="mGood">${goodTags(m.good_for || []).map(t => `<button type="button" class="filter${(m.good_for || []).includes(t) ? " on" : ""}" data-tag="${esc(t)}">${esc(t)}</button>`).join("")}<input class="field" id="mGoodNew" placeholder="+ another" autocomplete="off" aria-label="Add a tag" style="width:110px;padding:3px 10px;border-radius:999px;font-size:12px"></div></div>
      <div class="row-item" style="border:0;padding:0"><div class="main"><p class="title">Needs my OK each time</p><p class="sub">Workers ask before they use it.</p></div><button class="sw${m.approval === "ask" ? " on" : ""}" id="mAsk" role="switch" aria-checked="${m.approval === "ask"}" aria-label="Needs my OK each time" type="button"></button></div>
      <label>Price per million tokens: in<input class="field" id="mIn" inputmode="decimal" value="${n(p.in)}" autocomplete="off"></label>
      <label>Price per million tokens: out<input class="field" id="mOut" inputmode="decimal" value="${n(p.out)}" autocomplete="off"></label></div>${btns(isNew ? "Add model" : "Save")}`;
  }
  // what models are good for: the tags the list already uses, and the usual ones
  const goodTags = mine => [...new Set([...mine, ...st.data.models.models.flatMap(m => m.good_for || []),
    "general", "agentic", "coding", "research", "writing", "summaries", "triage", "curation", "scheduling", "vision"])];
  function wireModelForm(id, replace) {
    const ask = $("#mAsk"); ask.onclick = () => { ask.classList.toggle("on"); ask.setAttribute("aria-checked", ask.classList.contains("on")); };
    $("#mGood").onclick = e => { const t = e.target.closest("[data-tag]"); if (t) t.classList.toggle("on"); };
    $("#mGoodNew").onkeydown = e => {
      const v = e.target.value.trim().toLowerCase(); if (e.key !== "Enter" || !v) return; e.preventDefault();
      const have = $(`#mGood [data-tag="${CSS.escape(v)}"]`);
      if (have) have.classList.add("on"); else e.target.insertAdjacentHTML("beforebegin", `<button type="button" class="filter on" data-tag="${esc(v)}">${esc(v)}</button>`);
      e.target.value = "";
    };
    $("#dOk").onclick = async () => {
      const good_for = $$("#mGood [data-tag].on").map(b => b.dataset.tag);
      const entry = { id, tier: $("#mTier").value, good_for, approval: ask.classList.contains("on") ? "ask" : "" };
      if ($("#mIn").value.trim() !== "" && $("#mOut").value.trim() !== "") entry.price = { in: +$("#mIn").value, out: +$("#mOut").value };
      try { const r = await api("/api/models/save", { entry, replace }); closeDrawer(); toast(`Saved ${esc(shortId(id))}${sha(r.commit)}`, undo(r.commit)); load(true); } catch (x) { $("#dErr").textContent = x.message; }
    };
  }
  function editModel(m) {
    openDrawer(shortId(m.id), `<span class="tag">${esc(provOf(m.id))}</span>`, modelForm(m, false)); wireModelForm(m.id, m.id);
  }
  function addModel() {
    const ps = st.data.providers;
    openDrawer("Add a model", `<span class="faint" style="font-size:12px">from a provider's live list</span>`, `<div class="form"><label>Provider<select class="field" id="aProv">${ps.map(p => `<option value="${esc(p.key)}">${esc(p.title)}${p.set || p.local ? "" : " (no key yet)"}</option>`).join("")}</select></label>
      <label>Search<input class="field" id="aQ" placeholder="Type part of a name" autocomplete="off"></label></div><div id="aList" style="margin-top:12px"></div>`);
    const first = ps.find(p => p.set || p.local); if (first) $("#aProv").value = first.key;
    let timer, seq = 0, found = [];
    const run = async () => {
      const my = ++seq, list = $("#aList"); list.innerHTML = `<p class="faint">Looking…</p>`;
      try {
        const r = await api(`/api/providers/${encodeURIComponent($("#aProv").value)}/models?q=${encodeURIComponent($("#aQ").value.trim())}`); if (my !== seq) return;
        found = r.models;
        list.innerHTML = found.length ? found.map((m, i) => `<div class="mini pick" data-i="${i}" tabindex="0"><span class="t mono">${esc(m.id)}</span><span class="faint" style="font-size:12px">${esc(m.about || "")}</span></div>`).join("") : `<p class="faint">Nothing matches.</p>`;
      } catch (x) { if (my === seq) list.innerHTML = `<p class="err-line">${esc(x.message)}</p>`; }
    };
    const pick = i => {
      const m = found[i], have = st.data.models.models.some(x => x.id === m.id);
      $("#dTitle").textContent = shortId(m.id); $("#dMeta").innerHTML = `<span class="tag">${esc(provOf(m.id))}</span>${have ? ` <span class="tag rs">already in the list</span>` : ""}`;
      $("#dBody").innerHTML = modelForm({ id: m.id, tier: m.price && !m.price.in && !m.price.out ? "free" : "standard", price: m.price }, !have);
      wireModelForm(m.id, have ? m.id : null);
    };
    $("#aProv").onchange = run; $("#aQ").oninput = () => { clearTimeout(timer); timer = setTimeout(run, 250); };
    $("#aList").onclick = e => { const r = e.target.closest(".pick"); if (r) pick(+r.dataset.i); };
    $("#aList").onkeydown = e => { const r = e.target.closest(".pick"); if (r && e.key === "Enter") pick(+r.dataset.i); };
    run();
  }
  // the model for meaning-based search: a provider's embedding models (Anthropic has none)
  function embedPicker() {
    const ps = st.data.providers.filter(p => p.key !== "anthropic"), cur = find("knowledge.embeddings").value;
    const on = cur && cur !== "none";
    openDrawer("Meaning-based search", on ? `<span class="tag mono">${esc(cur)}</span>` : `<span class="faint" style="font-size:12px">off: notes are found by their words</span>`,
      `<p class="faint" style="font-size:12.5px;margin:0 0 12px">A model that turns each note into numbers, so the agent finds notes by what they mean, not only the words in them. Changing it re-reads every note once.</p>
      <div class="form"><label>Provider<select class="field" id="eProv">${ps.map(p => `<option value="${esc(p.key)}">${esc(p.title)}${p.set || p.local ? "" : " (no key yet)"}</option>`).join("")}</select></label>
      <label>Search<input class="field" id="eQ" placeholder="Type part of a name" autocomplete="off"></label></div><div id="eList" style="margin-top:12px"></div>
      ${on ? `<div class="btns" style="margin-top:14px"><button class="btn" id="eOff">Turn it off</button></div>` : ""}`);
    const mine = ps.find(p => on && cur.startsWith((p.key === "ollama" ? "ollama" : p.key) + "/")) || ps.find(p => p.set || p.local);
    if (mine) $("#eProv").value = mine.key;
    let timer, seq = 0, found = [];
    const run = async () => {
      const my = ++seq, list = $("#eList"); list.innerHTML = `<p class="faint">Looking…</p>`;
      try {
        const r = await api(`/api/providers/${encodeURIComponent($("#eProv").value)}/models?kind=embeddings&q=${encodeURIComponent($("#eQ").value.trim())}`); if (my !== seq) return;
        found = r.models;
        list.innerHTML = found.length ? found.map((m, i) => `<div class="mini pick" data-i="${i}" tabindex="0"><span class="t mono">${esc(modelName(m.id))}</span>${m.id === cur ? `<span class="tag ch">in use</span>` : ""}<span class="faint" style="font-size:12px">${esc(m.about || "")}</span></div>`).join("")
          : `<p class="faint">${$("#eQ").value.trim() ? "Nothing matches." : $("#eProv").value === "ollama" ? "No embedding models in Ollama yet. Get one with <span class=\"mono\">ollama pull nomic-embed-text</span>, then look again." : "This provider has no embedding models."}</p>`;
      } catch (x) { if (my === seq) list.innerHTML = `<p class="err-line">${esc(x.message)}</p>`; }
    };
    const pick = async i => { closeDrawer(); await save("knowledge.embeddings", found[i].id); };
    $("#eProv").onchange = run; $("#eQ").oninput = () => { clearTimeout(timer); timer = setTimeout(run, 250); };
    $("#eList").onclick = e => { const r = e.target.closest(".pick"); if (r) pick(+r.dataset.i); };
    $("#eList").onkeydown = e => { const r = e.target.closest(".pick"); if (r && e.key === "Enter") pick(+r.dataset.i); };
    if ($("#eOff")) $("#eOff").onclick = async () => { closeDrawer(); await save("knowledge.embeddings", "none"); };
    run();
  }
  function keyDrawer(p) {
    openDrawer(`${p.title} key`, p.set ? `<span class="mono faint">current …${esc(p.last4)}</span>` : "",
      `<div class="form"><label>${p.set ? "New key" : "Key"}<input class="field" id="kVal" type="password" autocomplete="new-password" spellcheck="false" placeholder="Paste it here"></label>${p.where ? `<p class="faint" style="font-size:12.5px;margin:0">Get one: ${/^https?:/.test(p.where) ? `<a href="${esc(p.where)}" target="_blank" rel="noopener">${esc(p.where)}</a>` : esc(p.where)}</p>` : ""}<p class="faint" style="font-size:12.5px;margin:0">It's checked with ${esc(p.title)} first, then saved on this computer. It isn't shown again.</p></div>${btns("Check and save")}`);
    const go = async () => {
      const ok = $("#dOk"); ok.disabled = true; $("#dErr").textContent = "";
      try { const r = await api("/api/providers/key", { provider: p.key, key: $("#kVal").value }); closeDrawer(); toast(`${esc(p.title)} key saved · ${r.models} models available`); load(true); }
      catch (x) { $("#dErr").textContent = x.message; ok.disabled = false; }
    };
    $("#dOk").onclick = go; $("#kVal").onkeydown = e => { if (e.key === "Enter") go(); }; $("#kVal").focus();
  }
  async function editFile(path) {
    openDrawer(path, `<span class="faint" style="font-size:12px">checked before saving</span>`, `<p class="faint">Loading…</p>`);
    try {
      const { text } = await api("/api/file?path=" + encodeURIComponent(path));
      $("#dBody").innerHTML = `<textarea class="field file" id="fText" spellcheck="false" aria-label="${esc(path)}">${esc(text)}</textarea>${btns("Save")}`;
      $("#dOk").onclick = async () => {
        $("#dErr").textContent = "";
        try { const r = await api("/api/file/save", { path, text: $("#fText").value }); closeDrawer(); toast(`Saved ${esc(path)}${sha(r.commit)}`, undo(r.commit)); load(true); }
        catch (x) { $("#dErr").textContent = x.message; }
      };
    } catch (x) { $("#dBody").innerHTML = `<p class="err-line">${esc(x.message)}</p>`; }
  }
})();
