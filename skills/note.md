---
name: note
description: Save a fact into the memory
model: haiku
effort: low
memory: true
input: The fact to remember
---

Save this fact into the memory:

{input}

First look at the memory map. If a note about the same thing already exists,
read it and write an updated note with the same title. Otherwise write a new note.
Write exactly one <note> block. Then say in one line what you saved.
