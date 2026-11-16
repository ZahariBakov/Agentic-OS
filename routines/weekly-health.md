---
description: Weekly health check of the memory (Python checks + AI review)
schedule: weekly mon 08:00
mode: passive
model: haiku
effort: low
memory: true
max_week_usd: 0.20
---

This is the weekly health check of the memory in {memory_dir}.

Python already ran the exact checks (links, missing fields, duplicate titles,
the map) just before this job. Its report is complete and up to date:

{memory_health}

Do not run any command and do not repeat these checks: trust the report.
You can only read files (Read, Glob, Grep), and that is all you need.

Your part is the work that needs judgement. Read the notes in the map, then write
a short report in simple English with three parts:
1. Python's problems: for each one, say in one line how to fix it.
2. Notes that disagree with each other: name both notes and the fact.
3. Notes that look out of date or too vague to be useful: name the note and why.

Write "nothing found" for an empty part. Keep the report under 25 lines.
Do not write <note> blocks. Do not change any file. The user decides what to fix.
