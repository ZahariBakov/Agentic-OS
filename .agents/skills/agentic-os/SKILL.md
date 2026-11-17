---
name: agentic-os
description: How the Agentic OS codebase fits together - jobs, router, leads, memory, safety, limits, checks, skills, feedback and calibrate, artifacts and the web page; which module owns which behaviour, where each kind of data lives, and the rules that keep agents safe and cheap. Use this whenever you change or debug code in agentic_os/, add a feature, add a lead to roster.json, a skill to skills/, a page or API to the web server, or the user asks "where is X done", "why did the job fail", "how does the lead resume", "add a new ...", even for a small change.
---

# Agentic OS - the codebase

Agentic OS runs Claude Code agents (`claude -p`) as jobs and manages them with
plain Python. The one rule behind the design: **exact work is Python, AI is
only for language and decisions.** Agents never write state, memory or skill
files themselves; they return tagged blocks and Python validates and saves.

For the `claude -p` flags, the stream and sessions, load `headless-agent-runs`.

## Start here

1. A job failed, hangs or costs too much -> "Flow of a job", then `debugging`.
2. A lead resumes wrongly or starts fresh too often -> "Leads".
3. A new feature -> read `PLAN.md` if you have one, find the owner module below, copy the
   nearest example (`clone-from-template`).
4. A web change -> "Web", plus `web-frontend`.

## Who owns what

