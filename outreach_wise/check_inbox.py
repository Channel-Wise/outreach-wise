"""`outreach-wise check-inbox`: try an SMTP/IMAP mailbox for real, without touching the database.

  1. SMTP: connect, TLS, log in
  2. IMAP: connect, log in, find the Sent folder (if a copy will be filed)
  3. optionally (--send-to): send one test email exactly the way a campaign would, then file
     the Sent copy

The password is read from OUTREACH_WISE_MAILBOX_PASSWORD or asked for interactively, never
taken as a command-line argument (it would end up in shell history).
"""

import getpass
import os

from .sender import build_mime
from .transports import AuthError, MailboxConfig, SendStateUnknown, SmtpImapTransport, TransportError

PASSWORD_ENV = "OUTREACH_WISE_MAILBOX_PASSWORD"


def add_arguments(parser) -> None:
    parser.add_argument("--email", required=True, help="the mailbox address, e.g. me@acme.com")
    parser.add_argument("--username", help="login name if it differs from --email")
    parser.add_argument("--smtp-host", required=True)
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--smtp-security", choices=["starttls", "ssl", "none"], default="starttls")
    parser.add_argument("--imap-host", default="", help="leave out to skip the IMAP checks")
    parser.add_argument("--imap-port", type=int, default=993)
    parser.add_argument("--imap-security", choices=["ssl", "starttls", "none"], default="ssl")
    parser.add_argument("--sent-folder", default="", help="default: detected")
    parser.add_argument(
        "--save-sent",
        choices=["auto", "yes", "no"],
        default="auto",
        help="file a copy in Sent (auto: by host)",
    )
    parser.add_argument("--send-to", help="also send one real test email to this address (use your own)")


class Failed(Exception):
    pass


def _step(out, label, action):
    try:
        result = action()
    except AuthError as err:
        out(f"✗ {label}: login rejected: {err}")
        raise Failed from None
    except (TransportError, SendStateUnknown, OSError) as err:
        out(f"✗ {label}: {err}")
        raise Failed from None
    out(f"✓ {label}" + (f" ({result})" if isinstance(result, str) else ""))
    return result


def run(args, out=print) -> int:
    """Returns the process exit code: 0 if every step worked."""
    password = os.environ.get(PASSWORD_ENV) or getpass.getpass(
        f"Password for {args.username or args.email}: "
    )
    config = MailboxConfig(
        email=args.email,
        username=args.username or args.email,
        password=password,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_security=args.smtp_security,
        imap_host=args.imap_host,
        imap_port=args.imap_port,
        imap_security=args.imap_security,
        save_sent={"auto": None, "yes": True, "no": False}[args.save_sent],
        sent_folder=args.sent_folder,
    )
    transport = SmtpImapTransport(config)
    try:
        _step(
            out,
            f"SMTP login to {config.smtp_host}:{config.smtp_port} ({config.smtp_security})",
            transport.check_smtp,
        )
        if config.imap_host:
            folder = _step(
                out,
                f"IMAP login to {config.imap_host}:{config.imap_port} ({config.imap_security})",
                transport.check_imap,
            )
            if folder:
                out(f"✓ Sent folder: {folder!r}")
        if not config.files_sent_copy:
            out(f"· {config.smtp_host} files sent mail itself; no extra copy will be added")
        elif not config.imap_host:
            out("· No IMAP server given: sent mail won't show up in the Sent folder")

        if args.send_to:
            message, message_id = build_mime(
                from_email=config.email,
                from_name="",
                to=args.send_to,
                subject="Outreach Wise test email",
                body="This is a test from `outreach-wise check-inbox`. If you can read it, sending works.\n",
            )
            sent = _step(out, f"Sent a test email to {args.send_to}", lambda: transport.send(message))
            _, warning = transport.after_send(message, sent)
            if warning:
                out(f"✗ {warning}")
            elif config.files_sent_copy and config.imap_host:
                out("✓ Copy filed in Sent")
            out(f"\nNow check {args.send_to} (inbox and spam) for the message with Message-ID {message_id}.")
            if warning:
                return 1
    except Failed:
        return 1
    return 0
