// The view of one job: what it shows, the message box, refresh, resume. (Open and close: views.js.)

const detail = document.getElementById("detail");
const detailBody = document.getElementById("detail-body");
const compose = document.getElementById("compose");
const composeWarning = document.getElementById("compose-warning");
const composeMessage = document.getElementById("compose-message");
const composeLabel = document.getElementById("compose-label");
const requestChanges = document.getElementById("request-changes");
// State of the job view that a redraw must keep (render from state):
let fileNotes = {};  // a review comment per changed file: path -> text
let openFiles = new Set();  // the changed files whose diff is open
let redrawLater = false;  // a live update came while you typed in the job view
let selected = null;  // the id of the job shown in the detail panel
let composeFor = null;  // the job that the text in the message box is for
let shown = null;  // the job shown now (its last data from the API)
let drawnKey = null;  // the data of the last drawn job view: the same data is not drawn again

detailBody.addEventListener("focusout", () => {
  if (!redrawLater) return;
  redrawLater = false;
  setTimeout(() => { if (selected) showDetail(selected); });  // after the focus has moved
});

// Draw the job view again after an action, but only if that job is still the one you look at
// (you may have opened another job while the request ran).
function refresh(id) {
  if (selected === id) showDetail(id);
}

async function showDetail(id) {
  selected = id;
  let job, files, space;
  try {
    [job, files] = await Promise.all([ui.api(`/api/jobs/${id}`), ui.api(`/api/jobs/${id}/changes`)]);
    // A job with an isolated workspace: its diff against the project (an error is shown, not thrown).
    space = job.workspace
      ? await ui.api(`/api/jobs/${id}/workspace`).catch(error => ({ error: error.message }))
      : null;
  } catch (error) {  // for example, the job file is gone (404)
    if (selected !== id) return;
    shown = null;
    drawnKey = null;
    ui.show(detailBody, ui.el("p", { className: "error" }, `Job ${id}: ${error.message}`),
      ui.el("button", { onclick: closeJob }, "Close"));
    detail.hidden = false;
    return;
  }
  // Another job was clicked (or Close) while we waited: an old reply must not draw over it.
  if (selected !== id) return;
  // Live updates come for every change of any job: draw only when this job's data changed,
  // so an open diff stays open and a long answer does not jump back to the top.
  const key = id + JSON.stringify([job, files, space]);
  if (key === drawnKey && shown && shown.id === id) {
    detail.hidden = false;
    return;
  }
  const kept = shown && shown.id === id ? keepView() : null;
  drawnKey = key;
  // A queued job has not started: the same stop cancels it (no agent, no cost).
  const queued = job.status === "queued";
  const stop = job.status === "running" || job.status === "waiting" || queued
    ? ui.el("button", { onclick: event => ui.busy(event.target, async () => {
        if (queued && !confirm("Cancel this job? It has not started, so it costs nothing.")) return;
        try {
          await ui.api(`/api/jobs/${id}/stop`, {});
        } catch (error) {
          alert((queued ? "Cancel" : "Stop") + " failed: " + error.message);
        }
        refresh(id);
        loadToday();
      }) }, queued ? "Cancel" : "Stop")
    : job.status === "interrupted"
    ? ui.el("button", { onclick: event => ui.busy(event.target, async () => { await resume(id); refresh(id); }) }, "Resume")
    : null;
  if (composeFor !== id) {  // another job: its drafts are not for this one
    compose.elements.text.value = "";
    composeMessage.textContent = "";
    fileNotes = {};
    openFiles = new Set();
    composeFor = id;
  }
  requestChanges.hidden = !FINISHED.includes(job.status);
  shown = job;
  compose.hidden = job.status === "interrupted";  // Resume first: it brings the message too
  composeWarning.hidden = !job.cold_warning;
  composeWarning.textContent = job.cold_warning || "";
  composeLabel.textContent = job.question
    ? "Your own answer (or click an option above)"
    : planPending(job)
    ? "Changes to the plan (Send approves the plan with these changes)"
    : "Write to the agent (a follow-up: it goes to the same session)";
  ui.show(detailBody,
    ui.el("div", { className: "buttons", style: "justify-content: space-between" },
      ui.el("h2", {}, `Job ${job.id} `, ui.badge(job.status)),
      ui.el("div", { className: "buttons" }, stop,
        ui.el("button", { onclick: closeJob }, "Close"))),
    ui.el("div", { className: "muted" },
      `${job.lead ? "Lead " + job.lead + (job.desk && job.desk !== job.lead ? ` (twin desk ${job.desk})` : "")
        + " | " : ""}${job.model} | ${ui.money(job.cost)}`
      + (job.folder ? ` | folder ${job.folder}` : "")),
    job.status === "queued" && job.waits_for
      ? ui.el("p", { className: "muted" }, `Waits for a free desk of ${job.waits_for}: it starts by itself `
          + "when one of its jobs ends.")
      : null,
    job.parent ? ui.el("div", { className: "muted" }, `Sub-job "${job.title}" of `,
      ui.el("a", { href: "#", onclick: event => { event.preventDefault(); openJob(job.parent); } }, "job " + job.parent),
      ` | ${job.model}: ${job.model_why} | budget ${ui.money(job.max_budget_usd)} | reads only`) : null,
    ui.el("pre", {}, job.task),
    subJobsView(job),
    job.status === "interrupted"
      ? ui.el("p", { className: "error" }, `Interrupted: ${job.interrupted_why}. `
          + "Resume continues the same session; the agent gets what already happened.")
      : null,
    job.held_for
      ? ui.el("p", { className: "muted" }, `Held: lead ${job.lead} waits for your answer in job ${job.held_for}. `,
          ui.el("button", { onclick: () => openJob(job.held_for) }, "Open that job"))
      : null,
    job.error ? ui.el("p", { className: "error" }, "Error: " + job.error) : null,
    conversationView(job),
    job.result ? [ui.el("h2", { style: "margin-top: 16px" }, job.messages.length ? "Last answer" : "Result"),
                  ui.el("pre", {}, job.result)] : null,
    wildcardsView(job),
    questionView(job),
    commandsView(job),
    planView(job),
    reviewView(job, space),
    job.check_output ? [ui.el("h2", { style: "margin-top: 16px" }, `Check: ${job.check}`),
                        ui.el("pre", {}, job.check_output)] : null,
    criticView(job),
    workspaceView(job, space),
    changesView(job, files),
    job.notes.length ? ui.el("p", {}, "Saved notes: " + job.notes.join(", ")) : null,
    job.project_notes && job.project_notes.length
      ? ui.el("p", {}, "Remembered for the project: " + job.project_notes.join(" | ")) : null,
  );
  if (kept) kept();
  detail.hidden = false;
}

