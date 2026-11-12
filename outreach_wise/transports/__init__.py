"""Mailbox transports: how a connected inbox sends (and, later, reads) mail.

`for_account` picks the right one for an `accounts` row:
  gmail_api  Gmail / Google Workspace over the Gmail API with OAuth (the original path)
  smtp_imap  any mailbox over SMTP + IMAP with a username and (app) password
  microsoft  Microsoft 365 / Outlook.com over SMTP + IMAP, signed in with OAuth (XOAUTH2)
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
    "reads_imap",
]


def kind(account) -> str:
    return account["transport"] if "transport" in account.keys() else "gmail_api"


def reads_imap(account) -> bool:
    """Replies for this inbox are read over IMAP (not the Gmail API)."""
    return kind(account) in ("smtp_imap", "microsoft")


def for_account(account, secret_key: str, google: Google | None) -> Transport:
    """Raises ValueError if the stored secret can't be decrypted (SECRET_KEY changed) or the
    provider's sign-in isn't configured."""
    if kind(account) == "smtp_imap":
        return SmtpImapTransport(MailboxConfig.from_account(account, secret_key))
    if kind(account) == "microsoft":
        from .. import microsoft  # noqa: PLC0415

        refresh = decrypt(secret_key, account["refresh_token"])
        try:
            client = microsoft.client()
        except microsoft.MicrosoftError as err:
            raise ValueError(str(err)) from err
        return SmtpImapTransport(
            MailboxConfig(
                email=account["email"],
                username=account["email"],
                password="",
                smtp_host=account["smtp_host"] or microsoft.SMTP_HOST,
                smtp_port=account["smtp_port"],
                smtp_security=account["smtp_security"],
                imap_host=account["imap_host"] or microsoft.IMAP_HOST,
                imap_port=account["imap_port"],
                imap_security=account["imap_security"],
                save_sent=False,  # Exchange files mail sent over SMTP itself
                access_token=lambda: client.access_token(refresh),
            )
        )
    if google is None:
        raise ValueError("Gmail inbox but Google OAuth is not configured")
    return GmailTransport(google, decrypt(secret_key, account["refresh_token"]))
