"""Secret scan: find text that looks like a password, a token or a key, before Python saves it.

An agent may read a
secret (for example in a source file) and put it into a note, a handoff or a
lesson. memory/ and skills/ are in git, so a secret there could be pushed. The
places that save agent text call find_secret() and refuse such text.

It is exact work (patterns), so it is Python, no AI. It finds the usual kinds
of keys; it cannot find every secret.

Usage (from the project folder):
    python -m agentic_os.secret_scan <file> [<file> ...]   exit code 1 when one looks like a secret
"""

import argparse
import re
import sys
from pathlib import Path

# (what it is, the pattern). The order matters only for the message.
PATTERNS = [
    ("an Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("an OpenAI or OpenRouter key", re.compile(r"\bsk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{32,}")),
    ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    # RSA, EC, OPENSSH, DSA, ENCRYPTED, PGP ("PGP PRIVATE KEY BLOCK") ...
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # "password = hunter22", "DB_PASSWORD=...", {"api_key": "..."}, "client_secret: ...": a value
    # of 8+ characters that is not a placeholder (${VAR}, $VAR, <your key>, ***, "your...", "xxx",
    # "example", "changeme") and not a pointer to where the secret is kept (a path, os.getenv(...),
    # process.env, os.environ).
    ("a password or secret value", re.compile(
        r"\b(?:\w+_)?(?:password|passwd|secret|api[_-]?key|access[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)"
        r"[\"']?\s*[:=]\s*[\"']?"
        r"(?!\$|<|\*{3}|your|xxx|example|changeme|~?/|\.{1,2}/|os\.|getenv|process\.env|none\b|null\b)"
        r"[^\s\"'`,}]{8,}", re.IGNORECASE)),
    # A bare token like data/app-token (48 hex characters). Commit ids (40) and sha256 sums (64) differ.
    ("a hex token", re.compile(r"\b[0-9a-fA-F]{48}\b")),
]


def find_secret(text):
    """Return what kind of secret the text seems to hold ("a GitHub token"), or None."""
    for what, pattern in PATTERNS:
        if pattern.search(text or ""):
            return what
    return None


def refusal(what, where="the text"):
    """Return the message for a refused text."""
    return (f"Not saved: {where} looks like it contains {what}. Write where the secret is kept "
            "(for example \"the key in ~/.config/...\"), never the value.")


def check(text, where="the text"):
    """Raise ValueError when the text looks like it holds a secret."""
    what = find_secret(text)
    if what is not None:
        raise ValueError(refusal(what, where))


def main():
    parser = argparse.ArgumentParser(description="Find text that looks like a secret.")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    found = False
    for path in args.files:
        what = find_secret(path.read_text(errors="replace"))
        if what is not None:
            print(f"{path}: looks like {what}")  # the kind only, never the value
            found = True
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