| Module | Owns | Data |
|---|---|---|
| `ask.py` | the only place that starts `claude -p`; the question goes through stdin; timeout and kill on any error; reads the stream; records limits | - |
| `jobs.py` | the commands (`main`), ingest (`MEETING_TASK` for `raw/meetings/`), `new_sources`; the functions of the four `job_*` modules under their old names (`jobs.add`, `jobs.run_job`), never a constant or `ask` | - |
| `job_store.py` | the `Job` dataclass and its file (`save`, `load`, `all_jobs`, `add`, `update`), `claim_lock`, the stop flag, `worker_busy`/`is_alive`, the inbox files, `settings` | `data/jobs/<id>.json`, `data/inbox/`, `data/settings.json` |
| `job_talk.py` | questions (`extract_question`), plans with a cost (`extract_plan`, `decide_plan`), follow-ups (`follow_up`, `prompt_for`, `add_messages`), `cold_warning`; starts no agent | - |
| `job_review.py` | `approve`, `reject`, `comment`, `undo`, `workspace_action`, signals and lesson jobs | - |
| `job_run.py` | agent calls (`first_call`, `call_agent`), checks and retries (`work`), `run_job`, `claim`, desks, `stop`, `recover`, `resume`, `SHUTDOWN` | `data/prompts/` |
| `router.py` | choose model + effort: rules -> Jev -> Haiku | `data/router_log.jsonl` |
| `leads.py` | roster, dispatch, session per lead, fresh vs resume, handoff | `roster.json`, `data/leads/<name>/` |
| `memory.py` | notes, map, log, identity, `<note>` blocks, memory prompt, `health()` exact checks, `file_lock` | `memory/` |
| `safety.py` | allowed tools per agent, folder limits, `protected()` files no agent may change (memory, routines, skills, data, roster, settings); `deny_rules(workspace)` repeats them inside a worktree of this project (`mirrored`) and denies the worktree's `.git` file; `Read(//<project>/data/app-token)` for every agent (absolute: a relative rule covers only agents whose cwd is the project); the hook runs as `python -I -c ...` (never `-m`: it puts the agent's cwd on sys.path) | `agentic_os/agent_settings.json` |
| `limits.py` | 5-hour and weekly usage; pause new jobs at 90% or on a "rejected" window; a top-level "rejected" (`rateLimitType`: a model, overage) is saved as `_limited` and pauses until its reset; `load()` returns only the windows | `data/limits.json` |
| `skills.py` | skill files, `{input}`/`{references}` blanks, run a skill as a job, `keep_wildcard` | `skills/*.md`, `skills/references/<name>.md` |
| `wildcards.py` | pick the wildcard places, the rules for the task, parse and check numbered items, add a kept wildcard to the references; imports no project module | - |
| `routines.py` | schedule (`daily`/`weekly`/`every`), `tick`, passive vs active, weekly cost cap | `routines/*.md`, `data/routines.json` |
| `documents.py` | PDF/Office/HTML in `raw/` -> `<name>.md` next to it (markitdown, Python only, in a child process `documents child`: `TIMEOUT_SECONDS`, `MAX_MEMORY_BYTES` as `RLIMIT_AS`) | `memory/raw/` |
| `briefing.py` | the facts for the morning briefing (one small function per part) | reads jobs, feedback, routines, memory, limits |
| `feedback.py` | feedback backlog, `<skill-file>` proposals, diff, accept/reject | `skills/feedback/`, `data/proposals/` |
| `critic.py` | blind comparison of a result with a reference (A/B in random order, Python maps back): winner, why, fix | `data/critic_log.jsonl` |
| `changes.py` | copy of a file before a job's first edit, diff (difflib), undo (`copyfile`, not `copy2`: a new mtime, so make rebuilds); skips protected files; `prune` deletes the folders of finished jobs older than `KEEP_DAYS` (by the newest copy and `finished_at`; under `.claim.lock`; an unreadable job file or a missing `data/jobs/` keeps everything; daily routine action `prune-snapshots`; the age ignores `.lock`) | `data/snapshots/<job>/` |
| `edit_hook.py` | the PreToolUse hook (set in `safety.settings()`); job id from `AGENTIC_OS_JOB_ID` (`ask(env=...)`), copies only inside `AGENTIC_OS_EDIT_DIR`; never blocks: exit 0 | - |
| `workspaces.py` | isolated workspaces: a git worktree per job that can edit (mode `isolate_jobs` in `data/settings.json`: always/shared/off); `jobs.prepare_workspace` makes it at run start, `jobs.edit_folder` is where the agent may edit and the check runs (the agent still runs in `work_folder`, so a lead keeps its session); `snapshot_tree(worktree, repo)` (its own `mkdtemp` index per call: threads; the index starts from `read-tree HEAD`, else a tracked file that a new .gitignore line ignores looks deleted and Apply deletes it (job 02eee4b0, `.idea/`); `LINKED` (.venv, node_modules) and `skip` leave the index with `update-index --force-remove` after `add -A`, never an `:(exclude)` pathspec (git 2.25 refuses it for an ignored link: job f694068c); `check_link` refuses a changed `.git` file), `review`, `apply` (refuses `safety.is_protected` paths of this project; `git apply`, else `merge_files` with `git merge-file` per file: `--3way` needs a clean index), `discard`; the caller saves the job (`jobs.workspace_action` under `.claim.lock`); does not import jobs. `create` removes a half-made worktree on any error (a failed copy of your uncommitted work must not start from HEAD); `remove_worktree` never runs `git worktree prune` (it deletes the user's own records), `forget_record` deletes only ours; `check_rerun` refuses a new run (follow-up, comment, resume) while the status is `conflicts` (Discard = "Remove workspace"); `drop_if_unchanged` at the end of a finished run; `create` refuses a repository with no commit (no HEAD: a clear message, not git's "invalid reference"); files that only the check made (a build folder: `job_run.run_check` lists `new_files` before it, `check_made` after it) go to `workspace["check_files"]` and `snapshot_tree(skip=...)` leaves them out of the diff and Apply; `changes.forget(job, worktree)` when a worktree is made, applied or discarded (the next one has the same path) | `job.workspace`, `~/.cache/agentic-os/worktrees/<job>/` |
| `mail.py` | Gmail over IMAP, read-only (`select(readonly=True)`, every fetch `BODY.PEEK`): `fetch_recent` (24 hours, 30 mails, 300 characters; a key-like snippet hidden; `<mail>` cannot be closed), `facts`; `setup` with getpass; `MailError` messages say what to do; `IMAP` is the connection class (conftest replaces it: no network in tests, `FakeIMAP` in tests/test_mail.py) | `data/mail.json` (0600; `safety.SECRET_FILES`, skipped by backups) |
| `weather.py` | the forecast from Open-Meteo, Python only: `fetch` (the one network call; `OPEN` is the opener, no redirects; conftest replaces it: no network in tests, the `host` fixture in tests/test_weather.py), `parse` (pure; "now" is the time in the answer, not our clock; only numbers and times are kept, every text is ours from `CODES`), `weather(now)` (cache `CACHE_SECONDS`; no network -> the old answer with `stale`), `place()` (setting `weather_place`, a wrong one -> `DEFAULT_PLACE`), `report`; `/api/weather` (503 on `WeatherError`), drawn by `ui.header` left of the limits. The degree sign is written as an escape (ASCII rule) | `data/weather.json` |
| `office_mcp.py` | the office MCP server (stdio JSON-RPC by hand, no package): `handle(message, job_id)`, `TOOLS` (`memory_search`, `project_recall`; read-only, free), `config_args(job_id)` (`--mcp-config` + `--strict-mcp-config`, `python -I -c`, `AGENTIC_OS_JOB_ID` in env), `tool_names()` for `--allowedTools`; added in `jobs.agent_args` (first, before the deny list); not for `tools: none`. Tests call `handle` in-process (a child process does not see monkeypatch) | - |
| `commands.py` | approved shell commands for the office tool `run_command`: `blocked` (words anywhere in the line, secret file names), `allowed_by` (your list: simple commands only, never the network), `request` -> `wait` (polls the file; only a pending one gives up, under `.commands.lock`) -> `run` (job's `edit_folder`, own process group, timeout, stop); `decide` (web, CLI); settings `commands_mode` ask/off, `commands_allow`; `MCP_TOOL_TIMEOUT` set in `call_agent`; live updates watch `data/commands/` too | `data/commands/<id>.json` |
| `specialists.py` | the office tool `delegate`: `start` (checks, `CAP_USD` for all sub-jobs of a job, an equal part of what is left as `max_budget_usd` -> `--max-budget-usd` in `first_call`), `choose_model` (router keyword rules, opus -> sonnet, haiku's track record), `delegate` (a thread per sub-job, `wait_all` stops jobs that did not start, a stopped parent or `WAIT_SECONDS`). A sub-job (`job.parent`) cannot edit (`can_edit`), ask (`can_ask`), run commands or delegate. `jobs.tool_timeout_ms` sets `MCP_TOOL_TIMEOUT`. The office server sends progress prints to stderr (stdout = JSON-RPC) | sub-jobs in `data/jobs/` |
| `brain.py` | the Brain page's graph (`graph()`, `/api/brain`, page `/brain`): area/note/lead/project nodes; edges in, link (`memory.LINK_PATTERN`, `WIKI_LINK_PATTERN`), similar (rare words, `SIMILAR_PER_NOTE`, `MIN_SIMILAR`), wrote (`job.notes`), worked (jobs per project); saves nothing. `static/brain.html` draws it (SVG, own force layout) | reads memory/, jobs, roster, projects |
| `secret_scan.py` | `find_secret`/`check`: refuse agent text that looks like a key before Python saves it (notes, handoffs, lessons, proposals, wildcards); `memory.save_notes` imports it inside the function (the hook loads memory) | - |
| `outcomes.py` | cost per successful job (done, no signals, not rejected), by lead and model; the Outcomes panel on the Costs page (`api_costs`) | reads jobs |
| `backups.py` | a `.tar.gz` of `data/` (no locks, no temp files), the newest `KEEP` kept; daily routine action `backup` | `backups/` (not in git, protected) |
| `project_memory.py` | per project (the git repository of `job.folder`, or the folder; not `~/.cache/agentic-os`): instructions (always), decisions, known failures, outcomes (only when relevant: shared rare words, `rank`); `brief` goes into the first call's task (not the prompt file: the lead's cache stays); `<project-note kind="decision|failure">` -> `save_agent_notes` (each note checked alone: kind, length, secret); `record_outcome` one line per done/failed job (replaced when the job runs again); called from `jobs.save_project_memory`; Projects panel, `/api/projects` (with `last_check` from `last_checks(jobs)`: the check of the project's newest done job, which Continue puts into the Check field; `api_add_job` refuses a task that ends with an empty "Next:"; `task_words` makes the outcome line of a Continue job from the words after "Next:"). Tests: `temp_jobs` moves `PROJECTS_DIR` (a job with a tmp folder has a project) | `data/projects/<name>-<hash>.json` |
| `suggest.py` | one-click answers from a question's words (list, "A, B or C?", yes/no) when the agent gave no options; `jobs.extract_question` sets `suggested: True` | - |
| `recos.py` | Recos: requests you repeat by hand (`~/.claude/history.jsonl` + jobs not from routines, skills or ingest) -> groups by shared words (Jaccard) -> suggested routine cards; repeated review notes of a lead -> memory cards; saved cards, handled ids, Set up (routine file or lesson job), Dismiss; Python only, no AI | `data/recos.json`, writes `routines/<name>.md` |
| `cache_report.py` | cold cache reloads from Claude Code's session files (`~/.claude/projects/**/*.jsonl`: time, model, usage per answer; our job files have no per-call times), their extra cost, and whether keep-warm pings would pay; Agentic OS agents (cwd under `~/.cache/agentic-os`, or a job's session) apart from your chats; Python only, saves nothing | reads `~/.claude/projects/` |
| `artifacts.py` | save every finished result, rebuild the index | `data/artifacts/` |
| `web.py` | FastAPI on 127.0.0.1, JSON API, SSE live updates, thread pool. The Jobs page: HTML in `static/jobs.html`, its code in `static/app/` (plain `<script>` files that share global names, one per part: `common`, `today`, `new_job`, `job_list`, `job_view`, `job_talk`, `job_review`, `projects`, `skills`, `lessons`, `routines`, `recos`). They only define names; `main.js`, loaded last, starts the page (header, live updates, first loads, timers): a reply that comes while a later file still loads must not call a name that does not exist yet. A new part: a file in `app/`, a `<script>` tag before `main.js`, its start call in `main.js` | `agentic_os/static/` |
| `access.py` | the web token: `decide` (pure: Host, Origin, then the `/__app?t=` login link, the `agentic_os_<port>` cookie or the `X-Agentic-OS-Token` header); the `local_only` middleware in `web.py` calls it; `HideToken` keeps the token out of uvicorn's access log. `token()` holds `TOKEN_LOCK` (threads) and links a `mkstemp` file (processes; `os.replace` without hard links); the middleware uses `token_or_none` (None -> `UNREADABLE`); `/events` checks `still_valid` each second, because a stream outlives the token check at its start. Web tests call the API functions directly, so they skip the middleware | `data/app-token` (0600) |

`data/` is in `.gitignore` (personal results), and so are the personal memory files
(`memory/` notes, `memory/identity/user.md`, `CLAUDE.local.md`). `skills/`, `routines/`,
`roster.json` and the example identity files are in git.

The job modules are layers, each imports only the ones before it: `job_store -> job_talk -> job_review -> job_run -> jobs`. In them, a name of another job module is written with its module (`job_store.save(job)`). Text below that says `jobs.<name>` means the function in its owner module (the table above).

Import order matters: `routines -> skills -> jobs -> feedback`; `briefing` imports `routines`, so `routines.render_task` imports `briefing` inside the function. `feedback.py` must not import
`jobs` or `skills` (circular import). Check the module docstring before adding
an import.

## Flow of a job

1. `jobs.add` saves a `queued` job (a JSON file is the database). No model
   given -> `router.route` chooses it here, at add time.
2. `run_queued` / the web pool picks it. `limits.pause_reason` can keep it
   queued. `claim` turns `queued` into `running` under a file lock, so a
   job never runs twice (double click, or web server + `jobs run`). A lead job takes `leads.lock`, then `leads.prepare`.
3. `first_call` builds the options: system prompt file, `safety.agent_args`
   (settings deny list, allow list, memory read-only).
4. `call_agent` -> `ask.ask` -> cost, session id, pid saved on the job.
5. `work`: if the job has a `check`, run it; on failure send the error back
   to the **same session** (`FIX_PROMPT`), up to `MAX_TRIES`. If the job has a
   `reference`, the critic (`run_critic`) compares a result that passed the
   check; when the reference wins, its fix goes back (`CRITIC_FIX_PROMPT`).
   The critic is advice: the job stays done after `MAX_TRIES`; see `job.verdicts`.
6. Python marks the job `done` or `failed`, saves `<note>` blocks (memory
   jobs only) and `<skill-file>` proposals, and saves the artifact.

A job is `done` only when its check passes, never because the agent says so.

## Recovery (interrupted jobs)

- `ask(on_session=...)` gets the session id from the stream's first `system`
  event, and `call_agent` saves it at once. The `result` event comes only at the
  end, so without this an interrupted first call could never resume.
- `claim` writes `job.worker = os.getpid()`. `recover()` (web `lifespan`, `jobs
  recover`) marks a `running` job with a dead worker as `interrupted` and SIGTERMs
  its old agent. Never judge "dead" by the status alone: the web server and
  `jobs run` can both run jobs.
- No automatic resume (rule 6). `resume(id)` (`jobs resume`, `POST
  /api/jobs/<id>/resume`, the Resume button) -> `queued`, `resumes += 1`.
- In `work`, `job.resuming` means "resumed". `resume_call` sends `RESUME_PROMPT` (tries,
  changed files from `changes.py`, last check output, "do NOT repeat") to that
  session. A lead's job reuses `leads/<name>/prompt.md`, not `leads.prepare`
  (which may start fresh). The cut call's cost is lost.
- `new_sources` counts `interrupted` as handled; Undo allows it.

## Follow-up messages

- `follow_up(id, text)` appends to `data/inbox/<id>.jsonl` under the claim lock.
  Never write new data from outside into the job's JSON file while it runs: the
  worker holds an old `Job` object and its next `save` deletes that data.
- `work` takes the inbox at the start (resume brief, follow-up call, or with the
  task) and again when the judges are happy. `run_job` re-queues under the lock
  when a message came after the last call, then runs again.
- `job.resuming` (set by `resume`) says "resumed", not `session_id`: a done job
  with a follow-up also has a session.
- `MAX_TRIES` counts the calls of one run (`start_tries`).
- Web: `POST /api/jobs/<id>/messages`; `api_job` adds `inbox` (`read_inbox`, not
  taken) and `cold_warning`. In `jobs.html` the message box `#compose` (code: `app/job_view.js`) is outside
  `#detail-body`: SSE redraws the body on every change, and a redrawn textarea
  loses the draft and the focus. Keep inputs out of redrawn parts.
- `ui.css` has `[hidden] { display: none !important }`: a class with `display`
  (`.form` is a grid) would beat the `hidden` attribute.

## Questions from the agent

- `ASK_RULES` are added to the task of jobs that `can_ask` (not routines, not
  ingest). `work` returns problem `"question"` when a reply has a `<question>`
  block; `run_job` saves `waiting` + `job.question` **under the lead's lock**, so
  the lead's next job sees it in `waiting_job` and is held (`held_for`, back to
  `queued`). `release_held` after the waiting job ends (or is stopped).
