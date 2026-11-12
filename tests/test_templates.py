"""Email template library: seeding, CRUD and the JSON responses the campaign editor uses."""

import pytest
from test_web import login

from outreach_wise import db, library
from outreach_wise.web import create_app

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(cfg, conn, google):
    return create_app(cfg, google).test_client()


def test_templates_are_seeded_once(conn, cfg):
    count = conn.execute("SELECT COUNT(*) FROM email_templates").fetchone()[0]
    assert count == len(library.SEEDS)
    db.init_db(cfg.database_path)  # running migrations again must not duplicate the seeds
    assert conn.execute("SELECT COUNT(*) FROM email_templates").fetchone()[0] == count


def test_template_library_crud(client, conn):
    token = login(client)
    assert client.get("/templates").status_code == 200
    res = client.post(
        "/templates",
        data={"csrf": token, "name": "Mine", "category": "Follow-up", "body": "Hi"},
        headers=FETCH,
    )
    template = next(t for t in res.get_json()["templates"] if t["name"] == "Mine")
    assert template["category"] == "Follow-up" and template["builtin"] == 0
    client.post(
        f"/templates/{template['id']}",
        data={"csrf": token, "name": "Mine 2", "category": "Nope", "subject": "S\nX", "body": "Hi"},
    )
    row = conn.execute("SELECT * FROM email_templates WHERE id = ?", (template["id"],)).fetchone()
    assert (row["name"], row["category"], row["subject"]) == ("Mine 2", "Other", "S X")
    res = client.post("/templates", data={"csrf": token, "name": "", "body": "Hi"}, headers=FETCH)
    assert res.status_code == 400
    client.post(f"/templates/{template['id']}/delete", data={"csrf": token})
    assert (
        conn.execute("SELECT COUNT(*) FROM email_templates WHERE id = ?", (template["id"],)).fetchone()[0]
        == 0
    )
