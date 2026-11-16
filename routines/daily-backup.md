---
description: A compressed copy of data/ in backups/ (the newest 7 are kept)
schedule: daily 03:00
mode: passive
action: backup
max_week_usd: 0.50
---

Python only, no AI, no cost: `python -m agentic_os.backups now` once a day.
data/ (jobs, lead sessions, lessons, undo copies, settings) is not in git, so
this is its only copy. How to restore: see the docstring of agentic_os/backups.py.