- The answer is a follow-up: `follow_up` marks the message `answer: True` ->
  `ANSWER_PROMPT`; `add_messages` moves the question into `job.messages`.
- Web: `questionView` in `app/job_talk.js` (one button per option -> `sendMessage`,
  the same path as the message box); `briefing.questions_part` is the reminder.
- A running web server must restart after new `Job` fields: an old process
  cannot load a job file that has them (`Job(**data)` raises TypeError).

## Review

- `approve`, `reject` (a reason is required), `comment` (a follow-up with
  `REVIEW_PROMPT`; file comments only for files in `changes.changes`), and
  `jobs.undo` work only on `FINISHED` jobs (undo: not queued or running).
- Web: `POST /api/jobs/<id>/review` (`verdict` approve/reject/changes). In
  `app/job_view.js`, `fileNotes` and `openFiles` are state, drawn again on every
  redraw; `showJobs` skips the job view redraw while a textarea/input in it has
  the focus (`redrawLater`, then a redraw on `focusout`).
- Approve does not copy files. `approve()` in `app/job_review.js` asks to apply an
  `active` workspace with changes first; a failed apply means no approve.
- Lessons: `add_signal` returning True -> `ask_lesson` adds a lesson job
  (`lesson_for` = {job, signal}) for a lead's job; `run_job` turns its `<lesson>`
  block into a draft (`leads.add_draft`). `leads.keep_lesson`/`drop_lesson`;
  kept lessons are a section of `LEAD_PROMPT` (fresh sessions only). The web
  page starts lesson jobs with `start_lessons(job_id)`. `GET /api/lessons`,
  `POST /api/lessons/<lead>/<id>/keep|drop`; the panel `loadLessons` in
  `app/lessons.js` reloads with the proposals (when the count of finished jobs changes).
