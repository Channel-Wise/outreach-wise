"""Find a mailbox's SMTP/IMAP settings from its address, the way Thunderbird does.

Lookup order (https://wiki.mozilla.org/Thunderbird:Autoconfiguration):
  1. the domain's own autoconfig file (autoconfig.<domain>, /.well-known/autoconfig)
  2. Mozilla's ISPDB (https://autoconfig.thunderbird.net/v1.1/<domain>)
  3. the ISPDB entry of the domain that hosts its MX (custom domains on Google Workspace,
     Microsoft 365, Zoho...)

Only the domain is sent to Mozilla; the full address goes only to the domain's own server.
"""

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass

import dns.exception
import dns.resolver
import httpx

ISPDB = "https://autoconfig.thunderbird.net/v1.1/"
MAX_BYTES = 256 * 1024
SOCKET_TYPES = {"SSL": "ssl", "STARTTLS": "starttls", "plain": "none"}


@dataclass
class Settings:
    provider: str
    username: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    imap_host: str = ""
    imap_port: int = 993
    imap_security: str = "ssl"
    # The provider advertises only OAuth sign-in. Some still take an app password (Fastmail);
    # Microsoft is retiring passwords for SMTP (end of 2026).
    oauth_preferred: bool = False
    source: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _fill(template: str, email: str) -> str:
    local, _, domain = email.partition("@")
    return (
        template.replace("%EMAILADDRESS%", email)
        .replace("%EMAILLOCALPART%", local)
        .replace("%EMAILDOMAIN%", domain)
    )


def _server(provider: ET.Element, tag: str, kind: str) -> ET.Element | None:
    """The first server of `kind`, preferring encrypted ones."""
    servers = [s for s in provider.findall(tag) if s.get("type") == kind]
    servers.sort(key=lambda s: (s.findtext("socketType") or "") == "plain")
    return servers[0] if servers else None


def parse(xml: bytes, email: str, source: str = "") -> Settings | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    provider = root.find("emailProvider")
    if provider is None:
        return None
    outgoing = _server(provider, "outgoingServer", "smtp")
    if outgoing is None:
        return None
    incoming = _server(provider, "incomingServer", "imap")
    auth = {a.text for a in outgoing.findall("authentication")}
    settings = Settings(
        provider=provider.findtext("displayShortName")
        or provider.findtext("displayName")
        or provider.get("id", ""),
        username=_fill(outgoing.findtext("username") or "%EMAILADDRESS%", email),
        smtp_host=_fill(outgoing.findtext("hostname") or "", email),
        smtp_port=int(outgoing.findtext("port") or 587),
        smtp_security=SOCKET_TYPES.get(outgoing.findtext("socketType") or "", "starttls"),
        oauth_preferred=bool(auth) and auth <= {"OAuth2"},
        source=source,
    )
    if incoming is not None:
        settings.imap_host = _fill(incoming.findtext("hostname") or "", email)
        settings.imap_port = int(incoming.findtext("port") or 993)
        settings.imap_security = SOCKET_TYPES.get(incoming.findtext("socketType") or "", "ssl")
    return settings if settings.smtp_host else None


def _get(http: httpx.Client, url: str, **params) -> bytes | None:
    try:
        response = http.get(url, params=params or None, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or len(response.content) > MAX_BYTES:
        return None
    return response.content


def _mx_domain(domain: str) -> str | None:
    """'aspmx.l.google.com' -> 'google.com': the domain whose ISPDB entry fits."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
    except dns.exception.DNSException:
        return None
    best = min(answers, key=lambda r: r.preference, default=None)
    if best is None:
        return None
    labels = str(best.exchange).rstrip(".").lower().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else None


def lookup(email: str, http: httpx.Client | None = None) -> Settings | None:
    email = email.strip().lower()
    domain = email.rpartition("@")[2]
    if not domain or "." not in domain:
        return None
    http = http or httpx.Client(timeout=httpx.Timeout(6.0, connect=4.0))
    own = [
        f"https://autoconfig.{domain}/mail/config-v1.1.xml",
        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
    ]
    for url in own:
        found = _get(http, url, emailaddress=email)
        settings = found and parse(found, email, "domain")
        if settings:
            return settings
    found = _get(http, ISPDB + domain)
    settings = found and parse(found, email, "ispdb")
    if settings:
        return settings
    mx = _mx_domain(domain)
    if mx and mx != domain:
        found = _get(http, ISPDB + mx)
        settings = found and parse(found, email, f"ispdb via MX ({mx})")
        if settings:
            return settings
    return None
