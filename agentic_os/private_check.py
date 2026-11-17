"""Private check: stop a git commit that would put personal text into the repo.

The pre-commit hook in .githooks/ runs it. It looks only at what is staged:
- a file that .gitignore ignores but was added anyway (git add -f), for example
  CLAUDE.local.md or memory/identity/user.md;
- an added line with one of your private words (your name, your email, a city ...).
  The words are in .private-words, one per line (not in git; # starts a comment);
- an added line that looks like a secret (see secret_scan.py).

It is exact work (git and text), so it is Python, no AI.
Turn the hook on once per clone:  git config core.hooksPath .githooks

Usage (from the project folder):
    python -m agentic_os.private_check     exit code 1 when the commit must stop
"""

import subprocess
import sys
from pathlib import Path

from agentic_os.secret_scan import find_secret

WORDS_FILE = ".private-words"
# Files that hold secret-like patterns on purpose: the secret check skips them (the word check does not).
SECRET_PATTERN_FILES = {"agentic_os/secret_scan.py", "tests/test_secret_scan.py", "tests/test_private_check.py"}


def git(*args, check=True):
    """Run git in the current folder and return its output."""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check).stdout


def private_words(path=WORDS_FILE):
    """Return the private words from the file (lower case), or [] when there is no file."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [line.strip().lower() for line in lines if line.strip() and not line.strip().startswith("#")]


def added_lines(diff):
    """Return (path, line number, text) for each added line of a `git diff -U0` output."""
    path, number = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            # @@ -a,b +c,d @@ : the new lines start at c
            number = int(line.split("+")[1].split(" ")[0].split(",")[0])
        elif line.startswith("+") and path is not None:
            yield path, number, line[1:]
            number += 1


def problems_in_lines(lines, words):
    """Return the messages for added lines that hold a private word or a secret."""
    found = []
    for path, number, text in lines:
        lower = text.lower()
        word = next((w for w in words if w in lower), None)
        if word is not None:
            found.append(f"{path}:{number}: a private word ({word})")
        what = None if path in SECRET_PATTERN_FILES else find_secret(text)
        if what is not None:
            found.append(f"{path}:{number}: looks like {what}")  # the kind only, never the value
    return found


def ignored_staged_files():
    """Return the staged files that .gitignore ignores (they were added with -f)."""
    staged = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    staged = [path for path in staged if path]
    if not staged:
        return []
    result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin", "-z"],
                            input="\0".join(staged), capture_output=True, text=True)
    return [path for path in result.stdout.split("\0") if path]


def main():
    found = [f"{path}: a personal file (it is in .gitignore)" for path in ignored_staged_files()]
    diff = git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff")
    found += problems_in_lines(added_lines(diff), private_words())
    if found:
        print("Commit stopped: the staged changes hold personal text.")
        for message in found:
            print("  " + message)
        print("Remove it (git restore --staged <file>, or edit the line) and commit again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
