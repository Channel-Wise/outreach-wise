"""SMTP/IMAP transport for any mailbox that offers them (Zoho, Yandex, Fastmail, own server,
Gmail with an app password...). Sending uses the standard library's smtplib; IMAP uses
imap-tools (https://github.com/ikvk/imap_tools).

The SMTP conversation is driven step by step (MAIL, RCPT, DATA) instead of `send_message`, so a
failure can be placed exactly relative to the point of no return: once the message body has
been transmitted, the server may have accepted it even if we never see its reply. Anything
after that point is SendStateUnknown and is never retried (see sender.py).
"""

import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

from flufl.bounce import all_failures
from imap_tools import AND, H, MailBox, MailBoxStartTls, MailBoxUnencrypted, MailMessage, MailMessageFlags
from imap_tools.errors import ImapToolsError, MailboxLoginError

from ..security import decrypt
from .base import AuthError, SendStateUnknown, SentRef, TransportError

TIMEOUT = 30  # seconds, per network operation
SECURITY = ("ssl", "starttls", "none")  # "none" is for local test servers only

# These providers file every message submitted over SMTP in Sent themselves; adding our own
# copy would show it twice.
SAVES_SENT_ITSELF = ("smtp.gmail.com", "smtp.googlemail.com", "smtp.office365.com", "smtp-mail.outlook.com")
SENT_FOLDER_NAMES = ("Sent", "Sent Items", "Sent Messages", "Sent Mail", "INBOX.Sent", "[Gmail]/Sent Mail")
# Replies can be filtered into spam, so that folder is read too.
JUNK_FOLDER_NAMES = (
    "Junk",
    "Spam",
    "Junk E-mail",
    "Junk Email",
    "Bulk Mail",
    "INBOX.Junk",
    "INBOX.Spam",
    "[Gmail]/Spam",
)
FETCH_LIMIT = 200  # messages per folder per poll; the rest comes next poll
SNIPPET_CHARS = 1000
MESSAGE_ID = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class MailboxConfig:
    email: str
    username: str
    password: str
    smtp_host: str
    smtp_port: int = 587
    smtp_security: str = "starttls"
    imap_host: str = ""
    imap_port: int = 993
    imap_security: str = "ssl"
    # None: decide from the host (see SAVES_SENT_ITSELF). True/False: always/never add a copy.
    save_sent: bool | None = None
    sent_folder: str = ""  # empty: detect from the \Sent special-use flag or common names
    timeout: float = TIMEOUT

    def __post_init__(self):
        for security in (self.smtp_security, self.imap_security):
            if security not in SECURITY:
                raise ValueError(f"Unknown connection security {security!r}; use one of {SECURITY}")

    @classmethod
    def from_account(cls, account, secret_key: str) -> "MailboxConfig":
        """Build from an `accounts` row; the password is stored encrypted in `secret`."""
        save_sent = account["save_sent"]
        return cls(
            email=account["email"],
            username=account["username"] or account["email"],
            password=decrypt(secret_key, account["secret"]),
            smtp_host=account["smtp_host"],
            smtp_port=account["smtp_port"],
            smtp_security=account["smtp_security"],
            imap_host=account["imap_host"] or "",
            imap_port=account["imap_port"],
            imap_security=account["imap_security"],
            save_sent=None if save_sent is None else bool(save_sent),
            sent_folder=account["sent_folder"] or "",
        )

    @property
    def files_sent_copy(self) -> bool:
        if self.save_sent is not None:
            return self.save_sent
        return self.smtp_host.lower() not in SAVES_SENT_ITSELF


def _failure(code: int, reply: bytes, stage: str) -> TransportError:
    text = reply.decode(errors="replace").strip() if isinstance(reply, bytes) else str(reply)
    message = f"SMTP {stage} rejected: {code} {text}"
    if code in (530, 534, 535, 538):
        return AuthError(message, code)
    # 4xx: temporary, try again later. 5xx: permanent for this recipient/message.
    return TransportError(message, code, retryable=400 <= code < 500)


