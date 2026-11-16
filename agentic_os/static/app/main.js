// Start the Jobs page. It runs last: the other files only define names, so a reply
// from the server can never call a function whose file has not loaded yet.

ui.header("Jobs");
startViews();  // the sidebar, and the view that the address asks for

// Live updates: the server sends the job list each time a job changes.
const live = document.getElementById("live");
const source = new EventSource("/events");
let finished = -1;  // how many jobs are done or failed
source.onmessage = event => {
  const jobs = JSON.parse(event.data);
  showJobs(jobs);
  loadToday();  // a job changed: maybe it needs you now, or it finished
  live.textContent = "\u25CF live";
  // A finished job can bring a new proposal. We do not reload the skill rows
  // here, because that would delete text that you are typing in them.
  const count = jobs.filter(job => job.status === "done" || job.status === "failed").length;
  if (count !== finished) {
    finished = count;
    loadProposals();
    loadLessons();  // a finished lesson job can bring a new draft
    loadProjects();  // and a project note or a past-job line
  }
};
source.onerror = () => { live.textContent = "reconnecting..."; };

loadLeads();
loadSkills();
loadProjects();
loadProposals();
loadRoutines();
loadRecos();
setInterval(loadRoutines, 60000);  // the next run and the costs change over time
loadSettings();
