// The Today panel: what needs you, who works now, what finished in the last 24 hours.

// Today: what needs you, who works now, what finished in the last 24 hours.
// Python collects the facts (GET /api/today); this only shows them. The panel is
// drawn again only when the facts change, so it does not flicker on live updates.
const todayCards = document.getElementById("today-cards");
let todayKey = null;

// One click answers a question from here, like the buttons in the job view.
async function answerFromToday(item, option, buttons) {
  // A cold session reloads its whole context at full price: ask first, like the job view does.
  if (item.cold_warning && !confirm(item.cold_warning + "\n\nSend anyway?")) return;
  const id = item.job;
  buttons.forEach(button => { button.disabled = true; });  // one answer only
  try {
    await ui.api(`/api/jobs/${id}/messages`, { text: option });
  } catch (error) {
    alert("Not sent: " + error.message);
  }
  loadToday();
}

function needItem(item) {
  const who = item.lead ? item.lead + ": " : "";
  const open = ui.el("button", { onclick: () => openJob(item.job) }, "Open");
  if (item.kind === "question") {
    const buttons = item.options.map(option => ui.el("button", { className: "primary",
      onclick: () => answerFromToday(item, option, buttons) }, option));
    return ui.el("div", { className: "today-item" },
      ui.el("div", {}, ui.el("span", { className: "tag needs-you" }, "question | waits " + age(item.at)), who + item.text),
      ui.el("div", { className: "buttons" }, buttons, open));
  }
  if (item.kind === "command") {
    return ui.el("div", { className: "today-item" },
      ui.el("div", {}, ui.el("span", { className: "tag needs-you" }, "command | waits " + age(item.at)),
        who, ui.el("code", {}, item.command), ui.el("span", { className: "muted" }, " | " + item.reason)),
      ui.el("div", { className: "buttons" }, commandButtons(item), open));
  }
  if (item.kind === "plan") {
    return ui.el("div", { className: "today-item" },
      ui.el("div", {}, ui.el("span", { className: "tag needs-you" }, `plan | ${money2(item.estimate)} (a guess) | waits ${age(item.at)}`),
        who + item.task),
      ui.el("div", { className: "buttons" }, open));
  }
  if (item.kind === "workspace") {
    return ui.el("div", { className: "today-item" },
      ui.el("div", {}, ui.el("span", { className: "tag needs-you" }, "workspace | " + age(item.at)),
        who + "changes wait: apply or discard | " + item.task),
      ui.el("div", { className: "buttons" }, open));
  }
  if (item.kind === "interrupted") {
    return ui.el("div", { className: "today-item" },
      ui.el("div", {}, ui.el("span", { className: "badge interrupted" }, "interrupted"), " ", who + item.task),
      ui.el("div", { className: "buttons" },
        ui.el("button", { className: "primary", onclick: event => ui.busy(event.target, async () => {
          await resume(item.job);
          loadToday();
        }) }, "Resume"), open));
  }
  const [panel, text] = {
    lessons: ["lessons", `${item.count} lesson draft(s) from the leads`],
    proposals: ["proposals", `${item.count} skill proposal(s) from calibrate`],
    recos: ["recos", `${item.count} Recos card(s): routines or lessons to set up`],
  }[item.kind];
  return ui.el("div", { className: "today-item" }, ui.el("div", {}, text),
    ui.el("button", { onclick: () => goTo(panel) }, "Show"));  // the panel's view has the same name
}

function showToday(facts) {
  // The minute is in the key too, so "waits 5 min" grows on the next live update.
  const key = JSON.stringify({ ...facts, now: null, minute: Math.floor(Date.now() / 60000) });
  if (key === todayKey) return;
  todayKey = key;
  const hour = new Date().getHours();
  document.getElementById("today-hint").textContent =
    `${hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening"} | `
    + new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const needs = facts.needs_you;
  ui.show(todayCards,
    ui.el("div", { className: "today-card" },
      ui.el("h3", {}, "Needs you", needs.length ? ui.el("span", { className: "tag needs-you" }, String(needs.length)) : null),
      needs.length ? needs.map(needItem) : ui.el("p", { className: "muted" }, "Nothing waits for you.")),
    ui.el("div", { className: "today-card" },
      ui.el("h3", {}, "Working now"),
      facts.working.length
        ? facts.working.map(job => ui.el("div", { className: "today-item click", onclick: () => openJob(job.job) },
            ui.el("div", {}, ui.badge("running"), " ", (job.lead ? job.lead + " | " : "") + job.model + " | " + job.task),
            ui.el("span", { className: "muted" }, ui.money(job.cost))))
        : ui.el("p", { className: "muted" }, "Nobody works now."),
      facts.queued ? ui.el("p", { className: "muted" }, `${facts.queued} job(s) wait in the queue.`) : null),
    ui.el("div", { className: "today-card" },
      ui.el("h3", {}, "Finished in the last 24 hours"),
      ui.el("p", { className: "muted" }, `${facts.finished_count} job(s), together ${ui.money(facts.finished_cost)}`
        + (facts.not_reviewed ? ` | ${facts.not_reviewed} not reviewed` : "")),
      facts.finished.length
        ? facts.finished.map(job => ui.el("div", { className: "today-item click", onclick: () => openJob(job.job) },
            ui.el("div", {}, ui.badge(job.status), " ",
              job.lesson ? ui.el("span", { className: "tag" }, "lesson") : null,
              job.review ? reviewTag({ review: { verdict: job.review } })
                : job.status === "done" && !job.lesson ? ui.el("span", { className: "tag" }, "not reviewed") : null,
              job.task,
              job.preview ? ui.el("div", { className: "muted today-preview" }, job.preview) : null),
            ui.el("span", { className: "muted" }, ui.money(job.cost))))
        : ui.el("p", { className: "muted" }, "Nothing finished today.")),
  );
}

function loadToday() {
  ui.api("/api/today").then(facts => {
    showToday(facts);
    showCounts(facts);  // the numbers in the sidebar
  }).catch(error => console.error("today:", error));
}
