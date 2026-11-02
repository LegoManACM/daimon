// Editing tasks and the schedule: the Edit form in the task drawer, the routine / reminder drawers, and "+ Add".
// Loaded after the main script (it uses its helpers: api, esc, toast, fail, openDrawer, closeDrawer, when, S ...).

const pad2 = n => String(n).padStart(2, "0");
// An ISO time as the value of a datetime-local input (local time), or "".
function localInput(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "";
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
const opts = (list, current, none) => (none ? [`<option value="">${esc(none)}</option>`] : [])
  .concat([...new Set([...list, current].filter(Boolean))].map(w => `<option value="${esc(w)}"${w === current ? " selected" : ""}>${esc(w)}</option>`)).join("");
async function ensureLists() { if (!S.tasks || !S.projects) await loadTasks(); }
function reloadSchedule() { if (S.view === "schedule") loadSchedule(); }
CHANGED.push(what => { if (what === "tasks" || what === "schedule") reloadSchedule(); });

// ---- edit a task ------------------------------------------------------------------------------------
// mode "wake": the schedule's reminder rows, where the text is all there is.
async function editTask(id, mode) {
  let t;
  try { await ensureLists(); t = await api(`/api/task/${encodeURIComponent(id)}`); } catch (e) { return fail(e); }
  const sec = t.sections || {}, running = t.status === "running", wake = mode === "wake";
  openDrawer(wake ? "Edit reminder" : "Edit task", `<span class="status-tag ${esc(t.status)}">${esc(t.status)}</span><span class="tag">${esc(t.id)}</span>`, `<div class="form">
    ${wake ? `<label>Reminder<textarea class="field" id="etBrief">${esc(sec.Brief || t.title)}</textarea></label>`
      : `<label>Title<input class="field" id="etTitle" value="${esc(t.title)}"></label>
    <label>Brief${running ? ` <span class="faint">(can't change while it runs: cancel it first)</span>` : ""}<textarea class="field" id="etBrief"${running ? " disabled" : ""}>${esc(sec.Brief || "")}</textarea></label>
    <label>Worker${running ? ` <span class="faint">(can't change while it runs)</span>` : ""}<select class="field" id="etWorker"${running ? " disabled" : ""}>${opts(S.workers, t.worker)}</select></label>`}
    <label>When${!wake && (t.status === "queued" || t.status === "proposed") ? " (leave empty to run right away)" : ""}<span style="display:flex;gap:6px"><input class="field" id="etWhen" type="datetime-local" value="${esc(localInput(t.run_at))}"><button class="btn ghost" type="button" onclick="$('#etWhen').value=''">Clear</button></span></label>
    ${wake ? "" : `<label>Due date<span style="display:flex;gap:6px"><input class="field" id="etDue" type="date" value="${esc(String(t.due || "").slice(0, 10))}"><button class="btn ghost" type="button" onclick="$('#etDue').value=''">Clear</button></span></label>
    <label>Project<select class="field" id="etProject">${opts(S.projects || [], t.project, "No project")}</select></label>`}
    ${["done", "failed", "cancelled"].includes(t.status) ? `<p class="faint" style="margin:0;font-size:12.5px">This task is finished. "Run again" will use what you save here.</p>` : ""}
    <div class="btns"><button class="btn primary" onclick="saveTask('${esc(t.id)}',${wake ? "true" : "false"})">Save</button><button class="btn ghost" onclick="openTask('${esc(t.id)}')">Back</button>
    ${wake && ["queued", "proposed", "waiting"].includes(t.status) ? `<button class="btn" onclick="if(confirm('Cancel this reminder?'))cancelItem('${esc(t.id)}')">Cancel it</button>` : ""}</div></div>`);
  setTimeout(() => $(wake ? "#etBrief" : "#etTitle")?.focus(), 250);
}
async function saveTask(id, wake) {
  const f = { run_at: $("#etWhen").value || "" };
  if (wake) { const text = $("#etBrief").value.trim(); if (!text) return toast("Write the reminder first"); f.brief = text; f.title = text.split("\n")[0].slice(0, 60); }
  else {
    f.title = $("#etTitle").value.trim(); if (!f.title) return toast("Give it a title");
    f.due = $("#etDue").value; f.project = $("#etProject").value;
    if (!$("#etBrief").disabled) f.brief = $("#etBrief").value.trim();
    if (!$("#etWorker").disabled) f.worker = $("#etWorker").value;
  }
  try {
    const t = await api(`/api/task/${encodeURIComponent(id)}`, { action: "edit", ...f });
    toast("Saved"); await loadTasks(); reloadSchedule(); showTask(t); $("#drawer").dataset.task = t.id;
  } catch (e) { fail(e); }
}
async function cancelItem(id) {
  try { await api(`/api/task/${encodeURIComponent(id)}`, { action: "cancel" }); toast("Cancelled"); closeDrawer(); await loadTasks(); reloadSchedule(); } catch (e) { fail(e); }
}

// ---- a row of the schedule --------------------------------------------------------------------------
function openScheduled(i) {
  const x = S.schedule[i];
  if (!x) return;
  if (x.kind === "routine") openRoutine(x.name); else editTask(x.id, x.kind === "wake" ? "wake" : "task");
}

// ---- routines ---------------------------------------------------------------------------------------
const SCHEDULE_HELP = "daily 08:00 · weekdays 08:30 · weekly mon,wed 09:00 · monthly 1 09:00 · every 30m / 2h / 1d";
let previewTimer, previewSeq = 0;
function previewSchedule() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const box = $("#rtPreview"), v = $("#rtSchedule")?.value.trim();
    if (!box) return;
    if (!v) { box.innerHTML = `<span class="faint">${esc(SCHEDULE_HELP)}</span>`; return; }
    const seq = ++previewSeq;
    try {
      const r = await api("/api/schedule/preview?schedule=" + encodeURIComponent(v));
      if (seq !== previewSeq || !$("#rtPreview")) return;
      box.innerHTML = r.ok ? `${esc(r.words)}<br><span class="faint">Next: ${r.runs.map(x => esc(when(x))).join(" · ")}</span>`
        : `<span style="color:var(--err)">${esc(r.error)}</span><br><span class="faint">${esc(SCHEDULE_HELP)}</span>`;
    } catch (e) { box.textContent = ""; }
  }, 250);
}
function routineForm(r) {  // r: the routine being edited, or null for a new one
  const on = r ? r.enabled : true;
  return `${r ? `<label>Name<input class="field" value="${esc(r.name)}" disabled></label>` : `<label>Name<input class="field" id="rtName" placeholder="e.g. weekly-review" autocapitalize="none" autocorrect="off"></label>`}
    <label>Schedule<input class="field" id="rtSchedule" value="${esc(r?.schedule || "")}" placeholder="weekly sun 18:00" autocapitalize="none" autocorrect="off" oninput="previewSchedule()"></label>
    <div id="rtPreview" style="font-size:12.5px;margin-top:-6px"></div>
    <label>Worker<select class="field" id="rtWorker">${opts(["main", ...S.workers, "none"], r?.worker || "main")}</select></label>
    <p class="faint" style="margin:-6px 0 0;font-size:12px">main gets the instructions as a message; another worker gets a task; none only sends the routine's event.</p>
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px"><div><div style="font-size:14px;color:var(--text)">On</div><div class="faint" style="font-size:12px">Off keeps it but stops it running.</div></div><button class="sw${on ? " on" : ""}" id="rtOn" type="button" role="switch" aria-checked="${on}" aria-label="On" onclick="this.classList.toggle('on');this.setAttribute('aria-checked',this.classList.contains('on'))"></button></div>
    <label>Instructions<textarea class="field" id="rtBody" style="min-height:160px" placeholder="What should it do each time?">${esc(r?.body || "")}</textarea></label>`;
}
const routineFields = () => ({ schedule: $("#rtSchedule").value.trim(), worker: $("#rtWorker").value, enabled: $("#rtOn").classList.contains("on"), body: $("#rtBody").value.trim() });
async function openRoutine(name) {
  openDrawer(name, `<span class="tag">routine</span>`, `<p class="faint">Loading…</p>`);
  let r;
  try { await ensureLists(); r = await api(`/api/routine/${encodeURIComponent(name)}`); } catch (e) { $("#dBody").innerHTML = `<p class="faint">${esc(e.message)}</p>`; return; }
  $("#dBody").innerHTML = `<div class="form">${routineForm(r)}
    <div class="btns"><button class="btn primary" onclick="saveRoutine('${esc(name)}')">Save</button><button class="btn" onclick="runRoutine('${esc(name)}')">Run now</button>
    ${name === "nightly" ? "" : `<button class="btn" onclick="deleteRoutine('${esc(name)}')">Delete</button>`}</div></div>`;
  previewSchedule();
}
async function saveRoutine(name) {
  try { await api(`/api/routine/${encodeURIComponent(name)}`, { action: "save", ...routineFields() }); toast("Saved"); closeDrawer(); reloadSchedule(); } catch (e) { fail(e); }
}
async function runRoutine(name) {
  try { await api(`/api/routine/${encodeURIComponent(name)}`, { action: "run" }); toast("Started. Its next scheduled time is unchanged."); closeDrawer(); } catch (e) { fail(e); }
}
async function deleteRoutine(name) {
  if (!confirm(`Delete the routine ${name}? You can undo it from Changes.`)) return;
  try { await api(`/api/routine/${encodeURIComponent(name)}`, { action: "delete" }); toast("Deleted"); closeDrawer(); reloadSchedule(); } catch (e) { fail(e); }
}

