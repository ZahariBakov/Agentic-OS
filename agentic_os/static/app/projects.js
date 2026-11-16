// The Projects panel: project memory and the Continue button.

// Project memory (project_memory.py). The form is not redrawn (it would lose what you type);
// only the list is. openProjects keeps the open ones open after a redraw.
const PROJECT_KINDS = { instruction: "instruction", decision: "decision", failure: "known failure", outcome: "past job" };
const projectForm = document.getElementById("project-form");
const projectMessage = document.getElementById("project-message");
const openProjects = new Set();

function loadProjects() {
  ui.api("/api/projects").then(projects => {
    const folders = document.getElementById("project-folders");
    ui.show(folders, projects.map(project => ui.el("option", { value: project.project })));
    const remove = (project, entry) => event => {
      if (!confirm(`Remove this from ${project.name}?\n\n${entry.text}`)) return;
      ui.busy(event.target, async () => {
        try {
          await ui.api(`/api/projects/${project.file}/entries/${entry.id}/remove`, {});
        } catch (error) {
          alert("Not removed: " + error.message);
        }
        loadProjects();
      });
    };
    ui.show(document.getElementById("project-list"),
      projects.length ? null : ui.el("p", { className: "muted" }, "No project memory yet."),
      // Continue sits next to the project, outside <details>: it shows while the project is closed.
      projects.map(project => ui.el("div", { className: "verdict buttons", style: "align-items: flex-start" },
        ui.el("details", {
          style: "flex: 1", open: openProjects.has(project.file),
          ontoggle: event => { event.target.open ? openProjects.add(project.file) : openProjects.delete(project.file); },
        },
        ui.el("summary", {}, ui.el("strong", {}, project.name), " ",
          ui.el("span", { className: "muted" }, project.project + " | " + Object.entries(project.counts)
            .filter(([, count]) => count).map(([kind, count]) => plural(count, PROJECT_KINDS[kind])).join(", "))),
        ui.el("p", {}, project.latest_outcome
          ? ["Last job: ", project.latest_outcome.text, " ",
             ui.el("span", { className: "muted" }, ui.time(project.latest_outcome.at))]
          : ui.el("span", { className: "muted" }, "No finished job yet.")),
        project.entries.map(entry => ui.el("div", { className: "buttons", style: "justify-content: space-between" },
          ui.el("span", {}, ui.el("span", { className: "tag" }, PROJECT_KINDS[entry.kind]), " ", entry.text, " ",
            ui.el("span", { className: "muted" }, `${entry.by}${entry.job ? " | job " + entry.job : ""} | ${ui.time(entry.at)}`)),
          ui.el("div", { className: "buttons" },
            entry.job ? ui.el("button", { onclick: () => openJob(entry.job) }, "Open job") : null,
            ui.el("button", { onclick: remove(project, entry) }, "Remove")))),
        ),
        ui.el("button", { className: "primary", onclick: () => continueProject(project),
                          title: "Fill the New job form for this project (nothing starts yet)" }, "Continue"),
      )));
  }).catch(error => console.error("projects:", error));
}

// "1 past job", "2 past jobs": every kind name takes an "s".
function plural(count, name) {
  return `${count} ${name}${count === 1 ? "" : "s"}`;
}

// Continue: fill the New job form with the project's folder and where it stands. Nothing
// starts and nothing costs money until you finish the task and press Start job.
function continueProject(project) {
  const last = project.latest_outcome ? ` The last job: ${project.latest_outcome.text}` : "";
  form.folder.value = project.project;
  form.check.value = project.last_check || "";  // the check of the project's newest done job
  // project_memory.task_words and web.api_add_job know this text: change them together.
  form.task.value = `Continue the work on ${project.name}.${last}\n\nNext: `;
  goTo("new");  // first: a hidden field cannot take the focus
  form.task.focus();
  form.task.setSelectionRange(form.task.value.length, form.task.value.length);
}

projectForm.onsubmit = event => {
  event.preventDefault();
  const button = projectForm.querySelector("button[type=submit]");
  ui.busy(button, async () => {
    try {
      const entry = await ui.api("/api/projects/entries", {
        folder: projectForm.folder.value, kind: projectForm.kind.value, text: projectForm.text.value });
      projectForm.text.value = "";
      projectMessage.className = "muted";
      projectMessage.textContent = `Remembered (${PROJECT_KINDS[entry.kind]}).`;
    } catch (error) {
      projectMessage.className = "error";
      projectMessage.textContent = "Not saved: " + error.message;
    }
    loadProjects();
  });
};
