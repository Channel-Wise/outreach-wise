"""Public data fetched for a lead before the AI writes anything.

- youtube_channel: channel info and the latest 50 uploads (titles, descriptions, views, age)
  through the YouTube Data API v3. About 3 quota units per lead.
- web_page: the text of one public web page.

Results are cached per lead in `lead_research` and shared by every playbook.
"""

import ipaddress
import json
import re
import socket
import sqlite3
import statistics
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .. import db

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
MAX_PAGE_BYTES = 2_000_000
MAX_PAGE_CHARS = 20_000
TIMEOUT = 20.0


class SourceError(Exception):
    pass


# --- YouTube --------------------------------------------------------------------------------


def channel_query(url: str) -> dict:
    """The channels.list parameter that identifies a channel link, @handle or channel ID."""
    value = url.strip()
    if re.fullmatch(r"UC[\w-]{22}", value):
        return {"id": value}
    if re.fullmatch(r"@[\w.\-]{3,100}", value):
        return {"forHandle": value}
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower()
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        raise SourceError(f"Not a YouTube channel link: {url}")
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0].startswith("@"):
        return {"forHandle": parts[0]}
    if len(parts) >= 2 and parts[0] == "channel":
        return {"id": parts[1]}
    if len(parts) >= 2 and parts[0] == "user":
        return {"forUsername": parts[1]}
    if parts and parts[0] == "watch" or "v" in parse_qs(parsed.query):
        raise SourceError("This is a video link; use the channel's link (youtube.com/@handle).")
    raise SourceError(f"Use a youtube.com/@handle or /channel/ link, not: {url}")


def _get(client: httpx.Client, path: str, key: str, **params) -> dict:
    try:
        response = client.get(f"{YOUTUBE_API}/{path}", params={**params, "key": key}, timeout=TIMEOUT)
    except httpx.HTTPError as err:
        raise SourceError(f"YouTube API unreachable: {err}") from None
    if response.status_code != 200:
        try:
            message = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = response.text[:200]
        raise SourceError(f"YouTube API error {response.status_code}: {message}")
    return response.json()


def _age_days(published: str, now: datetime) -> int | None:
    try:
        moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return max((now - moment).days, 0)


def youtube_channel(url: str, key: str, client: httpx.Client | None = None) -> dict:
    if not key:
        raise SourceError("Add a YouTube Data API key in Settings → AI personalization.")
    own = client is None
    client = client or httpx.Client()
    try:
        found = _get(client, "channels", key, part="snippet,statistics,contentDetails", **channel_query(url))
        items = found.get("items") or []
        if not items:
            raise SourceError(f"No YouTube channel found for {url}")
        channel = items[0]
        uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        video_ids = []
        if uploads:
            playlist = _get(
                client, "playlistItems", key, part="contentDetails", playlistId=uploads, maxResults=50
            )
            video_ids = [i["contentDetails"]["videoId"] for i in playlist.get("items", [])]
        videos_raw = []
        if video_ids:
            videos_raw = _get(
                client, "videos", key, part="snippet,statistics,contentDetails", id=",".join(video_ids)
            ).get("items", [])
    finally:
        if own:
            client.close()

    now = datetime.now(UTC)
    snippet, stats = channel.get("snippet", {}), channel.get("statistics", {})
    videos = []
    for item in videos_raw:
        s, st = item.get("snippet", {}), item.get("statistics", {})
        videos.append(
            {
                "title": s.get("title", ""),
                "published": s.get("publishedAt", "")[:10],
                "age_days": _age_days(s.get("publishedAt", ""), now),
                "views": int(st.get("viewCount", 0) or 0),
                "likes": int(st.get("likeCount", 0) or 0),
                "comments": int(st.get("commentCount", 0) or 0),
                "duration": item.get("contentDetails", {}).get("duration", ""),
                "url": f"https://www.youtube.com/watch?v={item.get('id', '')}",
                "description": (s.get("description") or "")[:700],
            }
        )
    views = [v["views"] for v in videos]
    hidden = stats.get("hiddenSubscriberCount")
    return {
        "channel": {
            "title": snippet.get("title", ""),
            "handle": snippet.get("customUrl", ""),
            "url": f"https://www.youtube.com/channel/{channel.get('id', '')}",
            "about": (snippet.get("description") or "")[:3000],
            "country": snippet.get("country", ""),
            "created": snippet.get("publishedAt", "")[:10],
            "subscribers": None if hidden else int(stats.get("subscriberCount", 0) or 0),
            "videos": int(stats.get("videoCount", 0) or 0),
            "total_views": int(stats.get("viewCount", 0) or 0),
        },
        "recent_videos": videos,
        "summary": {
            "videos_fetched": len(videos),
            "median_views": int(statistics.median(views)) if views else None,
            "days_since_last_upload": min(
                (v["age_days"] for v in videos if v["age_days"] is not None), default=None
            ),
        },
    }


