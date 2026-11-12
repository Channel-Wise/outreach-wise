"""Gmail API transport: a thin adapter over `google.Google`, behaviour unchanged."""

import base64
from email.message import EmailMessage

from ..google import Google, GoogleError
from .base import SentRef


class GmailTransport:
    def __init__(self, google: Google, refresh_token: str):
        self.google = google
        self.refresh_token = refresh_token

    def send(self, message: EmailMessage, thread_key: str | None = None) -> SentRef:
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self.google.send(self.refresh_token, raw, thread_key)
        return SentRef(sent["id"], sent["threadId"], message["Message-ID"])

    def after_send(self, message: EmailMessage, sent: SentRef) -> tuple[SentRef, str | None]:
        # Gmail may rewrite the Message-ID; follow-ups must reply to the one recipients see.
        try:
            headers = self.google.message(self.refresh_token, sent.provider_id)["headers"]
        except GoogleError:
            return sent, None  # threading still works through threadId
        real = headers.get("message-id") or sent.rfc_message_id
        return SentRef(sent.provider_id, sent.thread_key, real), None

    def verify(self) -> None:
        self.google.access_token(self.refresh_token, force=True)
