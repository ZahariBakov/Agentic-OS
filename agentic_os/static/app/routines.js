// The Routines panel.

// Routines: the schedule, the next run, the cost against the weekly cap, and "Run now".
const routineMessage = document.getElementById("routine-message");
const runningRoutines = new Set();  // "Run now" requests that have not answered yet

async function runRoutine(routine, button) {
  // A double click (or a click on the button that a redraw made) would start a routine twice
  if (button.disabled || runningRoutines.has(routine.name)) return;
  button.disabled = true;
  runningRoutines.add(routine.name);
  try {
    const result = await ui.api(`/api/routines/${routine.name}/run`, {});
    const ids = result.jobs.map(job => job.id).join(", ");
    routineMessage.className = "muted";
    routineMessage.textContent = result.jobs.length === 0
      ? `Routine ${routine.name}: nothing to do.`
      : routine.mode === "active"
        ? `Started routine ${routine.name}: job ${ids}.`
        : `Queued job ${ids} for ${routine.name} (passive). Press "Run queued jobs" to start it.`;
  } catch (error) {
    routineMessage.className = "error";
    routineMessage.textContent = error.message;
  }
  runningRoutines.delete(routine.name);
  loadRoutines();  // draws new buttons
}

const routineColumns = [
  { title: "Routine", value: r => ui.el("div", { className: "about" },
      ui.el("strong", {}, r.name), ui.el("small", {}, r.description)) },
  { title: "Schedule", value: r => r.schedule },
  { title: "Mode", value: r => ui.el("span", { className: "tag" }, r.mode) },
  { title: "Does", value: r => r.what },
  { title: "Next run", value: r => ui.time(r.next_run) },
  { title: "7 days / cap", value: r => `${ui.money(r.week_cost)} / ${ui.money(r.max_week_usd)}`, num: true },
  { title: "", value: r => ui.el("button", { disabled: runningRoutines.has(r.name),
      onclick: event => runRoutine(r, event.target) }, "Run now") },
];

function loadRoutines() {
  ui.api("/api/routines").then(rows => {
    ui.show(document.getElementById("routines"),
      rows.length ? ui.table(routineColumns, rows)
                  : ui.el("p", { className: "muted" }, "No routines yet. Add a file to routines/."));
  }).catch(error => {
    ui.show(document.getElementById("routines"), ui.el("p", { className: "error" }, "Could not load the routines: " + error.message));
  });
}
