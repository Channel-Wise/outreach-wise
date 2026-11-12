"""SMTP transport against a real in-process SMTP server (aiosmtpd), plus the Sent-copy logic
with a fake IMAP mailbox."""

import asyncio
import socket
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

from outreach_wise.transports import (
    AuthError,
    MailboxConfig,
    SendStateUnknown,
    SmtpImapTransport,
    TransportError,
    smtp_imap,
)


class Handler:
    """Records accepted messages. Recipients' local parts steer the outcome."""

    def __init__(self):
        self.messages = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        local = address.split("@")[0]
        if local == "full":
            return "452 4.2.2 Mailbox full, try later"
        if local == "nobody":
            return "550 5.1.1 No such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        if "slow@" in envelope.rcpt_tos[0]:
            await asyncio.sleep(3)  # longer than the client's timeout: reply never seen
        if "spam@" in envelope.rcpt_tos[0]:
            return "554 5.7.1 Message rejected as spam"
        self.messages.append(envelope)
        return "250 2.0.0 Queued"


def _auth(server, session, envelope, mechanism, auth_data):
    ok = auth_data.login == b"me@acme.test" and auth_data.password == b"app-password"
    return AuthResult(success=ok, handled=False)  # handled=False: aiosmtpd replies 535 itself


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtp_server():
    handler = Handler()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=_free_port(),
        authenticator=_auth,
        auth_require_tls=False,
        server_hostname="localhost",  # skip aiosmtpd's slow FQDN lookup
    )
    controller.start()
    yield controller, handler
    controller.stop()


def _config(controller, password="app-password", **kw) -> MailboxConfig:
    return MailboxConfig(
        email="me@acme.test",
        username="me@acme.test",
        password=password,
        smtp_host=controller.hostname,
        smtp_port=controller.port,
        smtp_security="none",
        **{"timeout": 5, **kw},
    )


def _message(to="lead@client.test", subject="Hello") -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Me <me@acme.test>"
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="acme.test")
    message.set_content("Merhaba, nasılsınız?\n.\nA line that is only a dot must survive.\n")
    return message


def test_send_delivers_and_threads_on_message_id(smtp_server):
    controller, handler = smtp_server
    message = _message()
    sent = SmtpImapTransport(_config(controller)).send(message)
    assert sent.rfc_message_id == message["Message-ID"] == sent.provider_id == sent.thread_key
    assert handler.messages[0].mail_from == "me@acme.test"
    assert handler.messages[0].rcpt_tos == ["lead@client.test"]
    received = message_from_bytes(handler.messages[0].original_content, policy=policy.default)
    assert received["Message-ID"] == message["Message-ID"]
    # Non-ASCII text and a line holding only "." (SMTP's end-of-data marker) arrive intact.
    assert received.get_content().replace("\r\n", "\n") == message.get_content()


def test_follow_up_keeps_the_thread_key(smtp_server):
    controller, _ = smtp_server
    sent = SmtpImapTransport(_config(controller)).send(_message(), thread_key="<first@acme.test>")
    assert sent.thread_key == "<first@acme.test>"


def test_wrong_password_is_auth_error(smtp_server):
    controller, handler = smtp_server
    with pytest.raises(AuthError):
        SmtpImapTransport(_config(controller, password="wrong")).send(_message())
    assert handler.messages == []


def test_temporary_rejection_is_retryable(smtp_server):
    controller, _ = smtp_server
    with pytest.raises(TransportError) as err:
        SmtpImapTransport(_config(controller)).send(_message(to="full@client.test"))
    assert err.value.retryable and not isinstance(err.value, AuthError)


def test_unknown_recipient_is_permanent(smtp_server):
    controller, _ = smtp_server
    with pytest.raises(TransportError) as err:
        SmtpImapTransport(_config(controller)).send(_message(to="nobody@client.test"))
    assert not err.value.retryable


def test_rejection_after_body_is_permanent_not_unknown(smtp_server):
    controller, _ = smtp_server
    with pytest.raises(TransportError) as err:
        SmtpImapTransport(_config(controller)).send(_message(to="spam@client.test"))
    assert not err.value.retryable


def test_lost_reply_after_body_is_unknown(smtp_server):
    controller, _ = smtp_server
    with pytest.raises(SendStateUnknown):
        SmtpImapTransport(_config(controller, timeout=1)).send(_message(to="slow@client.test"))


