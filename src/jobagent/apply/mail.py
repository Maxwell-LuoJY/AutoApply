"""Read verification codes / links from Gmail over IMAP (needs a Gmail app password in .env).

.env:
  GMAIL_ADDRESS=you@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Google Account → Security → 2-Step Verification → App passwords
"""
from __future__ import annotations

import email
import html
import imaplib
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv

from .. import config


class Mailbox:
    def __init__(self, address: str, app_password: str):
        self.address, self.app_password = address, app_password
        self._handed_out: set[str] = set()   # codes/links already used by some worker: never reuse one
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, default_address: str) -> "Mailbox | None":
        load_dotenv(config.ROOT / ".env")
        pw = os.environ.get("GMAIL_APP_PASSWORD")
        return cls(os.environ.get("GMAIL_ADDRESS") or default_address, pw.replace(" ", "")) if pw else None

    def _messages(self, sender: str, since: datetime) -> list[tuple[datetime, str, str]]:
        out = []
        with imaplib.IMAP4_SSL("imap.gmail.com") as m:
            m.login(self.address, self.app_password)
            m.select("INBOX", readonly=True)
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            _, data = m.search(None, f'(SINCE "{day}" FROM "{sender}")')
            for num in (data[0].split() if data and data[0] else [])[-30:]:
                _, msg_data = m.fetch(num, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                try:
                    sent = parsedate_to_datetime(msg["Date"])
                except (TypeError, ValueError):
                    continue
                if sent < since - timedelta(seconds=10):  # only mail triggered by this attempt (small clock slack)
                    continue
                subject = str(make_header(decode_header(msg["Subject"] or "")))
                out.append((sent, subject, _body_text(msg)))
        return sorted(out, reverse=True)

    def confirmations(self, since: datetime) -> list[tuple[datetime, str, str]]:
        """(sent, sender, subject) of application-confirmation emails received since `since`."""
        return _confirmation_headers(self, since)

    def wait_for(self, sender: str, pattern: re.Pattern, since: datetime | None = None, timeout: int = 180,
                 about: str | None = None) -> str | None:
        """Poll for a message from `sender` newer than `since` and return the first regex group found in it.
        `about` (a company name) must appear in the subject or body, so parallel applications can't swap codes."""
        since = since or datetime.now(timezone.utc)
        deadline = time.time() + timeout
        words = [w for w in re.findall(r"[a-z0-9]+", (about or "").lower()) if len(w) > 2][:2]
        while time.time() < deadline:
            for _, subject, body in self._messages(sender, since):
                text = f"{subject}\n{body}".lower()
                if words and not any(w in text for w in words):
                    continue
                for m in pattern.finditer(body):
                    with self._lock:
                        if m.group(1) in self._handed_out:
                            continue
                        self._handed_out.add(m.group(1))
                    return m.group(1)
            time.sleep(10)
        return None


_CONFIRM = re.compile(r"(?i)thank|received|application|applying|applied|submission|submitted")
_NOT_CONFIRM = re.compile(r"(?i)security code|verify|verification|activate|task awaits|job alert|recommended|newsletter")


def _confirmation_headers(mailbox: "Mailbox", since: datetime) -> list[tuple[datetime, str, str]]:
    out = []
    with imaplib.IMAP4_SSL("imap.gmail.com") as m:
        m.login(mailbox.address, mailbox.app_password)
        m.select("INBOX", readonly=True)
        # Gmail reads SINCE dates in its own time zone, so search a day back and filter by the Date header (an
        # evening run in U.S. Central time is already the next day in UTC, and found none of its confirmations).
        _, data = m.search(None, f'(SINCE "{(since - timedelta(days=1)).strftime("%d-%b-%Y")}")')
        for num in (data[0].split() if data and data[0] else [])[-300:]:
            _, d = m.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            msg = email.message_from_bytes(d[0][1])
            try:
                sent = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                continue
            subject = str(make_header(decode_header(msg["Subject"] or ""))).replace("\n", " ")
            sender = str(make_header(decode_header(msg["From"] or "")))
            if sent >= since and _CONFIRM.search(subject) and not _NOT_CONFIRM.search(subject):
                out.append((sent, sender, subject))
    return sorted(out)


def _body_text(msg) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/html":
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            parts.append(re.sub(r"\s+", " ", text))
            parts.extend(re.findall(r'href="([^"]+)"', payload.decode("utf-8", errors="replace")))
        else:
            parts.append(text)
    return "\n".join(parts)