class SmtpImapTransport:
    def __init__(self, config: MailboxConfig):
        self.config = config

    # --- SMTP ---------------------------------------------------------------------------------

    def _smtp(self) -> smtplib.SMTP:
        """Connected and logged in. Raises AuthError or a retryable TransportError: nothing has
        been sent yet, so every failure here is safe to retry."""
        c = self.config
        try:
            if c.smtp_security == "ssl":
                smtp = smtplib.SMTP_SSL(
                    c.smtp_host, c.smtp_port, timeout=c.timeout, context=ssl.create_default_context()
                )
            else:
                smtp = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=c.timeout)
        except (OSError, smtplib.SMTPException) as err:
            raise TransportError(
                f"Could not connect to {c.smtp_host}:{c.smtp_port}: {err}", retryable=True
            ) from err
        try:
            smtp.ehlo()
            if c.smtp_security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(c.username, c.password)
        except smtplib.SMTPAuthenticationError as err:
            _close(smtp)
            raise AuthError(
                f"SMTP login failed for {c.username}: {err.smtp_code} {_text(err.smtp_error)}", err.smtp_code
            ) from err
        except smtplib.SMTPNotSupportedError as err:
            _close(smtp)
            raise AuthError(
                f"{c.smtp_host} does not offer {err}; check the port and security setting"
            ) from err
        except smtplib.SMTPResponseException as err:
            _close(smtp)
            raise _failure(err.smtp_code, err.smtp_error, "login") from err
        except (OSError, smtplib.SMTPException) as err:
            _close(smtp)
            raise TransportError(f"SMTP handshake with {c.smtp_host} failed: {err}", retryable=True) from err
        return smtp

    def send(self, message: EmailMessage, thread_key: str | None = None) -> SentRef:
        message_id = message["Message-ID"]
        if not message_id:
            raise ValueError("message has no Message-ID")
        sender = parseaddr(message["From"])[1] or self.config.email
        recipients = [addr for _, addr in getaddresses(message.get_all("To", []) + message.get_all("Cc", []))]
        body = message.as_bytes(policy=policy.SMTP)
        smtp = self._smtp()
        try:
            # Before DATA's body goes out the server can't have accepted anything: safe to retry.
            try:
                options = ["BODY=8BITMIME"] if smtp.has_extn("8bitmime") else []
                code, reply = smtp.mail(sender, options)
                if code != 250:
                    raise _failure(code, reply, "MAIL FROM")
                for recipient in recipients:
                    code, reply = smtp.rcpt(recipient)
                    if code not in (250, 251):
                        raise _failure(code, reply, f"recipient {recipient}")
            except (OSError, smtplib.SMTPServerDisconnected) as err:
                raise TransportError(f"Connection lost before sending: {err}", retryable=True) from err
            # DATA. smtplib raises SMTPDataError only if the server refuses to start (no 354);
            # after that, the body is on the wire.
            try:
                code, reply = smtp.data(body)
            except smtplib.SMTPDataError as err:
                raise _failure(err.smtp_code, err.smtp_error, "DATA") from err
            except (OSError, smtplib.SMTPException) as err:
                raise SendStateUnknown(f"connection lost while sending: {err}") from err
            if code != 250:
                # An explicit rejection after the body: the server says it did not take it.
                raise _failure(code, reply, "message")
        finally:
            _close(smtp)
        return SentRef(message_id, thread_key or message_id, message_id)

    # --- IMAP ---------------------------------------------------------------------------------

    def _imap(self) -> MailBox:
        c = self.config
        if not c.imap_host:
            raise TransportError("No IMAP server configured")
        try:
            if c.imap_security == "ssl":
                box = MailBox(c.imap_host, c.imap_port, timeout=c.timeout)
            elif c.imap_security == "starttls":
                box = MailBoxStartTls(c.imap_host, c.imap_port, timeout=c.timeout)
            else:
                box = MailBoxUnencrypted(c.imap_host, c.imap_port, timeout=c.timeout)
        except (OSError, ImapToolsError) as err:
            raise TransportError(
                f"Could not connect to {c.imap_host}:{c.imap_port}: {err}", retryable=True
            ) from err
        try:
            return box.login(c.username, c.password, initial_folder=None)
        except MailboxLoginError as err:
            raise AuthError(f"IMAP login failed for {c.username}") from err
        except (OSError, ImapToolsError) as err:
            raise TransportError(f"IMAP login to {c.imap_host} failed: {err}", retryable=True) from err

    def sent_folder(self, box: MailBox) -> str:
        if self.config.sent_folder:
            return self.config.sent_folder
        folders = box.folder.list()
        for folder in folders:
            if "\\Sent" in folder.flags:
                return folder.name
        names = {folder.name.lower(): folder.name for folder in folders}
        for candidate in SENT_FOLDER_NAMES:
            if candidate.lower() in names:
                return names[candidate.lower()]
        raise TransportError("Could not find the Sent folder; set it in the inbox settings")

    def after_send(self, message: EmailMessage, sent: SentRef) -> tuple[SentRef, str | None]:
        """File a copy in Sent so the conversation shows up in the user's mail client."""
        if not self.config.files_sent_copy or not self.config.imap_host:
            return sent, None
        try:
            with self._imap() as box:
                folder = self.sent_folder(box)
                box.folder.set(folder)
                # Some servers file it themselves after all: never add a second copy.
                if not box.uids(AND(header=H("Message-ID", sent.rfc_message_id))):
                    box.append(message.as_bytes(policy=policy.SMTP), folder, flag_set=[MailMessageFlags.SEEN])
        except (OSError, ImapToolsError, TransportError) as err:
            return sent, f"Sent, but could not save a copy to the Sent folder: {err}"
        return sent, None

    # --- reading ------------------------------------------------------------------------------

    def fetch_new(self, state: dict | None, since: datetime) -> tuple[list[dict], dict]:
        """Messages that arrived in INBOX (and the spam folder) since the last call.

        `state` is what the previous call returned ({folder: {"uidvalidity", "last_uid"}}). With no
        usable state for a folder (first poll, or the server renumbered it), everything since
        `since` is returned; the caller deduplicates by message id. Messages come back in the
        same shape as the Gmail client's (see google._normalize_message), plus `references`,
        `bounced` and `temporary_failure`.
        """
        state = dict(state or {})
        found: list[dict] = []
        try:
            with self._imap() as box:
                for folder in self._read_folders(box):
                    status = box.folder.status(folder, ["UIDVALIDITY", "UIDNEXT"])
                    known = state.get(folder) or {}
                    box.folder.set(folder, readonly=True)
                    if known.get("uidvalidity") == status["UIDVALIDITY"]:
                        # Straight UID FETCH of the range: no SEARCH (cheaper, and some
                        # servers don't support SEARCH UID).
                        last = known["last_uid"]
                        batch = box.fetch(uid_list=f"{last + 1}:*", mark_seen=False, bulk=True)
                    else:
                        last = 0
                        criteria = AND(date_gte=since.date())
                        batch = box.fetch(criteria, mark_seen=False, limit=FETCH_LIMIT, bulk=True)
                    newest, fetched = last, 0
                    for message in batch:
                        fetched += 1
                        uid = int(message.uid)
                        if uid <= last:  # "n:*" always returns the newest message, even if old
                            continue
                        newest = max(newest, uid)
                        found.append(self._normalize(message, folder, status["UIDVALIDITY"]))
                    if last == 0 and fetched < FETCH_LIMIT:
                        # Searched by date and saw everything: continue from the newest UID.
                        newest = max(newest, status["UIDNEXT"] - 1)
                    state[folder] = {"uidvalidity": status["UIDVALIDITY"], "last_uid": newest}
        except (OSError, ImapToolsError, imaplib.IMAP4.error) as err:
            raise TransportError(f"Reading {self.config.imap_host} failed: {err}", retryable=True) from err
        found.sort(key=lambda m: m["internal_date"])
        return found, state

    def _read_folders(self, box: MailBox) -> list[str]:
        folders = box.folder.list()
        junk = next((f.name for f in folders if "\\Junk" in f.flags), None)
        if not junk:
            names = {f.name.lower(): f.name for f in folders}
            junk = next((names[n.lower()] for n in JUNK_FOLDER_NAMES if n.lower() in names), None)
        return ["INBOX"] + ([junk] if junk else [])

    def _normalize(self, message: MailMessage, folder: str, uidvalidity: int) -> dict:
        headers = {name.lower(): " ".join(values) for name, values in message.headers.items()}
        message_id = (MESSAGE_ID.findall(headers.get("message-id", "")) or [""])[0]
        references = MESSAGE_ID.findall(headers.get("in-reply-to", "") + " " + headers.get("references", ""))
        try:
            temporary, permanent = all_failures(message.obj)
        except Exception:  # a parser bug on odd mail must not stop polling
            temporary, permanent = frozenset(), frozenset()
        date = message.date if message.date.year > 1970 else datetime.now(UTC)
        return {
            # Unique per mailbox; stable across polls (Message-ID) or, without one, per UID.
            "id": f"imap:{self.config.email}:{message_id or f'{folder}/{uidvalidity}/{message.uid}'}",
            "thread_id": None,
            "snippet": (message.text or _strip_html(message.html))[:SNIPPET_CHARS],
            "internal_date": int(date.timestamp()),
            "labels": [folder],
            "headers": headers,
            "references": references,
            "bounced": sorted(a.decode(errors="replace").lower() for a in permanent),
            "temporary_failure": bool(temporary) and not permanent,
        }

    # --- checks -------------------------------------------------------------------------------

    def check_smtp(self) -> None:
        """Connect, secure and log in, then disconnect."""
        _close(self._smtp())

    def check_imap(self) -> str | None:
        """Log in; returns the Sent folder a copy would be filed in (None if none is filed)."""
        with self._imap() as box:
            return self.sent_folder(box) if self.config.files_sent_copy else None

    def verify(self) -> None:
        self.check_smtp()
        if self.config.imap_host:
            self.check_imap()


def _strip_html(markup: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup or "")).strip()


def _text(reply) -> str:
    return reply.decode(errors="replace").strip() if isinstance(reply, bytes) else str(reply)


def _close(smtp: smtplib.SMTP) -> None:
    try:
        smtp.quit()
    except (OSError, smtplib.SMTPException):
        smtp.close()
