// Views: the sidebar and which part of the page shows.
//
// The address says the view: #today, #jobs, #new... and #jobs/<id> for one job. So the
// browser's Back button and a reload keep where you were. showView() draws the page from the
// address (render from state): every way to change the view only changes the address.

const VIEWS = ["today", "jobs", "new", "projects", "skills", "routines", "recos", "lessons", "proposals",
               "settings"];
const START_VIEW = "today";
const MORE_KEY = "agentic-os-more-open";  // localStorage: is the "More" group open?

// "#jobs/6d4726b8" -> { view: "jobs", job: "6d4726b8" }; an unknown view -> the start view.
function route() {
  const [view, job] = location.hash.slice(1).split("/");
  return VIEWS.includes(view) ? { view, job: job || null } : { view: START_VIEW, job: null };
}

function showView() {
  const { view, job } = route();
  document.querySelectorAll(".view").forEach(part => { part.hidden = part.dataset.view !== view; });
  document.querySelectorAll(".sidebar a").forEach(link => {
    if (link.dataset.nav === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  // On a phone the Jobs view shows the list OR the job: this class says which (ui.css).
  document.getElementById("jobs-split").classList.toggle("has-job", view === "jobs" && job !== null);
  if (view !== "jobs") return;
  if (job && job !== selected) {
    showDetail(job);
  } else if (!job && selected) {
    selected = null;
    detail.hidden = true;
  }
  markSelected();
}

// Open a job: change the address; showView (on hashchange) draws it.
async function openJob(id) {
  if (route().view === "jobs" && route().job === id) return showDetail(id);  // already there: just draw
  location.hash = "jobs/" + id;
}

// Close the job view: back to the list.
function closeJob() {
  selected = null;
  detail.hidden = true;
  if (route().job) location.hash = "jobs";
  markSelected();
}

// Go to a view now (not only on the next hashchange), for code that then needs it visible.
function goTo(view) {
  location.hash = view;
  showView();
}

// The numbers next to the links, from the Today facts (GET /api/today).
// Jobs: what waits for you in a job (a question, a plan, a command...); the others: their counts.
function showCounts(facts) {
  const counts = { jobs: 0, recos: 0, lessons: 0, proposals: 0 };
  for (const item of facts.needs_you) {
    if (item.job) counts.jobs += 1;
    else if (item.kind in counts) counts[item.kind] = item.count;
  }
  for (const [name, count] of Object.entries(counts)) {
    const badge = document.getElementById("count-" + name);
    badge.hidden = !count;
    badge.textContent = count ? String(count) : "";
  }
}

// The "More" group folds; the page remembers it (only in this browser).
function setMoreOpen(open) {
  document.getElementById("more").hidden = !open;
  document.getElementById("more-toggle").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem(MORE_KEY, open ? "1" : "0"); } catch { /* private mode: it is not remembered */ }
}

function startViews() {
  let open = true;
  try { open = localStorage.getItem(MORE_KEY) !== "0"; } catch { /* private mode: open */ }
  setMoreOpen(open);
  document.getElementById("more-toggle").addEventListener("click", () => {
    setMoreOpen(document.getElementById("more").hidden);
  });
  addEventListener("hashchange", showView);
  showView();
}
