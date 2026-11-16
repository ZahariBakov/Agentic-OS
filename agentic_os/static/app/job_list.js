// The Jobs list (live) and the Run queued jobs button.
// The list is narrow (the selected job is next to it), so a row has three columns: the status,
// the task with a small line under it (lead, model, time), and the cost. One click opens a job.

const jobsBox = document.getElementById("jobs");
const runMessage = document.getElementById("run-message");

const columns = [
  { title: "Status", value: job => ui.badge(job.status) },
  { title: "Task", value: job => [
    job.question && job.status === "waiting"
      ? ui.el("span", { className: "tag needs-you" }, "question | waits " + age(job.question.at)) : null,
    planPending(job) ? ui.el("span", { className: "tag needs-you" }, "plan | waits " + age(job.plan.at)) : null,
    job.held_for ? ui.el("span", { className: "tag" }, "held") : null,
    reviewTag(job),
    job.routine ? ui.el("span", { className: "tag" }, "routine " + job.routine) : null,
    job.skill ? ui.el("span", { className: "tag" }, job.skill) : null,
    job.task.length > 80 ? job.task.slice(0, 77) + "..." : job.task,
    ui.el("div", { className: "job-meta" },
      (job.lead ? job.lead + " | " : "") + job.model + (job.effort ? ` (${job.effort})` : "")
      + (job.check || job.reference ? ` | tries ${job.tries}` : "") + " | " + ui.time(job.created_at)),
  ] },
  { title: "Cost", value: job => ui.money(job.cost), num: true },
];

function showJobs(jobs) {
  ui.show(jobsBox, ui.table(columns, jobs, job => openJob(job.id)));
  // The rows are in the order of the jobs: give each row its job id, for markSelected.
  jobsBox.querySelectorAll("tbody tr").forEach((row, index) => {
    if (jobs[index]) row.dataset.job = jobs[index].id;
  });
  markSelected();
  if (!selected) return;
  // A redraw would remove the box you type in (and its focus): wait until you leave it.
  if (detailBody.contains(document.activeElement) && document.activeElement.matches("textarea, input")) {
    redrawLater = true;
  } else {
    showDetail(selected);  // the selected job may have changed
  }
}

// The row of the job that is open looks selected.
function markSelected() {
  jobsBox.querySelectorAll("tbody tr").forEach(row => {
    row.classList.toggle("selected", row.dataset.job === selected);
  });
}

const runQueued = document.getElementById("run-queued");
runQueued.onclick = () => ui.busy(runQueued, async () => {
  try {
    const result = await ui.api("/api/run", {});
    runMessage.className = "muted";
    runMessage.textContent = `Started ${result.started} queued jobs.`;
  } catch (error) {
    runMessage.className = "error";
    runMessage.textContent = "Not started: " + error.message;
  }
});
