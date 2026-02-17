"""
Google OAuth credentials from macOS Keychain.

Reads tokens stored by google-auth CLI (or any tool that stores
OAuth tokens in Keychain under the "google-oauth" service).

See README.md for setup instructions.
"""

import json
import subprocess

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SERVICE = "google-oauth"
ACCOUNT_TOKEN = "token"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def _keychain_get(account):
    """Get value from keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _keychain_set(account, value):
    """Store value in keychain."""
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE, "-a", account],
        capture_output=True,
    )
    subprocess.run(
        ["security", "add-generic-password", "-s", SERVICE, "-a", account, "-w", value],
        capture_output=True,
    )


def get_credentials(scopes=None):
    """Get Google OAuth credentials from keychain.

    Returns:
        google.oauth2.credentials.Credentials

    Raises:
        RuntimeError if no token in keychain or token expired
    """
    if scopes is None:
        scopes = GMAIL_SCOPES

    token_json = _keychain_get(ACCOUNT_TOKEN)
    if not token_json:
        raise RuntimeError(
            "No token in keychain. See README.md for authentication setup."
        )

    creds = Credentials.from_authorized_user_info(json.loads(token_json), scopes)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _keychain_set(ACCOUNT_TOKEN, creds.to_json())
        else:
            raise RuntimeError("Token expired and can't be refreshed.")

    return creds


def get_gmail_service():
    """Get authenticated Gmail service."""
    creds = get_credentials(GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds)
