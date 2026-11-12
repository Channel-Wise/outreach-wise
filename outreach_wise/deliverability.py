"""Weekly domain health check: is every sending domain set up so mail lands in the inbox?

For each domain we send from, checks the DNS records receiving servers use to decide whether
to trust our mail. These outweigh content by far: the same email scored -0.9 sent through a
server the domain's SPF authorizes, and +9.4 through one it doesn't (rspamd, Nov 2026).

  SPF    exists, is valid, and authorizes the provider we actually send through
  DMARC  exists; p=none only monitors, quarantine/reject protects the domain
  DKIM   a public key is published for the provider's selector

SPF and DMARC parsing comes from checkdmarc (https://pypi.org/project/checkdmarc/); DKIM is a
plain TXT lookup. Results are kept in kv["deliverability"] and problems go to the activity log.
"""

import sqlite3
from dataclasses import dataclass

import checkdmarc
import dns.exception
import dns.resolver

from . import db, transports

INTERVAL_MINUTES = 7 * 24 * 60


@dataclass(frozen=True)
class Provider:
    name: str
    hosts: tuple[str, ...]  # substrings of the SMTP host
    spf_include: str  # substring of an SPF include that authorizes its servers
    dkim_selectors: tuple[str, ...]


PROVIDERS = (
    Provider("Google", ("gmail.com", "googlemail.com", "google"), "_spf.google.com", ("google",)),
    Provider(
        "Microsoft 365", ("office365", "outlook"), "spf.protection.outlook.com", ("selector1", "selector2")
    ),
    Provider("Zoho", ("zoho",), "zoho", ("zmail", "zoho", "default")),
    Provider("Yandex", ("yandex",), "_spf.yandex.net", ("mail",)),
    Provider("Fastmail", ("fastmail", "messagingengine"), "spf.messagingengine.com", ("fm1", "fm2", "fm3")),
)
GENERIC_SELECTORS = ("default", "mail", "dkim", "k1", "s1", "selector1", "google")


def provider_for(account) -> Provider | None:
    if transports.kind(account) == "gmail_api":
        return PROVIDERS[0]
    host = (account["smtp_host"] or "").lower()
    return next((p for p in PROVIDERS if any(h in host for h in p.hosts)), None)


def _includes(parsed: dict | None) -> list[str]:
    """Every include/redirect target in an SPF record, recursively."""
    found = []
    for mechanism in (parsed or {}).get("mechanisms", []):
        if mechanism.get("mechanism") in ("include", "redirect"):
            found.append(str(mechanism.get("value", "")).lower())
            found.extend(_includes(mechanism.get("parsed")))
    redirect = (parsed or {}).get("redirect")
    if isinstance(redirect, dict):
        found.append(str(redirect.get("domain", "")).lower())
        found.extend(_includes(redirect.get("parsed")))
    return found


def _dkim(domain: str, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        try:
            answers = dns.resolver.resolve(f"{selector}._domainkey.{domain}", "TXT", lifetime=5)
        except dns.exception.DNSException:
            continue
        if any("p=" in b"".join(r.strings).decode(errors="replace") for r in answers):
            return selector
    return None


def check_domain(domain: str, providers: list[Provider | None]) -> list[dict]:
    """[{check, status: ok|warn|fail, detail}] for one sending domain."""
    results = []

    spf = checkdmarc.check_spf(domain, timeout=5.0, retries=1)
    if not spf.get("record"):
        results.append(
            {
                "check": "SPF",
                "status": "fail",
                "detail": "No SPF record: receivers can't tell which servers may send for this domain.",
            }
        )
    elif not spf.get("valid"):
        results.append(
            {"check": "SPF", "status": "fail", "detail": f"SPF record is invalid: {spf.get('error')}"}
        )
    else:
        includes = _includes(spf.get("parsed"))
        for provider in {p for p in providers if p}:
            if any(provider.spf_include in include for include in includes):
                results.append({"check": "SPF", "status": "ok", "detail": f"Authorizes {provider.name}."})
            else:
                results.append(
                    {
                        "check": "SPF",
                        "status": "fail",
                        "detail": f"Doesn't authorize {provider.name}: mail sent through it fails SPF and "
                        f"is likely to land in spam. Add include:{provider.spf_include} to the SPF record.",
                    }
                )
        if None in providers:
            results.append(
                {
                    "check": "SPF",
                    "status": "warn",
                    "detail": "Can't verify that SPF covers this SMTP server (unknown provider).",
                }
            )
        for warning in spf.get("warnings", []):
            results.append({"check": "SPF", "status": "warn", "detail": warning})

    dmarc = checkdmarc.check_dmarc(domain, timeout=5.0, retries=1)
    if not dmarc.get("record"):
        results.append(
            {
                "check": "DMARC",
                "status": "fail",
                "detail": "No DMARC record. Gmail and Yahoo require one for bulk senders.",
            }
        )
    else:
        policy = dmarc.get("tags", {}).get("p", {}).get("value", "none")
        if policy == "none":
            results.append(
                {
                    "check": "DMARC",
                    "status": "warn",
                    "detail": "Policy is p=none (monitoring only). Once SPF and DKIM pass reliably, move to quarantine.",
                }
            )
        else:
            results.append({"check": "DMARC", "status": "ok", "detail": f"Policy is p={policy}."})

    selectors = tuple(dict.fromkeys(s for p in providers if p for s in p.dkim_selectors)) + GENERIC_SELECTORS
    selector = _dkim(domain, tuple(dict.fromkeys(selectors)))
    if selector:
        results.append({"check": "DKIM", "status": "ok", "detail": f"Key published (selector {selector!r})."})
    else:
        results.append(
            {
                "check": "DKIM",
                "status": "warn",
                "detail": "No DKIM key found under the usual selectors. Turn on DKIM signing at your email provider.",
            }
        )
    return results


def run(conn: sqlite3.Connection) -> dict:
    """Check every domain we send from; store the report and log what needs attention."""
    accounts = conn.execute("SELECT * FROM accounts WHERE enabled = 1").fetchall()
    by_domain: dict[str, list[Provider | None]] = {}
    for account in accounts:
        by_domain.setdefault(db.email_domain(account["email"]), []).append(provider_for(account))
    report = {"checked_at": db.iso(db.now()), "domains": {}}
    for domain, providers in sorted(by_domain.items()):
        if domain in ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com"):
            continue  # consumer mailboxes: the provider owns these records
        try:
            results = check_domain(domain, providers)
        except Exception as err:  # DNS trouble must not stop the worker
            results = [{"check": "DNS", "status": "warn", "detail": f"Check failed: {err}"}]
        report["domains"][domain] = results
        problems = [r for r in results if r["status"] != "ok"]
        for problem in problems:
            db.log_event(
                conn,
                "deliverability",
                f"{domain} {problem['check']}: {problem['detail']}",
                level="error" if problem["status"] == "fail" else "warning",
            )
        if not problems:
            db.log_event(conn, "deliverability", f"{domain}: SPF, DKIM and DMARC look good.")
    db.set_kv(conn, "deliverability", report)
    conn.commit()
    return report