- `job.signals` keeps bad results once each: `rejected`, `rework`, `undone`.
  Every undo goes through `jobs.undo` (web, `jobs undo`, `changes undo`), so the
  signal is never missed. `changes.main` imports `jobs` inside the function
  (jobs imports changes).

## Plans with a cost

- `needs_plan` (opus or `plan_first`, only if `can_ask`, first call only) ->
  `first_call(job, read_only=True)` + `PLAN_RULES` with `cost_reference`. `work`
  returns `"plan"` when the first reply has a `<plan>` block; with no block,
  the next calls get the edit tools back.
- `run_job` saves `waiting` + `job.plan` under the lead's lock; auto-approve
  (`settings()["auto_approve_plans_usd"]`) writes the decision to the inbox
  under the claim lock, so the end-of-run check queues the job again.
- `decide_plan`: approve/change -> a `plan: True` inbox message (a ready prompt,
  see `prompt_for`); reject -> `stopped`, no agent call. `follow_up` on a
  waiting plan = change.
- Web: `planView` in `app/job_talk.js`, `POST /api/jobs/<id>/plan`, `GET/POST
  /api/settings`; `api_job` adds `cost_history`. Format money with `jobs.usd`
  (4 decimals under one cent; `money2` in the page).
- Tests that check tools must read only the items between `--allowedTools`
  and `--disallowedTools`: `--settings` has "Edit|Write" in the hook matcher.