# --- web page -------------------------------------------------------------------------------


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.description = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            a = dict(attrs)
            if (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
                self.description = self.description or (a.get("content") or "").strip()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(" ".join(data.split()))


def _public_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SourceError(f"Not a web link: {url}")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror:
        raise SourceError(f"Can't resolve {parsed.hostname}") from None
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            raise SourceError(f"{parsed.hostname} isn't a public address.")
    return parsed.geturl()


def web_page(url: str, client: httpx.Client | None = None) -> dict:
    own = client is None
    client = client or httpx.Client()
    try:
        current = _public_url(url)
        for _ in range(4):
            with client.stream(
                "GET", current, timeout=TIMEOUT, headers={"User-Agent": "OutreachWise/0.1 (+research)"}
            ) as response:
                if response.is_redirect and response.headers.get("location"):
                    current = _public_url(urljoin(current, response.headers["location"]))
                    continue
                if response.status_code >= 400:
                    raise SourceError(f"{current} returned HTTP {response.status_code}")
                if "html" not in response.headers.get("content-type", "html"):
                    raise SourceError(f"{current} isn't a web page.")
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                encoding = response.encoding or "utf-8"
                break
        else:
            raise SourceError("Too many redirects.")
    except httpx.HTTPError as err:
        raise SourceError(f"Couldn't fetch {url}: {err}") from None
    finally:
        if own:
            client.close()
    parser = _Text()
    parser.feed(body.decode(encoding, errors="replace"))
    text = "\n".join(parser.parts)
    return {
        "url": current,
        "title": " ".join(parser.title.split()),
        "description": parser.description,
        "text": text[:MAX_PAGE_CHARS],
        "truncated": len(text) > MAX_PAGE_CHARS,
    }


# --- cache ----------------------------------------------------------------------------------


def fetch(
    conn: sqlite3.Connection,
    lead_id: int,
    source: str,
    value: str,
    *,
    youtube_key: str = "",
    stale_days: int = 30,
    client: httpx.Client | None = None,
) -> dict:
    """Cached research for one (lead, source, input). Raises SourceError."""
    row = conn.execute(
        "SELECT * FROM lead_research WHERE lead_id = ? AND source = ? AND input = ?", (lead_id, source, value)
    ).fetchone()
    if (
        row
        and row["data"]
        and not row["error"]
        and db.parse(row["fetched_at"]) > db.now() - timedelta(days=stale_days)
    ):
        return json.loads(row["data"])
    data, error = None, None
    try:
        if source == "youtube_channel":
            data = youtube_channel(value, youtube_key, client)
        elif source == "web_page":
            data = web_page(value, client)
        else:
            raise SourceError(f"Unknown source {source}")
    except SourceError as err:
        error = str(err)
    conn.execute(
        "INSERT INTO lead_research (lead_id, source, input, data, error, fetched_at) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(lead_id, source, input) DO UPDATE SET data = COALESCE(excluded.data, lead_research.data),"
        " error = excluded.error, fetched_at = excluded.fetched_at",
        (
            lead_id,
            source,
            value,
            json.dumps(data, ensure_ascii=False) if data else None,
            error,
            db.iso(db.now()),
        ),
    )
    conn.commit()
    if error:
        raise SourceError(error)
    return data
