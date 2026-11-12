"""Mailbox transports: how a connected inbox sends (and, later, reads) mail.

`for_account` picks the right one for an `accounts` row:
  gmail_api  Gmail / Google Workspace over the Gmail API with OAuth (the original path)
  smtp_imap  any mailbox over SMTP + IMAP with a username and (app) password
"""

from ..google import Google
from ..security import decrypt
from .base import AuthError, SendStateUnknown, SentRef, Transport, TransportError
from .gmail import GmailTransport
from .smtp_imap import MailboxConfig, SmtpImapTransport

__all__ = [
    "AuthError",
    "GmailTransport",
    "MailboxConfig",
    "SendStateUnknown",
    "SentRef",
    "SmtpImapTransport",
    "Transport",
    "TransportError",
    "for_account",
]


def kind(account) -> str:
    return account["transport"] if "transport" in account.keys() else "gmail_api"


def for_account(account, secret_key: str, google: Google | None) -> Transport:
    """Raises ValueError if the stored secret can't be decrypted (SECRET_KEY changed)."""
    if kind(account) == "smtp_imap":
        return SmtpImapTransport(MailboxConfig.from_account(account, secret_key))
    if google is None:
        raise ValueError("Gmail inbox but Google OAuth is not configured")
    return GmailTransport(google, decrypt(secret_key, account["refresh_token"]))
