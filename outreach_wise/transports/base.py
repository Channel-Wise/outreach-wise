"""What every mailbox transport (Gmail API, SMTP/IMAP, ...) offers the sender, and the errors it raises.

The error classes are the ones the send cycle already understands (`sender.process`), so a new
transport plugs into the existing duplicate-send protection without changing it:

  AuthError         credentials no longer work → the inbox is paused until reconnected
  TransportError    the send definitely did not happen; `retryable` says whether to try later
  SendStateUnknown  the message may have gone out → never retried, sequence stopped for review

They live in `google.py` for historical reasons and are re-exported here under generic names.
"""

from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from ..google import AuthError, SendStateUnknown
from ..google import GoogleError as TransportError

__all__ = ["AuthError", "SendStateUnknown", "SentRef", "Transport", "TransportError"]


@dataclass(frozen=True)
class SentRef:
    """Where a sent message can be found again.

    provider_id    the provider's own id (Gmail message id; for SMTP, the Message-ID)
    thread_key     what follow-ups thread onto (Gmail threadId; for SMTP, the first Message-ID)
    rfc_message_id the Message-ID header, used for In-Reply-To / References
    """

    provider_id: str
    thread_key: str
    rfc_message_id: str


class Transport(Protocol):
    def send(self, message: EmailMessage, thread_key: str | None = None) -> SentRef:
        """Hand the message to the provider. Does nothing else, so the caller can record the
        result immediately. Raises AuthError, TransportError or SendStateUnknown."""
        ...

    def after_send(self, message: EmailMessage, sent: SentRef) -> tuple[SentRef, str | None]:
        """Best-effort work once the send is recorded (fetch the final Message-ID, file a copy in
        Sent...). Never raises; returns the possibly updated ref and a warning, if any."""
        ...

    def verify(self) -> None:
        """Check the credentials and settings work. Raises AuthError or TransportError."""
        ...
