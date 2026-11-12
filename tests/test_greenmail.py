"""End-to-end over real SMTP + IMAP, against GreenMail (https://greenmail-mail-test.github.io/).

Skipped unless a GreenMail server is reachable. Start one with:

    docker run -d --rm --name ow-greenmail -p 127.0.0.1:3025:3025 -p 127.0.0.1:3143:3143 \\
      -e GREENMAIL_OPTS="-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap \\
         -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled" greenmail/standalone:2.1.15

GreenMail accepts any login and delivers every address locally, so each test uses a fresh
mailbox and plays both sides: our inbox, and the lead who replies or whose server bounces.
"""

import os
import smtplib
import socket
import uuid
from email.message import EmailMessage
from email.utils import make_msgid

import pytest
from conftest import make_campaign
from imap_tools import MailBoxUnencrypted

from outreach_wise import db, inbox, sender
from outreach_wise.security import encrypt

HOST = os.environ.get("GREENMAIL_HOST", "127.0.0.1")
SMTP_PORT, IMAP_PORT = 3025, 3143


def _reachable() -> bool:
    try:
        socket.create_connection((HOST, IMAP_PORT), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="GreenMail is not running (see module docstring)")


@pytest.fixture
def mailbox():
    address = f"me-{uuid.uuid4().hex[:8]}@acme.test"
    with MailBoxUnencrypted(HOST, IMAP_PORT).login(address, "pw", initial_folder=None) as box:
        box.folder.create("Sent")  # GreenMail starts with INBOX only
    return address


@pytest.fixture
def imap_account(conn, cfg, mailbox):
    conn.execute(
        "INSERT INTO accounts (email, refresh_token, transport, secret, smtp_host, smtp_port, smtp_security,"
        " imap_host, imap_port, imap_security, daily_limit, created_at)"
        " VALUES (?, '', 'smtp_imap', ?, ?, ?, 'none', ?, ?, 'none', 40, ?)",
        (mailbox, encrypt(cfg.secret_key, "pw"), HOST, SMTP_PORT, HOST, IMAP_PORT, db.iso(db.now())),
    )
    conn.commit()
    return conn.execute("SELECT * FROM accounts WHERE email = ?", (mailbox,)).fetchone()


def _deliver(message: EmailMessage) -> None:
    with smtplib.SMTP(HOST, SMTP_PORT) as smtp:
        smtp.send_message(message)


def _reply(to: str, in_reply_to: str, body="Sounds good, let's talk Tuesday.") -> None:
    message = EmailMessage()
    message["From"] = "Name1 <lead1@leads.io>"
    message["To"] = to
    message["Subject"] = "Re: Hi Name1"
    message["Message-ID"] = make_msgid(domain="leads.io")
    message["In-Reply-To"] = in_reply_to
    message["References"] = in_reply_to
    message.set_content(body)
    _deliver(message)


def _bounce(to: str, failed: str) -> None:
    """A standard RFC 3464 delivery status notification, as Postfix sends it."""
    raw = f"""From: MAILER-DAEMON@mx.leads.io (Mail Delivery System)
To: {to}
Subject: Undelivered Mail Returned to Sender
Message-ID: {make_msgid(domain="mx.leads.io")}
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="b1"

--b1
Content-Type: text/plain

I'm sorry to have to inform you that your message could not be delivered.

--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.leads.io

Final-Recipient: rfc822; {failed}
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown

--b1--
"""
    with smtplib.SMTP(HOST, SMTP_PORT) as smtp:
        smtp.sendmail("mailer-daemon@mx.leads.io", [to], raw.replace("\n", "\r\n").encode())


def _sent_message_id(conn) -> str:
    return conn.execute("SELECT rfc_message_id FROM messages WHERE status = 'sent'").fetchone()[0]


def test_send_files_a_copy_in_sent(conn, cfg, clock, imap_account, mailbox):
    make_campaign(conn, live=True, leads=1)
    assert sender.run_send_cycle(conn, cfg, None) == 1
    with MailBoxUnencrypted(HOST, IMAP_PORT).login(mailbox, "pw", initial_folder="Sent") as box:
        copies = list(box.fetch(mark_seen=False))
    assert [m.subject for m in copies] == ["Hi Name1"]
    assert copies[0].headers["message-id"][0].strip() == _sent_message_id(conn)


def test_reply_over_imap_stops_the_sequence(conn, cfg, clock, imap_account, mailbox):
    make_campaign(conn, live=True, leads=1, delay_days=0)
    sender.run_send_cycle(conn, cfg, None)
    _reply(mailbox, _sent_message_id(conn))

    counts = inbox.poll(conn, cfg, None, imap_account)
    assert counts == {"reply": 1}
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "replied"
    enrollment = conn.execute("SELECT * FROM enrollments").fetchone()
    assert enrollment["status"] == "replied"
    assert conn.execute("SELECT replied_at FROM messages").fetchone()[0] is not None

    # The follow-up never goes out, and polling again finds nothing new.
    clock.advance(hours=1)
    assert sender.run_send_cycle(conn, cfg, None) == 0
    assert inbox.poll(conn, cfg, None, imap_account) == {}


def test_follow_up_checks_imap_right_before_sending(conn, cfg, clock, imap_account, mailbox):
    """No poll in between: the reply must still win over the due follow-up."""
    make_campaign(conn, live=True, leads=1, delay_days=0)
    sender.run_send_cycle(conn, cfg, None)
    _reply(mailbox, _sent_message_id(conn))
    clock.advance(hours=1)
    sender.run_send_cycle(conn, cfg, None)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'sent'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "replied"


def test_follow_up_is_sent_in_thread_when_nobody_replied(conn, cfg, clock, imap_account, mailbox):
    make_campaign(conn, live=True, leads=1, delay_days=0)
    sender.run_send_cycle(conn, cfg, None)
    first = _sent_message_id(conn)
    clock.advance(hours=1)
    assert sender.run_send_cycle(conn, cfg, None) == 1
    with MailBoxUnencrypted(HOST, IMAP_PORT).login("lead1@leads.io", "pw") as box:
        follow_up = [m for m in box.fetch(mark_seen=False) if m.subject.startswith("Re:")][-1]
    assert follow_up.headers["in-reply-to"][0].strip() == first


def test_bounce_over_imap_suppresses_the_lead(conn, cfg, clock, imap_account, mailbox):
    make_campaign(conn, live=True, leads=1)
    sender.run_send_cycle(conn, cfg, None)
    _bounce(mailbox, "lead1@leads.io")

    assert inbox.poll(conn, cfg, None, imap_account) == {"bounce": 1}
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "bounced"
    assert db.is_suppressed(conn, "lead1@leads.io")


def test_unrelated_mail_is_ignored(conn, cfg, clock, imap_account, mailbox):
    message = EmailMessage()
    message["From"] = "newsletter@shop.test"
    message["To"] = mailbox
    message["Subject"] = "Big sale"
    message["Message-ID"] = make_msgid(domain="shop.test")
    message.set_content("50% off")
    _deliver(message)
    assert inbox.poll(conn, cfg, None, imap_account) == {"ignored": 1}
    assert tuple(conn.execute("SELECT from_email, snippet FROM inbound").fetchone()) == (None, None)
