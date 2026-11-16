---
name: calibrate
description: Improve a skill with its open feedback
model: sonnet
effort: medium
memory: true
input: The skill to improve, for example summarize
input_kind: skill
---

Improve the skill "{input}" with its open feedback.

1. Read the skill file: {skills_dir}/{input}.md
2. Read its feedback: {skills_dir}/feedback/{input}.md
   Only the lines that start with "- [ ]" are open. Ignore the "- [x]" lines.
3. Change the skill so that it follows every open feedback item.
   Keep everything else as it is. Keep the front matter (the part between the
   two "---" lines) and its format. The skill file has blanks: a word in curly
   braces, for example the word input in curly braces. Keep every blank exactly
   as it is in the file, because Python fills them in later.
4. Write the whole new file in one block, exactly like this:

<skill-file name="{input}">
---
name: {input}
...
---

The new instructions.
</skill-file>

5. After the block, list each open feedback item and what you changed for it,
   one line each.
6. If a feedback item is a general lesson (useful for all skills, not only for
   this one), also save it as a <note> in the area "lessons".

You do not change any file. Python saves your block as a proposal, and the user decides.