## Skills that start themselves, and agents with no tools

- Front matter `trigger: a, b`: `web.api_add_job` runs the skill when the new task starts
  with a phrase (`skills.triggered`, whole words; polite words around it are allowed:
  `POLITE_START`/`POLITE_END`, a fixed list), unless you chose a lead, a folder, a
  check or a reference. `input_kind: optional` lets the input be empty.
- `tools: none` -> `jobs.first_call` uses `safety.no_tool_args()` (`--tools ""` and
  `--strict-mcp-config`). An empty `--allowedTools` is not the same: read tools need no
  permission. Use it for any task that holds text written by strangers (mail).
- `{inbox_facts}` is filled in `jobs.fill_inbox` at the start of the first run, inside the
  try: a `MailError` fails the job with no agent call.

## Wildcards

- Front matter `items: N`, `wildcards: W` (0 < W < N) turns it on; `skills.add`
  picks the places, adds `wildcards.rules`, saves `job.wildcards` =
  {count, places, kept, problems}. `work()` checks the items after the command
  check and before the critic (exact checks first); problems -> `wildcards.FIX_PROMPT`.
- One wildcard and many need different English ("Item 3 is a WILDCARD" vs
  "Items 3 and 8 are WILDCARDS"); a test checks it.
- `keep_wildcard` holds `data/.references.lock` (a lock file in skills/ would show in git); the agent never
  writes the references (skills/ is protected).

## Recos

- `refresh` = `suggest` (routine cards) + `memory_cards` (per lead: review notes
  with verdict rejected/changes, lesson jobs skipped, grouped at 0.3, 2+), minus
  the `handled` ids, saved in `data/recos.json` under `.recos.lock`. Weekly
  routine action `recos` (imported inside `routines.recos_action`: recos
  imports routines).
- `setup(ids)`: a routine card -> `write_routine` (front matter + task, then
  `routines.load` checks it; a bad file is deleted); a memory card -> a lesson
  job (`jobs.REPEATED_TASK`, `lesson_for.signal = "repeated"`), which the caller
  starts. Set up and dismiss add the id to `handled`.
