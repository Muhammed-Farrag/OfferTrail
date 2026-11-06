"""Gmail sending with the minimum OAuth scope (`gmail.send`).

`gmail.send` can send but cannot read the mailbox or manage Gmail drafts, so drafts live in
the local database and a `send_unknown` outcome must be verified by the user in Sent mail.
A successful response means Gmail accepted the message — not that it was delivered or read.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Protocol

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class GmailRejected(Exception):
    """Gmail definitively refused the request (e.g. 400/403). Nothing was sent."""


class GmailUncertain(Exception):
    """Outcome unknown: timeout, connection drop or 5xx after the request left this machine."""


class GmailSender(Protocol):
    def send(self, mime_bytes: bytes) -> str: ...  # returns Gmail message ID


def build_mime(*, sender: str, recipient: str, subject: str, body: str, attachment_path: Path,
               attachment_filename: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(body)
    ctype, _ = mimetypes.guess_type(attachment_filename)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    msg.add_attachment(attachment_path.read_bytes(), maintype=maintype, subtype=subtype,
                       filename=attachment_filename)
    return msg.as_bytes()


class GmailApiSender:
    def __init__(self, token_path: Path, timeout_s: float = 30.0) -> None:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(token_path), [GMAIL_SEND_SCOPE])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            os.chmod(token_path, 0o600)
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._timeout = timeout_s

    def send(self, mime_bytes: bytes) -> str:
        from googleapiclient.errors import HttpError

        raw = base64.urlsafe_b64encode(mime_bytes).decode("ascii")
        try:
            # num_retries=0: never let the client library silently re-send.
            resp = self._service.users().messages().send(userId="me", body={"raw": raw}) \
                .execute(num_retries=0)
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if 400 <= int(status) < 500:
                raise GmailRejected(f"Gmail API {status}: {e.reason}") from e
            raise GmailUncertain(f"Gmail API {status}: {e.reason}") from e
        except Exception as e:  # timeouts, dropped connections, httplib2 errors: unknown
            raise GmailUncertain(f"{type(e).__name__}: {e}") from e
        return resp["id"]


def run_oauth_flow(client_secret_path: Path, token_path: Path) -> None:
    """Interactive local OAuth consent for the send-only scope."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), [GMAIL_SEND_SCOPE])
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(token_path, 0o600)


class FakeGmail:
    """Test double. mode: ok | reject | timeout_after_accept | timeout_before_send."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.accepted: list[bytes] = []  # what "Gmail" actually received

    def send(self, mime_bytes: bytes) -> str:
        if self.mode == "reject":
            raise GmailRejected("Gmail API 400: invalid recipient")
        if self.mode == "timeout_before_send":
            raise GmailUncertain("connect timeout")
        self.accepted.append(mime_bytes)
        if self.mode == "timeout_after_accept":
            raise GmailUncertain("read timeout after request was sent")
        return f"fake-gmail-{len(self.accepted)}"
