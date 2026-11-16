---
description: Queue an ingest job for each new file in memory/raw/ (meetings too)
schedule: daily 07:30
mode: passive
action: ingest-new
max_jobs: 3
max_week_usd: 0.50
---

Put sources into memory/raw/ and meeting transcripts into memory/raw/meetings/.
Python finds the new files; each one becomes an ingest job (Sonnet).
A file in meetings/ gets the meeting task: summary, decisions, action items,
open questions. Passive: the jobs wait until you start them.
