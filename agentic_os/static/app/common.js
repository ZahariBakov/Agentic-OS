// Helpers that several parts of the Jobs page use.

const FINISHED = ["done", "failed", "stopped"];  // a job in these states can be reviewed (jobs.FINISHED)

// A tag in the list: the user's last verdict on the job.
function reviewTag(job) {
  if (!job.review) return null;
  const tags = { approved: ["good", "approved"], rejected: ["bad", "rejected"], changes: ["", "changes asked"] };
  const [kind, text] = tags[job.review.verdict];
  return ui.el("span", { className: `tag ${kind}` }, text);
}

// "5 min" or "3.5 hours" since a time like "2026-11-04T10:16:37.856".
function age(at) {
  const minutes = (Date.now() - new Date(at).getTime()) / 60000;
  return minutes < 90 ? `${Math.max(0, Math.round(minutes))} min` : `${(minutes / 60).toFixed(1)} hours`;
}

function planPending(job) {
  return job.status === "waiting" && job.plan && job.plan.status === "pending";
}

// An amount with 2 decimals, but 4 below one cent (jobs.usd): haiku jobs cost less.
function money2(value) {
  if (value === null || value === undefined) return "none";
  return "$" + Number(value).toFixed(value >= 0.01 ? 2 : 4);
}

function diffView(text) {
  return ui.el("pre", { className: "diff" }, (text || "").split("\n").map(line => ui.el("div", {
    className: line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "info" : "",
  }, line || " ")));
}
