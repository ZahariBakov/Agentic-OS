// The Lessons panel: lesson drafts from the leads.

// Lessons: after a bad result, a lead writes one line. It waits here as a draft:
// you keep it (as written, or in your own words) or drop it. Kept lessons go into
// the lead's prompt in its next fresh session.
const MAX_LESSON_CHARS = 220;  // leads.MAX_LESSON_CHARS

function loadLessons() {
  ui.api("/api/lessons").then(all => {
    const panel = document.getElementById("lessons");
    const drafts = all.flatMap(lead => lead.drafts.map(draft => ({ ...draft, lead: lead.lead })));
    const kept = all.filter(lead => lead.kept.length);
    panel.hidden = drafts.length === 0 && kept.length === 0;
    const decide = (draft, action, text) => async () => {
      try {
        await ui.api(`/api/lessons/${draft.lead}/${draft.id}/${action}`, action === "keep" ? { text } : {});
      } catch (error) {
        alert("Not saved: " + error.message);
      }
      loadLessons();
    };
    const edit = draft => () => {
      const text = prompt(`Your words for ${draft.lead}'s lesson (one line, at most ${MAX_LESSON_CHARS} characters):`, draft.text);
      if (text !== null) decide(draft, "keep", text)();
    };
    ui.show(panel,
      ui.el("h2", {}, drafts.length ? `Lesson drafts (${drafts.length})` : "Lessons"),
      drafts.length ? ui.el("p", { className: "muted" },
        "A lead wrote these after a bad result. Only the lessons you keep reach its prompt.") : null,
      drafts.map(draft => ui.el("div", { className: "verdict" },
        ui.el("div", { className: "buttons", style: "justify-content: space-between" },
          ui.el("strong", {}, `${draft.lead}: ${draft.text}`),
          ui.el("div", { className: "buttons" },
            ui.el("button", { className: "primary", onclick: decide(draft, "keep", null) }, "Keep"),
            ui.el("button", { onclick: edit(draft) }, "Edit"),
            ui.el("button", { onclick: () => { if (confirm("Drop this lesson draft?")) decide(draft, "drop")(); } }, "Drop"))),
        ui.el("p", { className: "muted" }, "Why: " + (draft.reason || "(no reason given)")),
        ui.el("p", { className: "muted" }, `After job ${draft.job}: ${draft.trigger} | ${ui.time(draft.at)} `,
          ui.el("button", { onclick: () => openJob(draft.job) }, "Open the job")))),
      kept.length ? ui.el("details", {},
        ui.el("summary", {}, `Kept lessons (${kept.reduce((sum, lead) => sum + lead.kept.length, 0)})`),
        kept.map(lead => [
          ui.el("strong", {}, lead.lead),
          ui.el("ul", {}, lead.kept.map(lesson => ui.el("li", {}, lesson.text,
            lesson.edited ? ui.el("span", { className: "muted" }, " (your words)") : null))),
        ])) : null,
    );
  }).catch(error => console.error("lessons:", error));
}
