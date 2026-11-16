// The Skills panel, and the proposals from calibrate.

// Skills: one row per skill, with its own input, model, effort and a Run button.
function select(options, chosen) {
  return ui.el("select", {}, options.map(([value, text]) =>
    ui.el("option", { value, selected: value === chosen }, text)));
}

const skillMessage = document.getElementById("skill-message");

function skillRow(skill) {
  const input = ui.el("input", { placeholder: skill.input });
  const model = select(["haiku", "sonnet", "opus"].map(m => [m, m]), skill.model);
  const effort = select([["", "effort -"], ...["low", "medium", "high", "xhigh"].map(e => [e, e])], skill.effort || "");
  const button = ui.el("button", { className: "primary" }, "Run");
  button.onclick = () => ui.busy(button, async () => {
    try {
      const job = await ui.api(`/api/skills/${skill.name}/run`,
        { input: input.value, model: model.value, effort: effort.value || null });
      skillMessage.className = "muted";
      skillMessage.textContent = `Started skill ${skill.name} as job ${job.id} (${job.model}).`;
      input.value = "";
    } catch (error) {
      skillMessage.className = "error";
      skillMessage.textContent = error.message;
    }
  });
  input.onkeydown = event => { if (event.key === "Enter") button.click(); };
  // Feedback: Python adds one line to skills/feedback/<name>.md (no agent, no cost).
  const feedbackButton = ui.el("button", { title: "Park feedback for this skill" }, "Feedback");
  feedbackButton.onclick = async () => {
    const text = prompt(`Feedback for the skill ${skill.name}:`);
    if (!text) return;
    try {
      const result = await ui.api(`/api/skills/${skill.name}/feedback`, { text });
      skillMessage.className = "muted";
      skillMessage.textContent = `Saved. ${skill.name} has ${result.open} open feedback. Run calibrate with "${skill.name}" to use it.`;
      loadSkills();
    } catch (error) {
      skillMessage.className = "error";
      skillMessage.textContent = error.message;
    }
  };
  return ui.el("div", { className: "skill" },
    ui.el("div", { className: "about" }, ui.el("strong", {}, skill.name),
      ui.el("small", {}, `${skill.runs} ${skill.runs === 1 ? "run" : "runs"} | ${ui.money(skill.cost)}`
        + (skill.feedback ? ` | ${skill.feedback} feedback` : "")
        + (skill.wildcards ? ` | wildcards ${skill.wildcards} of ${skill.items}` : ""))),
    ui.el("div", { className: "about" }, input, ui.el("small", {}, skill.description)),
    model, effort, button, feedbackButton);
}

function loadSkills() {
  ui.api("/api/skills").then(skills => {
    ui.show(document.getElementById("skills"),
      skills.length ? skills.map(skillRow) : ui.el("p", { className: "muted" }, "No skills yet. Add a file to skills/."));
  }).catch(error => {
    ui.show(document.getElementById("skills"), ui.el("p", { className: "error" }, "Could not load the skills: " + error.message));
  });
}

// Proposals from calibrate: show the diff, and let the user decide.
function loadProposals() {
  ui.api("/api/proposals").then(items => {
    const panel = document.getElementById("proposals");
    panel.hidden = items.length === 0;
    ui.show(panel, ui.el("h2", {}, "Proposals from calibrate"), items.map(item => {
      const decide = action => event => ui.busy(event.target, async () => {
        try {
          await ui.api(`/api/proposals/${item.name}/${action}`, {});
        } catch (error) {
          alert(`${action} failed: ${error.message}`);
        }
        loadProposals();
        loadSkills();
      });
      return ui.el("div", {},
        ui.el("div", { className: "buttons", style: "justify-content: space-between" },
          ui.el("strong", {}, `New version of the skill ${item.name}`),
          ui.el("div", { className: "buttons" },
            ui.el("button", { className: "primary", onclick: decide("accept") }, "Accept"),
            ui.el("button", { onclick: decide("reject") }, "Reject"))),
        item.diff ? diffView(item.diff)
          : ui.el("p", { className: "error" }, "No diff: the skill file is gone. Reject removes the proposal."));
    }));
  });
}
