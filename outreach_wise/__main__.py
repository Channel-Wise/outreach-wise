"""Command line entry point: `outreach-wise [serve|worker|demo]`."""

import argparse
import logging
import random
import signal
import sys

from . import __version__, db, sheets
from .config import ConfigError, load_config, load_dotenv
from .google import Google


def _google(cfg):
    return Google(cfg.google_client_id, cfg.google_client_secret, cfg.oauth_redirect_uri)


def serve(cfg) -> None:
    from waitress import serve as waitress_serve

    from .web import create_app
    from .worker import Worker

    google = _google(cfg)
    # Exit cleanly on SIGTERM (docker stop, systemd) so the worker releases its lease.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    worker = Worker(cfg, google) if cfg.run_worker else None
    if worker:
        worker.start()
    logging.getLogger("outreach_wise").info(
        "Outreach Wise %s on http://%s:%s", __version__, cfg.host, cfg.port
    )
    try:
        waitress_serve(
            create_app(cfg, google), host=cfg.host, port=cfg.port, threads=8, ident="outreach-wise"
        )
    finally:
        if worker:
            worker.stop()


def run_worker(cfg) -> None:
    from .worker import Worker

    worker = Worker(cfg, _google(cfg))
    signal.signal(signal.SIGTERM, lambda *_: worker._stop.set())
    try:
        worker.run()
    except KeyboardInterrupt:
        pass


FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Riley", "Casey", "Morgan", "Jamie", "Avery", "Quinn", "Drew", ""]
TOPICS = ["Tech", "Cooking", "Travel", "Fitness", "Gaming", "Finance", "DIY", "Music", "Science", "Comedy"]


def demo(cfg, count: int) -> None:
    """Load fake leads on reserved example.com addresses (never deliverable, never sent live)."""
    rng = random.Random(42)
    rows = [["Email", "First Name", "Channel Name", "Subscribers", "Niche"]]
    for i in range(count):
        topic = rng.choice(TOPICS)
        rows.append(
            [
                f"creator{i + 1}@example.com",
                rng.choice(FIRST),
                f"{rng.choice(FIRST) or 'The'} {topic} Lab",
                str(rng.randint(5, 900) * 1000),
                topic,
            ]
        )
    conn = db.connect(cfg.database_path)
    leads, _ = sheets.parse_rows(rows)
    created, updated = sheets.upsert_leads(conn, leads)
    db.log_event(conn, "sync", f"Demo data: {created} fake leads added (example.com, never sent live)")
    conn.commit()
    conn.close()
    print(f"Added {created} demo leads ({updated} updated). Some have no first name, to show fallbacks.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="outreach-wise", description="Outreach Wise — by Channel Wise")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web UI and the background worker (default)")
    sub.add_parser("worker", help="run only the background worker")
    demo_parser = sub.add_parser("demo", help="load fake demo leads to try a dry run")
    demo_parser.add_argument("--count", type=int, default=60)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    try:
        cfg = load_config()
    except ConfigError as err:
        sys.exit(f"Configuration error: {err}")
    db.init_db(cfg.database_path)

    if args.command == "worker":
        run_worker(cfg)
    elif args.command == "demo":
        demo(cfg, args.count)
    else:
        serve(cfg)


if __name__ == "__main__":
    main()
