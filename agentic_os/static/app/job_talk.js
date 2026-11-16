// Parts of the job view about talking with the agent: plan, question, commands, sub-jobs.
// (Like agentic_os/job_talk.py.)

// The agent's plan. While it waits: the steps, the estimate (a guess, next to what
// earlier jobs really cost) and Approve / Reject; changes go in the message box.
// After the decision: what was decided, and the estimate next to the real cost.
function planView(job) {
  if (!job.plan) return null;
  const plan = job.plan;
  if (plan.status === "pending" && job.status !== "waiting") {  // stopped before your decision
    return ui.el("p", { className: "muted" },
      `Plan not decided: the job ${job.status === "stopped" ? "was stopped" : "is " + job.status} | estimate ${money2(plan.estimate)} (a guess)`);
  }
  if (plan.status !== "pending") {
    const decided = { approved: "approved", changed: "approved with changes", rejected: "rejected" }[plan.status];
    const real = job.status === "done" ? ` | real cost ${ui.money(job.cost)}` : "";
    return ui.el("p", { className: "muted" },
      `Plan ${plan.auto || decided} | estimate ${money2(plan.estimate)} (a guess)${real}`);
  }
  return ui.el("div", { className: "question" },
    ui.el("h2", {}, "The agent proposes a plan"),
    ui.el("div", { className: "muted" }, "Proposed " + ui.time(plan.at) + " | waits " + age(plan.at)),
    ui.el("pre", {}, plan.text),
    ui.el("p", {}, ui.el("strong", {}, `Estimate: ${money2(plan.estimate)}`),
      ui.el("span", { className: "muted" }, " (the agent's guess, not a limit)")),
    job.cost_history ? ui.el("p", { className: "muted" }, job.cost_history) : null,
    ui.el("p", { className: "muted" }, "Nothing changed yet: the agent could only read."),
    ui.el("div", { className: "buttons" },
      ui.el("button", { className: "primary", onclick: event => {
        event.target.disabled = true;  // one click: a double click would decide twice
        decidePlan("approve", "");
      } }, "Approve"),
      ui.el("button", { onclick: () => {
        const why = prompt("Reject the plan? The job stops, with no agent call. Why (optional):");
        if (why !== null) decidePlan("reject", why);
      } }, "Reject"),
      ui.el("span", { className: "muted" }, "To change it: write below and press Send."),
    ),
  );
}

async function decidePlan(decision, note) {
  const id = shownId();
  if (!id) return;
  try {
    const reply = await ui.api(`/api/jobs/${id}/plan`, { decision, note });
    composeMessage.className = "muted";
    composeMessage.textContent = reply.started ? "Plan approved. The work starts in the same session." : "Plan rejected. The job stopped.";
  } catch (error) {
    composeMessage.className = "error";
    composeMessage.textContent = "Not sent: " + error.message;
  }
  refresh(id);
}

// The sub-jobs this job delegated (specialists.py): they only read, and report back.
function subJobsView(job) {
  if (!job.sub_jobs || !job.sub_jobs.length) return null;
  const total = job.sub_jobs.reduce((sum, sub) => sum + sub.cost, 0);
  return [ui.el("h2", { style: "margin-top: 16px" }, `Sub-jobs (${ui.money(total)} of $0.50)`),
    job.sub_jobs.map(sub => ui.el("div", { className: "buttons", style: "justify-content: space-between" },
      ui.el("span", {}, ui.badge(sub.status), " ", sub.title, " ",
        ui.el("span", { className: "muted" }, `${sub.model} | ${ui.money(sub.cost)}`)),
      ui.el("button", { onclick: () => openJob(sub.id) }, "Open")))];
}

// Shell commands the agent asked to run (commands.py). A waiting one has Allow / Always allow /
// Deny; the agent waits for the answer (10 minutes at most). Python runs it, not the agent.
const COMMAND_STATUS = { pending: "waits for you", allowed: "allowed", running: "running", denied: "denied",
                         "no answer": "no answer in time", stopped: "the job was stopped" };

async function decideCommand(id, decision, buttons) {
  buttons.forEach(button => { button.disabled = true; });  // one answer only
  try {
    await ui.api(`/api/commands/${id}`, { decision });
  } catch (error) {
    alert("Not saved: " + error.message);
    buttons.forEach(button => { button.disabled = false; });
  }
  loadToday();
  if (shownId()) refresh(shownId());
}

function commandButtons(record) {
  const buttons = [];
  const add = (label, decision, title) => buttons.push(ui.el("button", {
    className: decision === "allow" ? "primary" : "", title, onclick: () => decideCommand(record.id, decision, buttons) }, label));
  add("Allow", "allow", "Run it once");
  add("Always allow", "always", "Run it, and later commands that start the same way, without asking (simple commands only)");
  add("Deny", "deny", "Do not run it; the agent is told");
  return buttons;
}

function commandStatus(record) {
  if (record.status !== "done") return COMMAND_STATUS[record.status] || record.status;
  return `exit code ${record.exit_code} | ${record.seconds} s` + (record.allowed_by ? " | on your list" : "");
}

function commandsView(job) {
  if (!job.commands || !job.commands.length) return null;
  return [ui.el("h2", { style: "margin-top: 16px" }, "Commands"),
    job.commands.map(record => record.status === "pending"
      ? ui.el("div", { className: "question" },
          ui.el("h2", {}, "The agent wants to run a command"),
          ui.el("div", { className: "muted" }, `In ${record.cwd} | waits ${age(record.at)}`),
          ui.el("pre", {}, record.command),
          ui.el("p", {}, "Why: " + record.reason),
          ui.el("div", { className: "buttons" }, commandButtons(record)))
      : ui.el("details", {},
          ui.el("summary", {}, ui.el("code", {}, record.command), " ", ui.el("span", { className: "muted" }, commandStatus(record))),
          ui.el("p", { className: "muted" }, "Why: " + record.reason),
          record.output !== null ? ui.el("pre", {}, record.output || "(no output)") : null))];
}

// The agent's open question: one button per option answers it with one click.
function questionView(job) {
  if (job.status !== "waiting" || !job.question) return null;
  return ui.el("div", { className: "question" },
    ui.el("h2", {}, "The agent asks you"),
    ui.el("div", { className: "muted" }, "Asked " + ui.time(job.question.at) + " | waits " + age(job.question.at)),
    ui.el("pre", {}, job.question.text),
    job.question.suggested
      ? ui.el("div", { className: "muted" }, "Suggested answers (Python made them from the question's words):")
      : null,
    ui.el("div", { className: "buttons" },
      job.question.options.map(option =>
        ui.el("button", { className: "primary", onclick: event => {
          // One answer only: a double click would send it twice.
          const buttons = event.target.parentNode.querySelectorAll("button");
          buttons.forEach(button => { button.disabled = true; });
          sendMessage(option).then(sent => {  // not sent (cancelled, or an error): you can click again
            if (!sent) buttons.forEach(button => { button.disabled = false; });
          });
        } }, option))),
  );
}
