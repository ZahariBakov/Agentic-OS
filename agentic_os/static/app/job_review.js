// Parts of the job view about reviewing a result: wildcards, critic, workspace, changes,
// review, approve. (Like agentic_os/job_review.py.)

// The critic's rounds (job.verdicts), oldest first, and the reference itself.
// Wildcards: Python chose which items break the pattern of the skill's references,
// and checked the marks. A wildcard you like goes into the references (Keep).
function wildcardsView(job) {
  const wild = job.wildcards;
  if (!wild || !job.wildcard_items || !job.wildcard_items.length) return null;
  const keep = number => async () => {
    try {
      await ui.api(`/api/jobs/${job.id}/wildcards/${number}/keep`, {});
    } catch (error) {
      alert("Not kept: " + error.message);
    }
    refresh(job.id);
  };
  return [
    ui.el("h2", { style: "margin-top: 16px" }, `Wildcards: items ${wild.places.join(" and ")} of ${wild.count}`),
    ui.el("p", { className: "muted" }, "A wildcard breaks a pattern of the references on purpose. "
      + "Keep the good ones: they go into the skill's references, so the next answers learn from them."),
    wild.problems && wild.problems.length ? ui.el("p", { className: "error" }, "Python's check: " + wild.problems.join(" ")) : null,
    ui.el("ol", {}, job.wildcard_items.filter(item => item.wildcard).map(item => ui.el("li", { value: item.number },
      item.text, " ",
      wild.kept.includes(item.number)
        ? ui.el("span", { className: "tag good" }, "kept")
        : job.status === "done" ? ui.el("button", { onclick: keep(item.number) }, "Keep") : null))),
  ];
}

function criticView(job) {
  if (!job.reference) return null;
  const rounds = job.verdicts.map((verdict, i) => {
    const won = verdict.winner === "result";
    const failed = !verdict.winner;  // the critic failed: no winner (the job keeps its result)
    return ui.el("div", { className: "verdict" },
      ui.el("strong", {}, `Round ${i + 1} `),
      ui.el("span", { className: `badge ${won ? "done" : "failed"}` },
        failed ? "no verdict" : `the ${verdict.winner} is better`),
      ui.el("span", { className: "muted" }, ` | ${ui.money(verdict.cost)}`),
      ui.el("p", {}, verdict.why),
      // When the result wins, the critic's fix is about the reference, so we do not show it.
      won || failed ? null : ui.el("p", {}, ui.el("strong", {}, "Fix: "), verdict.fix));
  });
  const waiting = job.status === "queued" || job.status === "running"
    ? "The critic compares when the result is ready." : "No verdict (the critic did not run).";
  return [
    ui.el("h2", { style: "margin-top: 16px" }, "Critic"),
    rounds.length ? rounds : ui.el("p", { className: "muted" }, waiting),
    ui.el("details", {}, ui.el("summary", {}, "Reference"),
      ui.el("pre", {}, job.reference_text ?? "(the reference file is missing)")),
  ];
}

// The job's isolated workspace (workspaces.py): the agent edited a git worktree, not the
// project. The user looks at the diff, then applies it to the project or discards it.
function workspaceView(job, space) {
  if (!space) return null;
  const title = ui.el("h2", { style: "margin-top: 16px" }, "Workspace");
  if (space.error) return [title, ui.el("p", { className: "error" }, space.error)];
  const working = !["done", "failed", "stopped", "interrupted"].includes(job.status);
  const act = (action, question) => event => {
    if (!confirm(question)) return;
    ui.busy(event.target, async () => {
      try {
        const reply = await ui.api(`/api/jobs/${job.id}/workspace/${action}`, {});
        alert(reply.message);
      } catch (error) {
        alert(`${action === "apply" ? "Apply" : "Discard"} failed: ` + error.message);
      }
      refresh(job.id);
      loadToday();
    });
  };
  const header = (...buttons) => ui.el("div",
    { className: "buttons", style: "justify-content: space-between; margin-top: 16px" },
    ui.el("h2", {}, "Workspace"), working ? null : ui.el("div", { className: "buttons" }, buttons));
  if (space.status === "conflicts") {
    // The worktree stays (it has the job's version of the files) until you remove it here.
    return [
      header(ui.el("button", { onclick: act("discard",
        "Did you fix the conflicts? The workspace folder is removed; the project does not change.") },
        "Remove workspace")),
      ui.el("p", { className: "error" }, "The changes were applied WITH CONFLICTS: look for <<<<<<< markers "
        + "(a binary or deleted file: the job's version is next to it as .from-job). "
        + "Fix them, then remove the workspace; until then the job cannot run again."),
      ui.el("pre", {}, (space.conflicts || []).join("\n")),
    ];
  }
  if (space.status !== "active") {
    const said = { applied: "The changes were applied to the project.",
                   discarded: "The workspace was discarded; the project did not change." }[space.status];
    return [title, ui.el("p", { className: "muted" }, `${said} (${space.files.length} file(s))`)];
  }
  return [
    header(
      space.files.length ? ui.el("button", { className: "primary",
        onclick: act("apply", `Apply ${space.files.length} file change(s) to the project? Nothing is committed.`) },
        "Apply to project") : null,
      ui.el("button", { onclick: act("discard", "Discard the workspace? Its changes are lost.") }, "Discard")),
    ui.el("p", { className: "muted" }, working
      ? `The agent works in ${space.path}. Apply or discard when the job has finished.`
      : `${space.files.length} file(s) changed in ${space.path}, not in the project yet.`),
    space.files.length ? ui.el("pre", {}, space.files.map(file => `${file.state}  ${file.path}`).join("\n")) : null,
    space.diff ? ui.el("details", {}, ui.el("summary", {}, "Diff"), diffView(space.diff)) : null,
  ];
}

