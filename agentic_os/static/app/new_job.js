// The New job form, the settings form under it, and the lead list.

// The leads for the select box.
function loadLeads() {
  ui.api("/api/leads").then(leads => {
    const select = document.getElementById("lead-select");
    for (const lead of leads) select.append(ui.el("option", { value: lead.name }, `${lead.name} (${lead.area})`));
  });
}

// The auto-approve amount (data/settings.json): plans up to it go on without your OK.
const planSettings = document.getElementById("plan-settings");
const planSettingsMessage = document.getElementById("plan-settings-message");
function loadSettings() {
  ui.api("/api/settings").then(values => {
    planSettings.usd.value = values.auto_approve_plans_usd;
    planSettings.isolate.value = values.isolate_jobs;
    planSettings.twins.value = String(values.lead_twins);
    planSettings.commands.value = values.commands_mode;
    planSettings.allow.value = values.commands_allow.join("\n");
  });
}
planSettings.onsubmit = async event => {
  event.preventDefault();
  try {
    const values = await ui.api("/api/settings", { auto_approve_plans_usd: Number(planSettings.usd.value || 0),
                                                   isolate_jobs: planSettings.isolate.value,
                                                   lead_twins: Number(planSettings.twins.value),
                                                   commands_mode: planSettings.commands.value,
                                                   commands_allow: planSettings.allow.value.split("\n") });
    planSettingsMessage.className = "muted";
    planSettingsMessage.textContent = values.auto_approve_plans_usd > 0 ? "Saved." : "Saved: every plan asks you.";
  } catch (error) {
    planSettingsMessage.className = "error";
    planSettingsMessage.textContent = "Not saved: " + error.message;
  }
};

// The form: send the new job as JSON.
const form = document.getElementById("new-job");
const message = document.getElementById("form-message");
form.onsubmit = event => {
  event.preventDefault();  // do not reload the page
  // The POST can take seconds (the router may ask Haiku): a second click must not add a second job.
  const button = event.submitter || form.querySelector("button:not([type=button])");
  ui.busy(button, () => addJob());
};

async function addJob() {
  const data = Object.fromEntries(new FormData(form));
  data.memory = form.memory.checked;
  data.plan = form.plan.checked;
  try {
    const job = await ui.api("/api/jobs", data);
    message.className = "muted";
    message.textContent = `Started job ${job.id} (${job.lead ? "lead " + job.lead + ", " : ""}${job.model}).`;
    form.task.value = "";
    form.reference.value = "";
    form.plan.checked = false;
    document.getElementById("reference-box").open = false;
  } catch (error) {
    message.className = "error";
    message.textContent = error.message;
  }
}