def test_unreachable_server_is_retryable():
    config = MailboxConfig(
        email="me@acme.test",
        username="me@acme.test",
        password="x",
        smtp_host="127.0.0.1",
        smtp_port=_free_port(),  # nothing listens here
        smtp_security="none",
        timeout=1,
    )
    with pytest.raises(TransportError) as err:
        SmtpImapTransport(config).send(_message())
    assert err.value.retryable


def test_rejects_unknown_security_setting():
    with pytest.raises(ValueError):
        MailboxConfig(email="a@b.c", username="a", password="x", smtp_host="h", smtp_security="tls")


# --- Sent copy -------------------------------------------------------------------------------


class FakeFolder:
    def __init__(self, box):
        self.box = box

    def list(self):
        return self.box.folders

    def set(self, name):
        self.box.selected = name


class FakeMailBox:
    def __init__(self, folders, existing=()):
        from imap_tools import FolderInfo

        self.folders = [FolderInfo(name, "/", flags) for name, flags in folders]
        self.existing = set(existing)
        self.appended = []
        self.selected = None
        self.folder = FakeFolder(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def uids(self, criteria):
        return ["1"] if any(mid in str(criteria) for mid in self.existing) else []

    def append(self, data, folder, flag_set=None):
        self.appended.append((folder, data))


def _imap_transport(monkeypatch, box, **kw):
    config = MailboxConfig(
        email="me@acme.test",
        username="me@acme.test",
        password="x",
        smtp_host="smtp.acme.test",
        imap_host="imap.acme.test",
        **kw,
    )
    transport = SmtpImapTransport(config)
    monkeypatch.setattr(transport, "_imap", lambda: box)
    return transport


def test_sent_copy_goes_to_special_use_folder(monkeypatch):
    box = FakeMailBox([("INBOX", ()), ("Gönderilmiş", ("\\HasNoChildren", "\\Sent"))])
    transport = _imap_transport(monkeypatch, box)
    message = _message()
    sent = smtp_imap.SentRef(message["Message-ID"], message["Message-ID"], message["Message-ID"])
    ref, warning = transport.after_send(message, sent)
    assert warning is None and ref == sent
    assert [folder for folder, _ in box.appended] == ["Gönderilmiş"]


def test_sent_copy_falls_back_to_common_names(monkeypatch):
    box = FakeMailBox([("INBOX", ()), ("Sent Items", ())])
    transport = _imap_transport(monkeypatch, box)
    message = _message()
    sent = smtp_imap.SentRef(message["Message-ID"], message["Message-ID"], message["Message-ID"])
    transport.after_send(message, sent)
    assert [folder for folder, _ in box.appended] == ["Sent Items"]


def test_no_second_copy_if_server_already_filed_it(monkeypatch):
    message = _message()
    box = FakeMailBox([("Sent", ("\\Sent",))], existing=[message["Message-ID"]])
    transport = _imap_transport(monkeypatch, box)
    sent = smtp_imap.SentRef(message["Message-ID"], message["Message-ID"], message["Message-ID"])
    transport.after_send(message, sent)
    assert box.appended == []


def test_providers_that_save_sent_themselves_are_skipped(monkeypatch):
    box = FakeMailBox([("Sent", ("\\Sent",))])
    config = MailboxConfig(
        email="me@gmail.com",
        username="me@gmail.com",
        password="x",
        smtp_host="smtp.gmail.com",
        imap_host="imap.gmail.com",
    )
    transport = SmtpImapTransport(config)
    monkeypatch.setattr(transport, "_imap", lambda: box)
    message = _message()
    transport.after_send(message, smtp_imap.SentRef("a", "a", "a"))
    assert box.appended == []


def test_sent_copy_failure_is_only_a_warning(monkeypatch):
    box = FakeMailBox([("INBOX", ())])  # no Sent folder anywhere
    transport = _imap_transport(monkeypatch, box)
    message = _message()
    sent = smtp_imap.SentRef("a", "a", "a")
    ref, warning = transport.after_send(message, sent)
    assert ref == sent and "Sent folder" in warning


# --- end to end through the send cycle ---------------------------------------------------------


@pytest.fixture
def smtp_account(conn, cfg, smtp_server):
    from outreach_wise import db
    from outreach_wise.security import encrypt

    controller, _ = smtp_server
    conn.execute(
        "INSERT INTO accounts (email, refresh_token, transport, username, secret, smtp_host, smtp_port,"
        " smtp_security, daily_limit, created_at) VALUES (?, '', 'smtp_imap', ?, ?, ?, ?, 'none', 40, ?)",
        (
            "me@acme.test",
            "me@acme.test",
            encrypt(cfg.secret_key, "app-password"),
            controller.hostname,
            controller.port,
            db.iso(db.now()),
        ),
    )
    conn.commit()
    return conn.execute("SELECT * FROM accounts WHERE email = 'me@acme.test'").fetchone()


def test_live_campaign_sends_over_smtp(conn, cfg, smtp_server, smtp_account):
    from conftest import make_campaign

    from outreach_wise import sender

    _, handler = smtp_server
    make_campaign(conn, live=True, leads=1)
    assert sender.run_send_cycle(conn, cfg, None) == 1  # no Google client needed at all

    assert handler.messages[0].rcpt_tos == ["lead1@leads.io"]
    received = message_from_bytes(handler.messages[0].original_content, policy=policy.default)
    assert received["Subject"] == "Hi Name1"
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert row["status"] == "sent"
    assert row["rfc_message_id"] == received["Message-ID"] == row["gmail_thread_id"]
    enrollment = conn.execute("SELECT * FROM enrollments").fetchone()
    assert enrollment["next_step"] == 2 and enrollment["thread_id"] == received["Message-ID"]
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "contacted"


def test_smtp_follow_ups_are_held_until_reply_detection_exists(conn, cfg, clock, smtp_server, smtp_account):
    from conftest import make_campaign

    from outreach_wise import sender

    _, handler = smtp_server
    make_campaign(conn, live=True, leads=1, delay_days=0)
    sender.run_send_cycle(conn, cfg, None)
    clock.advance(hours=1)
    sender.run_send_cycle(conn, cfg, None)
    assert len(handler.messages) == 1
    enrollment = conn.execute("SELECT * FROM enrollments").fetchone()
    assert enrollment["status"] == "stopped" and "detect replies" in enrollment["stop_reason"]


def test_wrong_smtp_password_pauses_the_inbox(conn, cfg, smtp_server, smtp_account):
    from conftest import make_campaign

    from outreach_wise import sender
    from outreach_wise.security import encrypt

    conn.execute("UPDATE accounts SET secret = ?", (encrypt(cfg.secret_key, "wrong"),))
    conn.commit()
    make_campaign(conn, live=True, leads=1)
    assert sender.run_send_cycle(conn, cfg, None) == 0
    assert conn.execute("SELECT enabled FROM accounts").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM messages").fetchone()[0] == "failed"


# --- outreach-wise check-inbox ------------------------------------------------------------------


def _check_inbox(controller, monkeypatch, capsys, password, *extra):
    from outreach_wise.__main__ import main

    monkeypatch.setenv("OUTREACH_WISE_MAILBOX_PASSWORD", password)
    argv = ["check-inbox", "--email", "me@acme.test", "--smtp-host", controller.hostname]
    argv += ["--smtp-port", str(controller.port), "--smtp-security", "none", *extra]
    with pytest.raises(SystemExit) as exit_:
        main(argv)
    return exit_.value.code, capsys.readouterr().out


def test_check_inbox_sends_a_real_test_email(smtp_server, monkeypatch, capsys):
    controller, handler = smtp_server
    code, out = _check_inbox(controller, monkeypatch, capsys, "app-password", "--send-to", "me@acme.test")
    assert code == 0, out
    assert "✓ SMTP login" in out and "✓ Sent a test email to me@acme.test" in out
    assert "won't show up in the Sent folder" in out  # no IMAP server given
    assert handler.messages[0].rcpt_tos == ["me@acme.test"]


def test_check_inbox_reports_a_wrong_password(smtp_server, monkeypatch, capsys):
    controller, handler = smtp_server
    code, out = _check_inbox(controller, monkeypatch, capsys, "nope", "--send-to", "me@acme.test")
    assert code == 1 and "login rejected" in out
    assert handler.messages == []
