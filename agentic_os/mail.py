"""Mail: read the Gmail inbox (read-only) and give the facts for the inbox skill.

Exact work is Python: Python logs in, finds the new mail and cuts it short.
The agent of the inbox skill only gets this text and writes the digest, with
no tools at all (safety.no_tool_args): mail is written by strangers, and a
request hidden in a mail must find nothing to do.

Read-only, always:
    - the inbox is opened with select(readonly=True), and every fetch is a
      BODY.PEEK, so no mail becomes "read";
    - nothing is sent, moved or deleted.

What goes to the agent (and so to Anthropic): for each mail of the last
HOURS hours, at most MAX_MAILS, the sender, the subject, the date and the
first SNIPPET characters of the text. A snippet that looks like a key
(secret_scan) is hidden.

The password is a Gmail "App password", in data/mail.json (mode 0600). Only
`mail setup` writes it, and it asks with getpass, so the password is not shown
and is never in a job, a log or a backup. No agent may read the file
(safety.SECRET_FILES).

Usage (from the project folder):
    python -m agentic_os.mail setup   save the address and the app password, then test the login
    python -m agentic_os.mail check   list the new mail (free, no AI)
    python -m agentic_os.mail facts   the text that the inbox skill gets (free, no AI)
"""

import argparse
import email
import email.policy
import getpass
import html.parser
import imaplib
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from agentic_os import secret_scan
from agentic_os.job_store import DATA_DIR

CONFIG_FILE = DATA_DIR / "mail.json"
HOST = "imap.gmail.com"
PORT = 993
TIMEOUT = 30  # seconds for each network step
HOURS = 24
MAX_MAILS = 30
SNIPPET = 300  # characters of the text of each mail
MAX_BYTES = 2_000_000  # a bigger mail (attachments): only its headers are read
BLANK = "{inbox_facts}"  # the inbox skill's blank; jobs.run_job fills it when the job starts
IMAP = imaplib.IMAP4_SSL  # tests replace it with a fake (no network in tests)
SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
APP_PASSWORD = re.compile(r"[a-z]{16}")  # Google shows it as 4 groups of 4 small letters


class MailError(Exception):
    """The mail cannot be read: no setup, a bad password, no network. The message says what to do."""


def save_config(address, password):
    """Write data/mail.json with mode 0600 (only you can read it)."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(handle, 0o600)  # an old file keeps its old mode with O_CREAT: set it again
    with os.fdopen(handle, "w") as file:
        json.dump({"address": address, "password": password}, file)


def load_config():
    """Return {"address", "password"}. Raise MailError when there is no setup or the file is open to others."""
    if not CONFIG_FILE.exists():
        raise MailError("The mail is not set up. Run: python -m agentic_os.mail setup")
    if CONFIG_FILE.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise MailError(f"Others can read {CONFIG_FILE}. Run: chmod 600 {CONFIG_FILE}")
    try:
        config = json.loads(CONFIG_FILE.read_text())
        return {"address": str(config["address"]), "password": str(config["password"])}
    except (ValueError, KeyError, TypeError) as error:
        raise MailError(f"{CONFIG_FILE} is broken ({error}). Run: python -m agentic_os.mail setup") from error


def connect(config):
    """Log in to Gmail. Return the IMAP connection. The error never shows the password."""
    try:
        box = IMAP(HOST, PORT, timeout=TIMEOUT)
    except OSError as error:
        raise MailError(f"Cannot reach {HOST}: {error}") from error
    try:
        box.login(config["address"], config["password"])
    except imaplib.IMAP4.error as error:
        close(box)
        # Gmail's reply (for example "[AUTHENTICATIONFAILED] Invalid credentials") has no password in it.
        said = one_line(str(error), 200)
        raise MailError(f"Gmail refused the login ({said}). Check the address and the app password "
                        "(python -m agentic_os.mail setup).") from error
    return box


def close(box):
    """Log out; a broken connection is not an error here."""
    try:
        box.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


class TextOnly(html.parser.HTMLParser):
    """Collect the text of an HTML mail (no tags, no scripts, no styles)."""

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip > 0:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def one_line(text, size):
    """Return the text on one line, without control characters, at most size characters.

    A mail cannot close the <mail> tag of the skill's task either (it would look like our text).
    """
    text = " ".join(CONTROL.sub(" ", text).split())
    text = re.sub(r"<(/?)mail>", r"(\1mail)", text, flags=re.IGNORECASE)
    return text if len(text) <= size else text[:size - 3].rstrip() + "..."


def body_text(message):
    """Return the plain text of a mail (from its HTML when it has no plain part)."""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):  # an unknown charset or a broken part
        return ""
    if part.get_content_type() == "text/html":
        parser = TextOnly()
        parser.feed(text)
        text = " ".join(parser.parts)
    return text


def parse(header, raw=None):
    """Return one mail as a dict: sender, subject, date (datetime or None), snippet."""
    message = email.message_from_bytes(raw or header, policy=email.policy.default)
    try:
        date = parsedate_to_datetime(str(message["Date"])) if message["Date"] else None
    except (TypeError, ValueError):
        date = None
    if date is not None and date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    snippet = one_line(body_text(message), SNIPPET) if raw else "(not read: the mail is very big)"
    if secret_scan.find_secret(snippet):
        snippet = "(hidden: the text looks like it holds a key or a password)"
    return {"sender": one_line(str(message["From"] or "?"), 120),
            "subject": one_line(str(message["Subject"] or "(no subject)"), 200),
            "date": date, "snippet": snippet}


def fetch_part(box, number, what):
    """Return (the first line of the reply, the bytes) of one FETCH, or raise MailError."""
    status, data = box.fetch(number, what)
    found = [item for item in data or [] if isinstance(item, tuple)]
    if status != "OK" or not found:
        raise MailError(f"Gmail did not return mail {number.decode()}.")
    return found[0]


def fetch_recent(now=None, hours=HOURS, limit=MAX_MAILS):
    """Return the mails of the inbox from the last hours, newest first, at most limit."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    box = connect(load_config())
    try:
        status, _ = box.select("INBOX", readonly=True)  # readonly: nothing becomes "read"
        if status != "OK":
            raise MailError("Gmail did not open the inbox.")
        # SINCE takes a day, not a time: the exact time is checked below, with the Date header.
        status, data = box.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            raise MailError("Gmail did not answer the search.")
        mails = []
        for number in reversed(data[0].split()):  # the newest have the biggest numbers
            info, header = fetch_part(box, number, "(RFC822.SIZE BODY.PEEK[HEADER])")
            size = SIZE.search(info)
            raw = None
            if size and int(size[1]) <= MAX_BYTES:
                raw = fetch_part(box, number, "(BODY.PEEK[])")[1]
            mail = parse(header, raw)
            if mail["date"] is not None and mail["date"] < since:
                continue
            mails.append(mail)
            if len(mails) == limit:
                break
        return mails
    except (imaplib.IMAP4.error, OSError) as error:
        raise MailError(f"Reading the mail failed: {error}") from error
    finally:
        close(box)


