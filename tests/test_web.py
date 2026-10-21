import pytest
from conftest import make_campaign

from outreach_wise import db, sender
from outreach_wise.security import unsubscribe_token
from outreach_wise.web import create_app


@pytest.fixture
def client(cfg, conn, google, monkeypatch):
    monkeypatch.setattr("outreach_wise.web.time.sleep", lambda _: None)
    return create_app(cfg, google).test_client()


def login(client):
    assert client.post("/login", data={"password": "correct horse"}).status_code == 302
    with client.session_transaction() as session:
        return session["csrf"]


def test_pages_require_login(client):
    response = client.get("/campaigns")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert client.post("/login", data={"password": "nope"}).status_code == 200


def test_posts_require_csrf_token(client):
    login(client)
    assert client.post("/settings/pause").status_code == 400
    assert client.post("/settings/pause", data={"csrf": "wrong"}).status_code == 400


def test_every_page_renders(client, conn, cfg, google, account):
    make_campaign(conn, leads=3)
    sender.run_send_cycle(conn, cfg, google)
    login(client)
    for path in [
        "/",
        "/leads",
        "/leads?status=new&q=lead",
        "/leads/1",
        "/campaigns",
        "/campaigns/1",
        "/campaigns/1/preview",
        "/activity",
        "/activity?kind=dry_run",
        "/suppressions",
        "/settings",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
    assert b"Hi Name1" in client.get("/campaigns/1/preview").data


def test_campaign_editing_flow(client, conn):
    token = login(client)
    response = client.post("/campaigns", data={"csrf": token, "name": "October creators"})
    assert response.status_code == 302
    step = conn.execute("SELECT * FROM steps WHERE position = 1").fetchone()
    variant = conn.execute("SELECT * FROM variants WHERE step_id = ?", (step["id"],)).fetchone()
    client.post(
        f"/variants/{variant['id']}",
        data={"csrf": token, "subject": "Hey {{first_name|there}}", "body": "Body", "active": "on"},
    )
    assert (
        conn.execute("SELECT subject FROM variants WHERE id = ?", (variant["id"],)).fetchone()[0]
        == "Hey {{first_name|there}}"
    )
    client.post(f"/steps/{step['id']}/variants", data={"csrf": token})
    assert conn.execute("SELECT COUNT(*) FROM variants WHERE step_id = ?", (step["id"],)).fetchone()[0] == 2
    client.post("/campaigns/1/steps", data={"csrf": token})
    assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 3


def test_going_live_needs_explicit_approval(client, conn, account):
    make_campaign(conn, leads=2)
    token = login(client)
    client.post("/campaigns/1/go-live", data={"csrf": token, "approve": "yes", "confirm_name": "wrong"})
    assert conn.execute("SELECT mode FROM campaigns").fetchone()[0] == "dry_run"
    client.post("/campaigns/1/go-live", data={"csrf": token, "confirm_name": "Test campaign"})
    assert conn.execute("SELECT mode FROM campaigns").fetchone()[0] == "dry_run"
    client.post(
        "/campaigns/1/go-live", data={"csrf": token, "approve": "yes", "confirm_name": "Test campaign"}
    )
    assert conn.execute("SELECT mode, approved_at IS NOT NULL FROM campaigns").fetchone()[:] == ("live", 1)


def test_going_live_is_blocked_without_an_inbox(client, conn):
    make_campaign(conn, leads=1)
    token = login(client)
    client.post(
        "/campaigns/1/go-live", data={"csrf": token, "approve": "yes", "confirm_name": "Test campaign"}
    )
    assert conn.execute("SELECT mode FROM campaigns").fetchone()[0] == "dry_run"


def test_public_unsubscribe_page(client, conn, cfg):
    make_campaign(conn, leads=1)
    token = unsubscribe_token(cfg.secret_key, 1)
    assert b"Unsubscribe?" in client.get(f"/u/{token}").data
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "new"  # GET alone changes nothing
    assert b"unsubscribed" in client.post(f"/u/{token}").data  # also RFC 8058 one-click
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "unsubscribed"
    assert db.is_suppressed(conn, "lead1@leads.io")
    assert client.get("/u/forged-token").status_code == 404


def test_pause_all_sending(client, conn):
    token = login(client)
    client.post("/settings/pause", data={"csrf": token})
    assert db.get_settings(conn)["sending_paused"] is True
