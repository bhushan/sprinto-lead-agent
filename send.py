import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import config


def can_send():
    """Real sending needs a Gmail app password and your own address; the draft only ever goes to that address."""
    return bool(os.environ.get("GMAIL_APP_PASSWORD")) and not config.TO_ADDRESS.endswith("@example.com")


def deliver(subject, body):
    """Mail a draft to your own address (never to the prospect). Raises if Gmail refuses."""
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = config.TO_ADDRESS, config.TO_ADDRESS, " ".join(subject.split())
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
        s.login(config.TO_ADDRESS, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def send(domain, subject, body, approved=False):
    """Step 7. Writes to outbox/ unless running live AND approved (run.py --send)."""
    if config.offline() or not approved:
        Path("outbox").mkdir(exist_ok=True)
        Path(f"outbox/{domain}.txt").write_text(
            f"To: {config.TO_ADDRESS}\nSubject: {subject}\n\n{body}\n", encoding="utf-8")
        return "drafted to outbox/ (not sent)"
    if not can_send():
        raise SystemExit("Set TO_ADDRESS (your own address) and GMAIL_APP_PASSWORD in .env before sending.")
    deliver(subject, body)
    return "sent"
