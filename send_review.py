#!/usr/bin/env python3
"""Send review email (non-interactive, for cron jobs)."""

import base64
import smtplib
import sys
from email.mime.text import MIMEText

import markdown

from config import load_config

STYLE = """\
<style>
body { font-family: -apple-system, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; color: #333; }
h1 { font-size: 1.4em; border-bottom: 2px solid #333; padding-bottom: 6px; }
h2 { font-size: 1.1em; margin-top: 1.5em; color: #555; }
h3 { font-size: 1em; margin-top: 1.2em; }
ul { padding-left: 1.5em; }
li { margin-bottom: 4px; }
code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 0.9em; }
hr { border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }
</style>
"""


def md_to_html(body: str, html_prefix: str = "") -> str:
    """Convert markdown body to styled HTML, with optional pre-rendered HTML prefix."""
    html_body = markdown.markdown(body, extensions=["fenced_code", "tables"])
    return f"<html><head>{STYLE}</head><body>{html_prefix}{html_body}</body></html>"


def send_gmail(to: str, subject: str, body: str, html_prefix: str = ""):
    """Send email via Gmail API (macOS Keychain auth)."""
    from keychain_auth import get_gmail_service

    service = get_gmail_service()

    message = MIMEText(md_to_html(body, html_prefix), "html")
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()

    print(f"Email sent! Message ID: {result['id']}")
    return result


def send_smtp(
    to: str, subject: str, body: str, smtp_config: dict, html_prefix: str = ""
):
    """Send email via SMTP."""
    import os

    message = MIMEText(md_to_html(body, html_prefix), "html")
    message["to"] = to
    message["subject"] = subject
    message["from"] = smtp_config["username"]

    password = os.environ.get(smtp_config.get("password_env", "SMTP_PASSWORD"))
    if not password:
        raise RuntimeError(
            f"Set {smtp_config.get('password_env', 'SMTP_PASSWORD')} env var"
        )

    with smtplib.SMTP(smtp_config["host"], smtp_config.get("port", 587)) as server:
        server.starttls()
        server.login(smtp_config["username"], password)
        server.send_message(message)

    print(f"Email sent via SMTP to {to}")


def send_email(to: str, subject: str, body: str, html_prefix: str = ""):
    """Send email using configured method."""
    cfg = load_config()
    method = cfg["email_method"]

    if method == "gmail":
        send_gmail(to, subject, body, html_prefix)
    elif method == "smtp":
        if not cfg.get("smtp"):
            raise RuntimeError(
                "smtp settings required in config.yaml for email_method: smtp"
            )
        send_smtp(to, subject, body, cfg["smtp"], html_prefix)
    else:
        raise RuntimeError(f"Unknown email_method: {method}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: send_review.py <to> <subject> <body>")
        sys.exit(1)

    send_email(sys.argv[1], sys.argv[2], sys.argv[3])