- Tests: `temp_jobs` moves `recos.RECOS_FILE` and `recos.HISTORY_FILE`. `suggest`
  reads `HISTORY_FILE` at call time (a default argument is fixed at definition
  time and monkeypatch cannot move it). A fake lead needs the full role (see
  `tests/test_lessons.py`).

## Today panel

- A finished (or interrupted) job with an active workspace is a `needs_you` item of kind
  `workspace`; `api_changes` returns `[]` once a workspace is applied or discarded (the copies were
  of the worktree).

- `today.py` collects the facts (Python, free): `needs_you` (questions, pending
  plans, interrupted jobs sorted by `at`, then the lesson-draft and proposal
  counts), `working`, `queued`, `finished` (24 hours, by `finished_at`, old jobs
  by `created_at`), `not_reviewed`, `spent_24h`. Web: `GET /api/today`,
  `showToday` in `app/today.js` (redraws only when the facts or the minute change).
- An inbox message has `text` (the prompt for the agent) and may have `note`
  (what the user wrote). The conversation shows `note || text`. A new kind of
  message with a long prompt must set `note` (`follow_up(..., shown=...)`).

## Leads

- One desk = one session, one job at a time. A lead's desks: its own (`Pip`) and twins (`Pip~2`,
  setting `lead_twins`). `jobs.take_desk_for` takes a free desk without waiting (`leads.take_desk`,
  flock LOCK_NB; released in a `finally` right after the work, before any nested `run_job`).
  All busy -> `queued` + `waits_for` (no pool place); `release_held` wakes the oldest one when a
  desk's job ends. `desk_candidates`: a job that ran before, or a lesson job, stays on its desk;
  a twin only for a read-only lead or a job with an active workspace. `waiting_job(desk)` is per
  desk: a desk with an open question is skipped. A twin's session (`data/leads/Pip~2/`) starts
  from the lead's handoff and lessons plus `TWIN_NOTE`, and retires after `TWIN_IDLE`. Use
  `job.desk or job.lead` for prepare/finish/prompt; `job.lead` stays the lead (lessons, outcomes).
- `fresh_reason` decides fresh vs resume: no session, other folder (sessions
  are per folder), context > `MAX_CONTEXT`, or cold (idle > `CACHE_TTL`) and big.
- A resumed session reuses the same `prompt.md`, so the prompt cache hits.
- The lead writes `<handoff>` after every reply (sessions can die); a fresh
  session gets the last handoff in its prompt.
- Lead folders live outside the repo (`~/.cache/agentic-os/workspaces/`), so the
  agent does not load this project's `CLAUDE.md`. Keep new agents outside too.

## Routines

- Schedule logic is pure functions with a `now` argument (`parse_schedule`,
  `next_run`, `is_due`), so tests never wait for the clock. Keep it that way.
- `tick` saves the run time *before* it starts the job, so a crash or a slow
  job never starts it twice. A new routine waits for its first time; missed
  runs start once.
- The cost cap is checked before a run; a job's cost is known only after it,
  so one run can go over the cap and the next one is skipped.
- One job of a routine runs at a time: `claim(id, guard=jobs.routine_wait_reason)` checks a
  running sibling and the cap under the claim lock (`Waits` -> stays queued); at the end
  `run_job` starts `next_of_routine`. Without it, jobs started together all passed the cap.
- `tick(now, run)` / `run_now(name, now, run)`: `run` starts an active job.
  The CLI uses `jobs.run_job` (waits); the web server passes `run_in_pool`, so
  its `routine_loop` (started in `lifespan`, every `TICK_SECONDS`) never blocks.
- A routine is a skill, a task, or a built-in `action` (`ACTIONS` in routines.py,
  for example `ingest-new`). An action may add several jobs (at most `max_jobs`),
  so `start`/`run_now` return a list of jobs; `None` means the cost cap stopped it.
- `ingest_new` calls `documents.convert_new()` first; only `.md`/`.txt` are
  ingest sources, never the binary document. markitdown reads a text file with
  an Office name as plain text; only really damaged binaries raise errors, so
  tests use real damaged bytes (`broken()` in tests/test_documents.py).
- An ingest job has `job.source` (the raw file). `new_sources` treats done, queued
  and running jobs as handled; a failed ingest is tried again. Old jobs without
  `source` are matched by `OLD_INGEST_PATTERN` on the task text.
- A task routine can use blanks: `{memory_dir}`, `{memory_health}`, `{briefing_facts}`. Python fills
  them in `render_task` when the job is made. Pattern for new routines: Python
  does the exact part first and puts the result in the prompt; tell the agent
  the result is complete, or it tries to re-run it (and the shell is denied).
- Do not run cron `tick` and the web loop together: two processes, no shared
  lock. That is what `--no-routines` is for.
