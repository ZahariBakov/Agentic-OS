---
description: Delete the file copies of finished jobs older than 30 days (data/snapshots/)
schedule: daily 03:30
mode: passive
action: prune-snapshots
max_week_usd: 0.50
---

Python only, no AI, no cost: `python -m agentic_os.changes prune` once a day.
A job that is not finished keeps its copies. After the copies are gone, the
job's changes can no longer be shown or undone.
