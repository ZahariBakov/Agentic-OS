"""Agentic OS: a Python "operating system" that runs and manages Claude Code agents."""

# This file runs first, before any module of the package. It uses only old
# Python syntax, so even Python 2 can run it and show a clear message.
import sys

if sys.version_info < (3, 13):
    sys.exit("Agentic OS needs Python 3.13. Activate the venv first: source .venv/bin/activate")
