# Contributing to Outreach Wise

Thanks for helping! Outreach Wise is intentionally small. The best contributions make it more
reliable, safer or simpler, not bigger.

## Principles

- **Minimal.** One process, one SQLite file, server-rendered pages. New dependencies, services or
  build steps need a strong reason; open an issue first.
- **Plain code.** Plain SQL instead of an ORM; functions before classes. Match the style of the
  surrounding code.
- **Safety first.** Anything touching sending (`sender.py`, `inbox.py`, `campaigns.py`) must keep
  these guarantees, and must come with tests:
  - never send the same step twice, even across crashes and retries;
  - when unsure whether an email went out, stop instead of retrying;
  - dry-run campaigns never call Gmail;
  - replies, bounces, unsubscribes and suppressions always win.
- **No real data.** Never commit credentials, real lead lists, real email addresses or logs.
  Tests and fixtures use reserved domains such as `example.com` or obviously fake ones.

## Development setup

```bash
uv sync
cp .env.example .env
```

Set `SECRET_KEY` and `ADMIN_PASSWORD` in `.env`, then load fake leads and start the app:

```bash
uv run outreach-wise demo
uv run outreach-wise serve
```

The test suite uses an in-memory fake of the Google client and a frozen clock, so it never hits
the network:

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Pull requests

1. Keep PRs focused: one change, with a short description of the *why*.
2. Add or update tests for behavior changes.
3. Update the README if you change configuration, setup or user-facing behavior.
4. Database changes go in a **new** entry at the end of `MIGRATIONS` in `db.py`. Never edit an
   existing migration.
5. Make sure CI (tests + lint) passes.

By contributing you agree that your contributions are licensed under the MIT License.