// The files the agent edited (saved by the edit hook), with a diff and Undo buttons.
// Undo puts back the copy from before the job's first edit; it never uses git.
const CHANGE_BADGE = { added: "done", modified: "done", deleted: "failed", unchanged: "queued" };

function changesView(job, files) {
  if (files.length === 0) return null;
  const working = job.status === "queued" || job.status === "running";
  const undo = async path => {
    const question = path ? `Undo the agent's changes to\n${path}?` : "Undo every file change of this job?";
    if (!confirm(question)) return;
    try {
      await ui.api(`/api/jobs/${job.id}/undo`, { path });
    } catch (error) {
      alert("Undo failed: " + error.message);
    }
    refresh(job.id);
  };
  const changed = files.filter(file => file.state !== "unchanged").length;
  return [
    ui.el("div", { className: "buttons", style: "justify-content: space-between; margin-top: 16px" },
      ui.el("h2", {}, "Changes"),
      changed && !working ? ui.el("button", { onclick: () => undo(null) }, "Undo all") : null),
    ui.el("p", { className: "muted" }, working
      ? "Undo is possible when the job has finished."
      : `${changed} of ${files.length} file(s) differ from before this job.`),
    files.map(file => ui.el("details", {
      className: "verdict", open: openFiles.has(file.path),
      ontoggle: event => { event.target.open ? openFiles.add(file.path) : openFiles.delete(file.path); },
    },
      ui.el("summary", {},
        ui.el("span", { className: `badge ${CHANGE_BADGE[file.state]}` }, file.state), " ", file.path, " ",
        file.state !== "unchanged" && !working
          ? ui.el("button", { onclick: event => { event.preventDefault(); undo(file.path); } }, "Undo")
          : null),
      file.diff ? diffView(file.diff) : ui.el("p", { className: "muted" }, "No difference."),
      FINISHED.includes(job.status) && file.state !== "unchanged"
        ? ui.el("textarea", {
            className: "file-note", value: fileNotes[file.path] || "",
            placeholder: "A comment on this file, for \"Request changes\" (optional)",
            oninput: event => { fileNotes[file.path] = event.target.value; },
          })
        : null)),
  ];
}

// The review: your last verdict, the bad results, and Approve / Reject.
// "Request changes" is next to Send: it takes the text in the box and the file comments.
const SIGNAL_TEXT = { rejected: "rejected", rework: "changes were asked", undone: "changes were undone" };

function reviewView(job, space) {
  const finished = FINISHED.includes(job.status);
  if (!finished && !job.review) return null;
  const verdict = job.review
    ? ui.el("p", {}, ui.el("strong", {}, { approved: "Approved", rejected: "Rejected", changes: "Changes asked" }[job.review.verdict]),
        ui.el("span", { className: "muted" }, " | " + ui.time(job.review.at)),
        job.review.note && job.review.verdict !== "changes" ? ": " + job.review.note : "")
    : ui.el("p", { className: "muted" }, "Not reviewed yet.");
  return [
    ui.el("h2", { style: "margin-top: 16px" }, "Review"),
    verdict,
    job.signals.length
      ? ui.el("p", { className: "muted" }, "Bad results: " + job.signals.map(signal => SIGNAL_TEXT[signal]).join(", ")
          + " (later, the lead is asked for a lesson)")
      : null,
    finished ? ui.el("div", { className: "buttons" },
      ui.el("button", { className: "primary", onclick: event => ui.busy(event.target, () => approve(job, space)) }, "Approve"),
      ui.el("button", { onclick: () => {
        const why = prompt("Why is the result bad? (The agent learns from it. The job does not run again.)");
        if (why !== null) sendReview("reject", why);
      } }, "Reject"),
      ui.el("span", { className: "muted" }, "To ask for changes: write below, and press Request changes."),
    ) : null,
  ];
}

// Approve says "the result is good"; it does not copy files. When the job's workspace still
// has changes that are not in the project, ask to apply them first (a failed apply: no approve).
async function approve(job, space) {
  const waiting = space && space.status === "active" && space.files && space.files.length;
  if (waiting && confirm(`${space.files.length} file change(s) of this job are only in its workspace, `
      + "not in the project yet.\n\nOK: apply them to the project, then approve.\nCancel: approve only.")) {
    try {
      const reply = await ui.api(`/api/jobs/${job.id}/workspace/apply`, {});
      if (reply.status !== "applied") alert(reply.message);  // conflicts: what to fix
    } catch (error) {
      alert("Apply failed, so the job is not approved: " + error.message);
      refresh(job.id);
      return;
    }
    loadToday();
  }
  await sendReview("approve", "");
}

// Send a review to the selected job. Return true if it was sent.
async function sendReview(verdict, note, files = {}) {
  const id = shownId();
  if (!id) return false;
  try {
    const reply = await ui.api(`/api/jobs/${id}/review`, { verdict, note, files });
    composeMessage.className = "muted";
    composeMessage.textContent = { approve: "Approved.", reject: "Rejected.",
      changes: "Sent. The job runs again in the same session." }[verdict];
    refresh(id);
    return true;
  } catch (error) {
    composeMessage.className = "error";
    composeMessage.textContent = "Not sent: " + error.message;
    return false;
  }
}

requestChanges.addEventListener("click", () => ui.busy(requestChanges, async () => {
  if (shown && shown.cold_warning && !confirm(shown.cold_warning + "\n\nSend anyway?")) return;
  const id = composeFor;
  if (await sendReview("changes", compose.elements.text.value, fileNotes) && composeFor === id) {
    compose.elements.text.value = "";
    fileNotes = {};
  }
}));