- Tests fake `jobs.run_job` and use the `temp_routines` fixture: no real agent.
  To try the web loop without touching `data/`, start the app from a script
  that points `ROUTINES_DIR`, `STATE_FILE` and `JOBS_DIR` to a scratch folder.

## Guidelines

- New state is a JSON or Markdown file written by Python, with a lock if
  threads share it, and an atomic write (`memory.write_atomic`).
- New agent behaviour: add a tagged block to the prompt and a parser in
  Python. Find the blocks with `memory.blocks_outside_code(PATTERN, text)`: it skips a
  block whose tag starts in Markdown code (an example) and never removes code first. Do not give the agent write access to project files.
- Every `claude` call goes through `ask.ask`, so limits and errors stay in one
  place.
- Keep each module runnable: `python -m agentic_os.<module> ...` with argparse.
- Code, comments and messages in simple English.

## Rules from the full code review (2026-11-05)

- Stop is a flag file `data/jobs/<id>.stop` (`jobs.stop_file`), not only the status:
  the worker's Job object saves "running" after the critic or a follow-up. `is_stopped`
  reads the flag; `claim` clears it; `run_job` ends a flagged run as "stopped".
- Agents run in their own process group (`ask(start_new_session=True)`); end them with
  `ask.kill_group` (the tools too). The check (`run_check`) does the same.
- Library functions raise ValueError for wrong input, never SystemExit: SystemExit in a web
  endpoint stops the whole server. Only `main()` turns ValueError into SystemExit (`jobs.add`).
- `run_job` runs again at the end only when a message came during this run: not while a plan
  waits for the OK (the message goes with the decision), and not when the run failed before
  `work()` took the inbox (it would fail the same way, again and again).
- `jobs.add(..., fields={...})` sets wildcards, lesson_for, source, routine before the one
  save. Never add a job and then save it again: another worker may have claimed it.
  For an old Job object use `jobs.update(id, **fields)` (claim lock, fresh load).
- **Who writes a job's file:** while a live worker owns a job (`jobs.worker_busy`: `worker` set
  and alive), only the worker writes it; it saves its whole old Job object at the end. A stop
  (`stop_file`) and a message (the inbox) are side files; every other user action (review, undo,
  apply/discard, plan decisions, keep wildcard) raises `busy_error` ("try again in a moment").
  A dead worker owns nothing; `recover` also frees a dead worker of a job that is not running.
- Every paid call goes through `call_agent`, which checks `is_stopped` first; `work` checks it
  before it takes the inbox. A held job checks it before it writes "queued".
- A queued job that still has its `question` was answered and holds the lead (`waiting_job`).
- The edit hook saves copies only inside `AGENTIC_OS_EDIT_DIR` (`edit_hook.environment`,
  set by `call_agent` when `can_edit(job)`): it runs before the permission check.
- `safety.HOOK_MODULES` (the files the hook loads), `.git/` and `.venv/` are protected;
  `tests/test_safety.py` checks that the hook loads nothing else.
- `memory.write_atomic` uses a unique temp name; `memory.memory_lock()` (a file lock in
  `data/`) guards notes; artifacts use a file lock; routine state changes only through
  `record_run(name, now, added)` (state lock, fresh load).
- A routine's cap is checked again when its job starts (`routines.job_cap_reason` in
  `run_job`); `tick` catches errors (and SystemExit) per routine.
- `feedback.skill_files` counts tags (a skill file may hold an example block); only
  `calibrate` jobs save proposals; accept closes only the feedback saved with the proposal.
- Server stop: `web.Server.handle_exit` calls `stop_work` at the FIRST Ctrl+C (uvicorn runs the
  lifespan end only after all connections close): `jobs.SHUTDOWN`, the pool drops waiting jobs,
  this process's agents get SIGTERM, SSE streams end. A cut job keeps "running" (not failed);
  `recover()` makes it "interrupted" at the next start. Run the server with `Server(config).run()`
  inside `except KeyboardInterrupt` (uvicorn.run does that itself).
- Page: `showDetail` draws only when the job's data changed (`drawnKey`), and `keepView` keeps open
  `<details>` and scroll on a real redraw. After an `await`, redraw with `refresh(id)` (only if that
  job is still selected), never `showDetail(id)`: it would pull the view back to the old job.
- Check the page in a real browser: a test server script with the data paths in the scratchpad
  (and `access.decide` + `access.still_valid` allowed ONLY there), headless Chrome with
  `--remote-debugging-port`, and a node script over CDP (Node has WebSocket). `--dump-dom` gets no SSE.
  Two traps: (1) a check with the cache off hides caching bugs. On 2026-11-16 the user's browser
  kept an old ui.css (the server sent no Cache-Control), while the cache-off check was green. Pages
  and static files now send `web.NO_CACHE`; keep it, and after a CSS/JS change look once in a
  normal browser profile. (2) `Page.navigate` to the same page with another `#hash` does not
  reload it (the old JS and CSS stay): navigate to `about:blank` first for a real reload.