// Remember what you opened and how far you scrolled in the job view. Return a function
// that puts it back after a redraw (the same job, with new data).
function keepView() {
  const open = new Set([...detailBody.querySelectorAll("details[open] > summary")].map(summary => summary.textContent));
  const scrolls = [...detailBody.querySelectorAll("pre")].map(pre => pre.scrollTop);
  return () => {
    detailBody.querySelectorAll("details > summary").forEach(summary => {
      if (open.has(summary.textContent)) summary.parentNode.open = true;
    });
    detailBody.querySelectorAll("pre").forEach((pre, index) => { pre.scrollTop = scrolls[index] || 0; });
  };
}

// What the page says about a message in the inbox (not read by the agent yet).
// Only a running job has a "current call"; after a run that ended before the agent took
// the inbox (a failed start, a stop), the message waits for the job's next run.
function inboxNote(status) {
  if (status === "running") return "waiting: the agent reads it after its current call";
  if (status === "queued") return "waiting: the agent gets it when the job starts";
  return "not read: the last run ended before the agent got it; it goes with the next run";
}

// The conversation after the task: older answers and your messages, oldest first.
function conversationView(job) {
  const read = job.messages.map(message => ({ ...message, waiting: false }));
  const waiting = job.inbox.map(message => ({ ...message, waiting: true }));
  const all = [...read, ...waiting];
  if (all.length === 0) return null;
  return [
    ui.el("h2", { style: "margin-top: 16px" }, "Conversation"),
    all.map(message => ui.el("div", { className: `message ${message.from}` },
      ui.el("div", { className: "muted" },
        `${message.from === "user" ? "You" : "Agent"} | ${ui.time(message.at)}`,
        message.waiting ? ` | ${inboxNote(job.status)}` : ""),
      ui.el("pre", {}, message.note || message.text),  // note: your words; text: the full prompt
      message.options && message.options.length
        ? ui.el("div", { className: "muted" }, "Options: " + message.options.join(" | ")) : null)),
  ];
}

// The job that the panel shows, or null while another job loads (an action then waits).
function shownId() {
  return shown && shown.id === selected ? shown.id : null;
}

// Send a message (a follow-up, or an answer to the agent's question) to the selected job.
// Return true if it was sent.
async function sendMessage(text) {
  const id = shownId();
  if (!text.trim() || !id) return false;
  if (shown.cold_warning && !confirm(shown.cold_warning + "\n\nSend anyway?")) return false;
  try {
    const reply = await ui.api(`/api/jobs/${id}/messages`, { text });
    composeMessage.className = "muted";
    composeMessage.textContent = reply.started
      ? "Sent. The job runs again in the same session."
      : "Sent. The agent reads it after its current call.";
    refresh(id);
    return true;
  } catch (error) {
    composeMessage.className = "error";
    composeMessage.textContent = "Not sent: " + error.message;
    return false;
  }
}

compose.addEventListener("submit", event => {
  event.preventDefault();
  // Ctrl+Enter has no submitter: then the form's Send button is the one to disable.
  const button = event.submitter || compose.querySelector("button:not([type=button])");
  const id = composeFor;
  ui.busy(button, async () => {
    // Clear the draft only if it still belongs to that job (you may have opened another meanwhile).
    if (await sendMessage(compose.elements.text.value) && composeFor === id) compose.elements.text.value = "";
  });
});
compose.elements.text.addEventListener("keydown", event => {
  if (event.key === "Enter" && event.ctrlKey) compose.requestSubmit();
});

// Resume an interrupted job. It costs money, so the browser asks first.
async function resume(id) {
  if (!confirm("Resume this job? The agent continues in the same session (this costs money).")) return;
  try {
    await ui.api(`/api/jobs/${id}/resume`, {});
  } catch (error) {
    alert("Resume failed: " + error.message);
  }
}