def facts(now=None):
    """Return the new mail as text for the inbox skill. Raise MailError when it cannot be read."""
    mails = fetch_recent(now)
    if not mails:
        return f"No new mail in the last {HOURS} hours."
    lines = [f"New mail in the last {HOURS} hours: {len(mails)} (newest first"
             + (f", only the newest {MAX_MAILS}" if len(mails) == MAX_MAILS else "") + ")."]
    for number, mail in enumerate(mails, 1):
        when = mail["date"].astimezone().strftime("%a %H:%M") if mail["date"] else "?"
        lines += ["", f"{number}. From: {mail['sender']}", f"   Subject: {mail['subject']}",
                  f"   Date: {when}", f"   Text: {mail['snippet']}"]
    return "\n".join(lines)


def setup():
    """Ask for the address and the app password, save them, and test the login."""
    print("You need a Gmail App password: Google account -> Security -> 2-Step Verification -> "
          "App passwords. It is not your normal password.")
    address = input("Gmail address: ").strip()
    password = getpass.getpass("App password (it is not shown while you type): ").replace(" ", "")
    if not address or not password:
        raise SystemExit("Nothing saved: the address and the password are both needed.")
    if not APP_PASSWORD.fullmatch(password):
        # Never print the password, not even a part of it: only what is wrong with it.
        raise SystemExit(f"Nothing saved: an App password is 16 small letters (like \"abcd efgh ijkl mnop\"); "
                         f"you typed {len(password)} characters. Your normal Google password does not work.")
    save_config(address, password)
    print(f"Saved in {CONFIG_FILE} (only you can read it). Testing the login...")
    close(connect(load_config()))
    print("The login works. Write \"check my email\" on the web page.")


def main():
    parser = argparse.ArgumentParser(description="Read the Gmail inbox (read-only).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="save the address and the app password, then test the login")
    commands.add_parser("check", help="list the new mail (free, no AI)")
    commands.add_parser("facts", help="the text that the inbox skill gets (free, no AI)")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup()
        elif args.command == "check":
            mails = fetch_recent()
            for mail in mails:
                when = mail["date"].astimezone().strftime("%m-%d %H:%M") if mail["date"] else "?"
                print(f"{when:<11}  {mail['sender'][:40]:<40}  {mail['subject'][:60]}")
            print(f"{len(mails)} mail(s) in the last {HOURS} hours. Nothing was marked as read.")
        else:
            print(facts())
    except MailError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
