# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Report them privately through
GitHub: **Security → Report a vulnerability** on
[Channel-Wise/outreach-wise](https://github.com/Channel-Wise/outreach-wise/security/advisories/new).

We aim to acknowledge reports within a few working days.

## How Outreach Wise handles secrets

- Google refresh tokens are encrypted at rest (Fernet) with a key derived from `SECRET_KEY`.
- Sessions and unsubscribe links are signed with `SECRET_KEY`. All forms are CSRF-protected.
- Secrets come from environment variables; `.env` and the database are git-ignored.
- Content of inbox messages unrelated to your leads is never stored (only the message id, so
  it isn't processed twice).

## Operator responsibilities

- Use a long random `SECRET_KEY` and a strong `ADMIN_PASSWORD`.
- Serve the app over HTTPS (reverse proxy) when it's reachable from the internet.
- Protect and back up the SQLite file. It contains your leads and encrypted tokens.
- Revoke access at <https://myaccount.google.com/permissions> when you remove an inbox.
