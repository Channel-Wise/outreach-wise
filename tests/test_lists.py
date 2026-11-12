import io
import json

import pytest
from conftest import add_leads, make_campaign

from outreach_wise import campaigns, db, lists, sheets
from outreach_wise.web import create_app


@pytest.fixture
def client(cfg, conn, google, monkeypatch):
    monkeypatch.setattr("outreach_wise.web.time.sleep", lambda _: None)
    client = create_app(cfg, google).test_client()
    assert client.post("/login", data={"password": "correct horse"}).status_code == 302
    with client.session_transaction() as session:
        client.csrf = session["csrf"]
    return client


def members(conn, list_id):
    return {
        r[0]
        for r in conn.execute(
            "SELECT l.email FROM leads l JOIN lead_list_members m ON m.lead_id = l.id WHERE m.list_id = ?",
            (list_id,),
        )
    }


def fields(conn, email):
    return json.loads(conn.execute("SELECT fields FROM leads WHERE email = ?", (email,)).fetchone()[0])


# --- upgrading from the single sheet in Settings -----------------------------------------------


def test_the_sheet_in_settings_becomes_a_list_with_the_existing_leads(conn):
    add_leads(conn, 2)
    db.save_settings(conn, {"sheet_url": "abc", "sheet_tab": "Outreach Wise", "sheet_email_column": "mail"})
    lists.adopt_existing(conn)
    row = conn.execute("SELECT * FROM lead_lists").fetchone()
    assert (row["name"], row["sheet_url"], row["sheet_tab"], row["sheet_email_column"]) == (
        "Outreach Wise",
        "abc",
        "Outreach Wise",
        "mail",
    )
    assert members(conn, row["id"]) == {"lead1@leads.io", "lead2@leads.io"}
    assert db.get_settings(conn)["sheet_url"] == ""  # the list owns the sheet now
    lists.adopt_existing(conn)  # idempotent
    assert conn.execute("SELECT COUNT(*) FROM lead_lists").fetchone()[0] == 1


def test_leads_without_a_sheet_go_to_a_leads_list(conn):
    add_leads(conn, 2)
    lists.adopt_existing(conn)
    (list_id, name) = conn.execute("SELECT id, name FROM lead_lists").fetchone()
    assert name == "Leads" and len(members(conn, list_id)) == 2


# --- sources --------------------------------------------------------------------------------


def test_each_sheet_list_syncs_into_its_own_list(conn, cfg, google, account):
    en, _ = lists.create(conn, "English", {"sheet_url": "abc", "sheet_tab": "EN"})
    tr, _ = lists.create(conn, "Türk kanallar", {"sheet_url": "abc", "sheet_tab": "TR"})
    google.sheet = [["Email", "Name"], ["a@leads.io", "A"]]
    sheets.sync(conn, cfg, google, en)
    google.sheet = [["Email", "Name"], ["b@leads.io", "B"], ["a@leads.io", "A"]]
    sheets.sync(conn, cfg, google, tr)
    assert members(conn, en) == {"a@leads.io"}
    assert members(conn, tr) == {"a@leads.io", "b@leads.io"}  # one lead, two lists
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
    assert (
        json.loads(conn.execute("SELECT last_sync FROM lead_lists WHERE id = ?", (tr,)).fetchone()[0])[
            "created"
        ]
        == 1
    )


def test_csv_and_json_files(conn):
    list_id, _ = lists.create(conn, "Files")
    csv_rows = lists.file_rows(
        "leads.csv", "E-mail;First Name;Channel URL\nA@Leads.io;Ayşe;https://yt/@a\n".encode()
    )
    stats = sheets.import_rows(conn, list_id, csv_rows, "File leads.csv", "e-mail")
    assert stats["created"] == 1
    assert fields(conn, "a@leads.io") == {
        "e_mail": "A@Leads.io",
        "first_name": "Ayşe",
        "channel_url": "https://yt/@a",
    }

    data = json.dumps([{"email": "a@leads.io", "niche": "finance"}, {"email": "b@leads.io", "tags": ["x"]}])
    stats = sheets.import_rows(conn, list_id, lists.file_rows("leads.json", data.encode()), "File leads.json")
    assert (stats["created"], stats["updated"]) == (1, 1)
    assert fields(conn, "a@leads.io")["first_name"] == "Ayşe"  # merged, not replaced
    assert fields(conn, "a@leads.io")["niche"] == "finance"
    assert fields(conn, "b@leads.io")["tags"] == '["x"]'
    with pytest.raises(lists.LeadImportError):
        lists.file_rows("bad.json", b'{"email": 1')


# --- campaigns ------------------------------------------------------------------------------