// ---- + Add ------------------------------------------------------------------------------------------
const ADD_KINDS = [["reminder", "Reminder"], ["task", "Task"], ["routine", "Routine"]];
function addMenu(kind) {
  kind = kind || "reminder";
  ensureLists().then(() => {
    const forms = {
      reminder: `<label>Remind you to<textarea class="field" id="adText" placeholder="e.g. Call the dentist"></textarea></label>
        <label>When<input class="field" id="adWhen" type="datetime-local"></label>`,
      task: `<label>What should happen<input class="field" id="adTitle" placeholder="e.g. Find flights home"></label>
        <label>Details for the worker<textarea class="field" id="adBrief" placeholder="Anything it should know (optional)"></textarea></label>
        <label>Worker<select class="field" id="adWorker">${opts(S.workers, "main")}</select></label>
        <label>When<input class="field" id="adWhen" type="datetime-local"></label>`,
      routine: routineForm(null),
    };
    openDrawer("Add to the schedule", "", `<div class="form"><div class="filters" style="order:0;margin:0;padding:0;flex-wrap:wrap;overflow:visible">${ADD_KINDS.map(([k, l]) => `<button class="filter${k === kind ? " on" : ""}" type="button" onclick="addMenu('${k}')">${l}</button>`).join("")}</div>
      ${forms[kind]}
      <div class="btns"><button class="btn primary" onclick="addItem('${kind}')">Add ${kind}</button><button class="btn ghost" onclick="closeDrawer()">Cancel</button></div></div>`);
    if (kind === "routine") previewSchedule();
    setTimeout(() => $({ reminder: "#adText", task: "#adTitle", routine: "#rtName" }[kind])?.focus(), 250);
  });
}
async function addItem(kind) {
  let b = { kind };
  if (kind === "reminder") b = { kind, text: $("#adText").value.trim(), when: $("#adWhen").value };
  else if (kind === "task") b = { kind, title: $("#adTitle").value.trim(), brief: $("#adBrief").value.trim(), worker: $("#adWorker").value, when: $("#adWhen").value };
  else b = { kind, name: $("#rtName").value.trim(), ...routineFields() };
  try { await api("/api/schedule/add", b); toast("Added"); closeDrawer(); await loadTasks(); reloadSchedule(); } catch (e) { fail(e); }
}
