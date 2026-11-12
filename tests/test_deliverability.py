import checkdmarc
import pytest

from outreach_wise import db, deliverability, worker
from outreach_wise.security import encrypt

GOOGLE_SPF = {
    "record": "v=spf1 include:_spf.google.com ~all",
    "valid": True,
    "warnings": [],
    "parsed": {
        "mechanisms": [{"mechanism": "include", "value": "_spf.google.com", "parsed": {"mechanisms": []}}]
    },
}


@pytest.fixture
def dns_records(monkeypatch):
    records = {
        "spf": GOOGLE_SPF,
        "dmarc": {"record": "v=DMARC1; p=quarantine", "tags": {"p": {"value": "quarantine"}}},
        "dkim": "google",
    }
    monkeypatch.setattr(checkdmarc, "check_spf", lambda domain, **kw: records["spf"])
    monkeypatch.setattr(checkdmarc, "check_dmarc", lambda domain, **kw: records["dmarc"])
    monkeypatch.setattr(deliverability, "_dkim", lambda domain, selectors: records["dkim"])
    return records


def _statuses(results):
    return [(r["check"], r["status"]) for r in results]


def test_well_configured_domain_passes(dns_records):
    results = deliverability.check_domain("acme.com", [deliverability.PROVIDERS[0]])
    assert _statuses(results) == [("SPF", "ok"), ("DMARC", "ok"), ("DKIM", "ok")]


def test_spf_that_does_not_cover_the_smtp_provider_fails(dns_records):
    zoho = deliverability.PROVIDERS[2]
    results = deliverability.check_domain("acme.com", [zoho])
    spf = [r for r in results if r["check"] == "SPF"]
    assert spf[0]["status"] == "fail" and "include:zoho" in spf[0]["detail"]


def test_missing_records_and_monitor_only_dmarc(dns_records):
    dns_records.update(
        spf={"record": None, "valid": False},
        dmarc={"record": "v=DMARC1; p=none", "tags": {"p": {"value": "none"}}},
        dkim=None,
    )
    results = deliverability.check_domain("acme.com", [deliverability.PROVIDERS[0]])
    assert _statuses(results) == [("SPF", "fail"), ("DMARC", "warn"), ("DKIM", "warn")]


def test_nested_includes_are_followed():
    parsed = {
        "mechanisms": [
            {
                "mechanism": "include",
                "value": "spf.acme-mail.com",
                "parsed": {"mechanisms": [{"mechanism": "include", "value": "_spf.google.com"}]},
            }
        ]
    }
    assert deliverability._includes(parsed) == ["spf.acme-mail.com", "_spf.google.com"]


def test_provider_is_detected_from_the_smtp_host(conn, account):
    assert deliverability.provider_for(account).name == "Google"
    row = {"transport": "smtp_imap", "smtp_host": "smtp.zoho.eu"}

    class Row(dict):
        def keys(self):
            return super().keys()

    assert deliverability.provider_for(Row(row)).name == "Zoho"
    assert deliverability.provider_for(Row(row, smtp_host="mail.acme.com")) is None


def test_run_stores_report_and_logs_problems(conn, cfg, account, dns_records):
    dns_records["dkim"] = None
    conn.execute(
        "INSERT INTO accounts (email, refresh_token, transport, secret, smtp_host, created_at)"
        " VALUES ('me@gmail.com', '', 'smtp_imap', ?, 'smtp.gmail.com', ?)",
        (encrypt(cfg.secret_key, "x"), db.iso(db.now())),
    )
    report = deliverability.run(conn)
    assert list(report["domains"]) == ["sender.io"]  # gmail.com is the provider's business
    events = conn.execute("SELECT level, message FROM events WHERE kind = 'deliverability'").fetchall()
    assert [(e["level"], e["message"].split(":")[0]) for e in events] == [("warning", "sender.io DKIM")]
    assert db.get_kv(conn, "deliverability")["domains"]["sender.io"][0]["status"] == "ok"


def test_worker_runs_it_weekly(conn, cfg, google, account, clock, dns_records):
    worker.tick(conn, cfg, google)
    first = db.get_kv(conn, "deliverability")["checked_at"]
    clock.advance(days=3)
    worker.tick(conn, cfg, google)
    assert db.get_kv(conn, "deliverability")["checked_at"] == first
    clock.advance(days=5)
    worker.tick(conn, cfg, google)
    assert db.get_kv(conn, "deliverability")["checked_at"] != first