def test_enroll_and_auto_enroll_from_a_list(conn):
    add_leads(conn, 4)
    finance, _ = lists.create(conn, "Finance")
    lists.add_members(conn, finance, [1, 2])
    campaign_id = campaigns.create(conn, "From a list")
    assert campaigns.count_eligible(conn, campaign_id, list_id=finance) == 2
    assert campaigns.enroll(conn, campaign_id, list_id=finance) == 2
    enrolled = {
        r[0] for r in conn.execute("SELECT lead_id FROM enrollments WHERE campaign_id = ?", (campaign_id,))
    }
    assert enrolled == {1, 2}

    other = campaigns.create(conn, "Auto from a list")
    conn.execute(
        "UPDATE campaigns SET status = 'active', auto_enroll = 1, list_id = ? WHERE id = ?", (finance, other)
    )
    add_leads(conn, 6)  # leads 5 and 6 are new, in no list
    lists.add_members(conn, finance, [5])
    assert campaigns.auto_enroll(conn) == 1  # only lead 5, from the campaign's list


def test_preview_before_anyone_is_enrolled(conn, cfg, client):
    add_leads(conn, 4)
    finance, _ = lists.create(conn, "Finance")
    lists.add_members(conn, finance, [3, 4])
    campaign_id = campaigns.create(conn, "Nobody enrolled yet")

    def samples():
        preview = campaigns.preview(conn, cfg, campaign_id, footer="")
        return [r["email"] for r in preview["steps"][0]["variants"][0]["renders"]]

    assert samples() == ["lead1@leads.io", "lead2@leads.io", "lead3@leads.io"]  # from any list
    conn.execute("UPDATE campaigns SET list_id = ? WHERE id = ?", (finance, campaign_id))
    assert samples() == ["lead3@leads.io", "lead4@leads.io"]  # from the campaign's list
    conn.commit()
    assert client.get(f"/campaigns/{campaign_id}/preview").status_code == 200


# --- pages ----------------------------------------------------------------------------------


def test_list_pages(client, conn):
    make_campaign(conn, leads=2)
    response = client.post(
        "/lists",
        data={"csrf": client.csrf, "name": "Türk kanallar", "source": "manual"},
        follow_redirects=True,
    )
    assert response.status_code == 200 and "Türk kanallar" in response.get_data(as_text=True)
    list_id = conn.execute("SELECT id FROM lead_lists WHERE name = 'Türk kanallar'").fetchone()[0]

    client.post(
        f"/lists/{list_id}/add",
        data={"csrf": client.csrf, "email": "Ayse@Example.com", "field_name": ["first_name", "Channel URL"],
              "field_value": ["Ayşe", "https://yt/@ayse"]},
    )  # fmt: skip
    assert fields(conn, "ayse@example.com") == {
        "email": "ayse@example.com",
        "first_name": "Ayşe",
        "channel_url": "https://yt/@ayse",
    }
    upload = (io.BytesIO(b"email,first_name\nmehmet@example.com,Mehmet\n"), "tr.csv")
    client.post(
        f"/lists/{list_id}/import",
        data={"csrf": client.csrf, "file": upload},
        content_type="multipart/form-data",
    )
    assert members(conn, list_id) == {"ayse@example.com", "mehmet@example.com"}

    client.post(
        "/leads/lists", data={"csrf": client.csrf, "list_id": list_id, "action": "add", "lead_id": ["1"]}
    )
    assert "lead1@leads.io" in members(conn, list_id)
    client.post(
        "/leads/lists", data={"csrf": client.csrf, "list_id": list_id, "action": "remove", "lead_id": ["1"]}
    )
    assert "lead1@leads.io" not in members(conn, list_id)

    for path in ["/lists", f"/lists/{list_id}", f"/leads?list={list_id}", "/campaigns/1", "/settings", "/"]:
        assert client.get(path).status_code == 200, path
    assert "mehmet@example.com" in client.get(f"/leads?list={list_id}").get_data(as_text=True)
    assert "lead1@leads.io" not in client.get(f"/leads?list={list_id}").get_data(as_text=True)

    client.post(
        "/campaigns/1/settings", data={"csrf": client.csrf, "name": "Test campaign", "list_id": list_id}
    )
    assert conn.execute("SELECT list_id FROM campaigns WHERE id = 1").fetchone()[0] == list_id
    page = client.get("/campaigns/1").get_data(as_text=True)
    assert (
        '"name": "Türk kanallar"' in page or '"name":"Türk kanallar"' in page or "T\\u00fcrk kanallar" in page
    )

    client.post(f"/lists/{list_id}/delete", data={"csrf": client.csrf})
    assert not conn.execute("SELECT 1 FROM lead_lists WHERE id = ?", (list_id,)).fetchone()
    assert conn.execute("SELECT 1 FROM leads WHERE email = 'mehmet@example.com'").fetchone()  # leads stay
    assert conn.execute("SELECT list_id FROM campaigns WHERE id = 1").fetchone()[0] is None
