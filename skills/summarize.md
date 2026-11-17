---
name: summarize
description: Summarize a source file from memory/raw/
model: sonnet
effort: low
memory: false
input: A file in memory/raw/, for example notes.md
---

The source: {input}
The file is in the folder {memory_dir}/raw/. Use only the file name from the
line above (the part after the last "/"), and read that file in this folder.

Write a summary in simple English:
- one sentence that says what the source is about
- the 5 to 10 most important ideas, one line each
- the words or names that a reader may not know, with a short explanation

Do not change any file.