- A script that seeds or runs anything (a browser check, a demo, a manual try) NEVER writes to the
  real `data/`: move every module path like `tests/conftest.py` `no_real_data` does. Save
  `real = jobs.DATA_DIR.resolve()` BEFORE the loop: comparing with `jobs.DATA_DIR` inside the loop
  breaks once the loop moved it, and the later paths (`JOBS_DIR`...) stay real. After the loop,
  assert that no `Path` in `agentic_os.*` is under `real`, before the first `jobs.add`. (On
  2026-11-06 a browser check skipped this and put 19 fake jobs into `data/jobs/`; Recos then
  suggested them as routines.)
- `ask.ask`: our bookkeeping on the stream (`keep_books`: the session id, `limits.record`) and a
  non-JSON line are printed, never raised: a working agent is not killed and its cost is kept.
  Error results carry `errors`, not `result` (`error_text`). `limits.record` also holds a file lock.
- The critic is advice: when it fails, a job whose check passed stays done; the round has
  `winner: None` and the error in `why`; its cost counts (`critic.NoChoice.cost`, `AgentError.cost`);
  it gets `on_start`, so Stop ends it.
- A saved pid is ours only if that process started before the job file's last save
  (`is_alive(pid, saved_at(job.id))`, `/proc/<pid>/stat`): after a reboot the pid is someone else.
- Errors after the run (notes, proposal, lesson, artifact) keep the first error: `job.error or ...`.
- A routine action takes `(limit, fields=None)`; its jobs get the mark in `jobs.add`.
- `jobs.claim_lock()` is the one claim lock (jobs, skills, changes). `jobs.all_jobs()` skips a
  broken job file with a message. `settings()` gives the defaults for a broken file and 0 for a
  non-finite auto-approve amount. `memory.write_atomic` fsyncs and keeps the old file's mode.
- Routines: `week_cost` counts by `finished_at`; `forget_gone()` drops the state of a deleted
  routine; a clock that goes back <= `DST_SHIFT` waits (summer time); `fresh_task(name)` fills the
  blanks again when a routine's job first starts (`run_job`).
- Recos: routine cards carry `keys` (source|time of each request); handled keys never make a card
  again. Memory cards have no keys on purpose: a new correction must bring the card back.
- `skills.render` fills blanks in one regex pass (a blank inside the user's input stays).
- Tests: a test that could reach a paid call on unfixed code (the Haiku dispatcher, the router)
  must fake it (`leads.dispatch_haiku`, `router.ROUTERS`), so it fails, never pays.
- Web: start jobs only with `web.run_in_pool` (it prints a crash); file reads in async code
  go through `asyncio.to_thread`. In the page, `ui.busy(button, action)` for every button
  that costs money or sends something, and `if (selected !== id) return` after an await.

## Pitfalls

- Edit/Write rules need an absolute path written as `//abs/path/**`.
- A deny rule beats an allow rule; a refused tool is silent in `-p`.
- Stopping: mark `stopped` before the kill, or the job ends as `failed`. While a
  check runs there is no pid: `stop` only marks it, and `work` ends the job
  before the next agent call (`is_stopped`).
- Changing a lead's prompt mid-session breaks the cache; reset the lead.
- Most module locks are `threading.Lock`: they protect files only inside one
  process. Where two processes can clash (claiming a job, a lead's session),
  use `memory.file_lock` (flock).
- A test that runs a real subprocess (for example the hook command) does not see
  monkeypatch: it writes to the real `data/`. Give it input that writes nothing.
- A `set` or `frozenset` of strings has a different order in each Python run (hash
  randomization). Never take a title, an id or "the first one" from it: sort first.
  Check with `PYTHONHASHSEED=1` and `=2`.
- Tests: replace a name (monkeypatch) in the module that owns it: `job_run.ask`, `job_run.run_check`,
  `job_store.JOBS_DIR`, `job_talk.add_messages`. `jobs.<function>` is only a second name: replacing it
  changes nothing inside the job modules (it works only for callers outside them, like `jobs.run_job`
  for routines). `jobs.py` has no constants and no `ask`, so such a line fails at once.
- Tests: the autouse fixture `no_real_data` (conftest.py) moves every `Path` constant of an
  `agentic_os` module that points into the real `data/` or `~/.cache/agentic-os`. A new constant
  is covered by itself; a path in a default argument or a list is not (build it at call time).
  A test of a 0600 mode needs `open_umask` (under umask 077 it passes by itself).
- Tests: never let a test start a job in `web.pool`. It runs after the test
  ends, when monkeypatch has put the real `data/` folders back.

## Check a change

- `.venv/bin/python -m compileall -q agentic_os`
- `.venv/bin/pytest -q` (tests in `tests/`)
- Start the web page and try the change for real (`build-and-run`).
- A cheap real agent call: a Pip/Haiku job with a short task.
