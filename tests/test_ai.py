import json
import stat

import httpx
import pytest
from conftest import add_leads

from outreach_wise import campaigns, db, sender
from outreach_wise.ai import checks, generate, keys, playbooks, providers, scheduler, sources

PLAYBOOK = """+++
name = "Test creators"

[input]
column = "channel_url"
sources = []

[fields.hook]
describe = "One concrete thing"
max_words = 30

[fields.greeting]
describe = "Hi <name>"
fallback = "Hi there,"

[fields.subject]
describe = "Subject"
alternatives = 2

[checks]
email_words = [3, 80]
banned = ["synergy"]
one_link = true
numbers_in_data = true
+++

# Rules
Be specific.
"""


class FakeProvider:
    name = "fake"

    def __init__(self, answer=None, error=None):
        self.calls = []
        self.answer = answer or {
            "fields": {"hook": "Your Tezgah series stood out.", "greeting": "", "subject": "Tezgah?"},
            "alternatives": {"subject": ["A", "B"]},
            "evidence": [{"claim": "series", "source": "video titles"}],
            "notes_for_reviewer": "check the video",
            "skip_reason": "",
        }
        self.error = error

    def generate(self, system, user, schema, max_tokens=16000):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.error:
            raise self.error
        return providers.Result(
            data=self.answer, model="fake-model", usage={"output_tokens": 10}, cost_usd=0.01
        )


@pytest.fixture
def ai_on(conn):
    db.save_settings(conn, {"ai_enabled": True})
    conn.commit()
    return db.get_settings(conn)


@pytest.fixture
def playbook_id(conn):
    pid, error = playbooks.save(conn, PLAYBOOK)
    assert error is None
    conn.commit()
    return pid


def ai_campaign(conn, playbook_id, *, leads=2, subject="{{ai_subject}}", body="{{ai_greeting}} {{ai_hook}}"):
    add_leads(conn, leads, channel_url="https://www.youtube.com/@someone")
    campaign_id = campaigns.create(conn, "AI campaign")
    steps = campaigns.steps_with_variants(conn, campaign_id)
    campaigns.update_variant(conn, steps[0]["variants"][0]["id"], subject, body, True)
    conn.execute(
        "UPDATE campaigns SET status = 'active', playbook_id = ? WHERE id = ?", (playbook_id, campaign_id)
    )
    campaigns.enroll(conn, campaign_id)
    conn.commit()
    return campaign_id


def statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM ai_fields ORDER BY id")]


# --- playbook format ------------------------------------------------------------------------


def test_playbook_round_trips_and_examples_load(conn):
    parsed = playbooks.parse(PLAYBOOK)
    assert [f.name for f in parsed.fields] == ["hook", "greeting", "subject"]
    assert parsed.get_field("greeting").required is False
    assert playbooks.parse(playbooks.dump(parsed)) == parsed
    names = {row["name"] for row in playbooks.all_playbooks(conn)}
    assert {"YouTube creators", "Companies by website"} <= names


@pytest.mark.parametrize(
    "text, message",
    [
        ("no settings", "starts with"),
        ("+++\nname = 'x'\n", "isn't closed"),
        ("+++\nname = 'x'\n[input]\ncolumn='c'\n+++\n", "at least one field"),
        ("+++\nname = 'x'\n[input]\ncolumn='c'\nsources=['tiktok']\n[fields.a]\n+++\n", "input.sources"),
        ("+++\nname = 'x'\n[input]\ncolumn='c'\n[fields.Bad]\n+++\n", "lowercase"),
    ],
)
def test_invalid_playbooks_explain_the_problem(text, message):
    with pytest.raises(playbooks.PlaybookError, match=message):
        playbooks.parse(text)


def test_examples_are_read_only(conn):
    example = conn.execute("SELECT id FROM playbooks WHERE builtin = 1").fetchone()["id"]
    _, error = playbooks.save(conn, PLAYBOOK, example)
    assert "duplicate" in error


# --- scheduling and the send gate -----------------------------------------------------------


def test_held_until_approved_then_sent_with_ai_values(conn, cfg, google, ai_on, playbook_id, clock):
    ai_campaign(conn, playbook_id, leads=1)
    assert sender.run_send_cycle(conn, cfg, google) == 0  # dry run, but nothing to render yet
    enrollment = conn.execute("SELECT * FROM enrollments").fetchone()
    assert enrollment["status"] == "active" and enrollment["pinned_step"] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert statuses(conn) == ["pending"]

    fake = FakeProvider()
    assert scheduler.run(conn, cfg, ai_on, provider=fake) == 1
    assert statuses(conn) == ["needs_review"]
    clock.advance(hours=2)
    assert sender.run_send_cycle(conn, cfg, google) == 0  # still needs approval

    conn.execute("UPDATE ai_fields SET status = 'approved'")
    conn.commit()
    clock.advance(hours=2)
    assert sender.run_send_cycle(conn, cfg, google) == 1
    message = conn.execute("SELECT subject, body FROM messages").fetchone()
    assert message["subject"] == "Tezgah?"
    assert message["body"].startswith("Hi there, Your Tezgah series stood out.")  # greeting fell back


def test_campaign_without_approval_sends_drafts(conn, cfg, google, ai_on, playbook_id, clock):
    campaign_id = ai_campaign(conn, playbook_id, leads=1)
    conn.execute("UPDATE campaigns SET require_approval = 0 WHERE id = ?", (campaign_id,))
    scheduler.plan(conn, ai_on)
    scheduler.run(conn, cfg, ai_on, provider=FakeProvider())
    assert sender.run_send_cycle(conn, cfg, google) == 1


def test_skipped_lead_stops_its_sequence(conn, cfg, google, ai_on, playbook_id):
    ai_campaign(conn, playbook_id, leads=1)
    scheduler.plan(conn, ai_on)
    answer = {"fields": {"hook": "", "greeting": "", "subject": ""}, "alternatives": {"subject": []},
              "evidence": [], "notes_for_reviewer": "", "skip_reason": "Creator looks underage"}  # fmt: skip
    scheduler.run(conn, cfg, ai_on, provider=FakeProvider(answer))
    assert statuses(conn) == ["skipped"]
    sender.run_send_cycle(conn, cfg, google)
    enrollment = conn.execute("SELECT status, stop_reason FROM enrollments").fetchone()
    assert enrollment["status"] == "stopped" and "underage" in enrollment["stop_reason"]


def test_variant_without_ai_fields_never_calls_the_model(conn, cfg, google, ai_on, playbook_id):
    campaign_id = ai_campaign(conn, playbook_id, leads=4)
    step = campaigns.steps_with_variants(conn, campaign_id)[0]
    assert campaigns.add_variant(conn, step["id"]) is None
    b = campaigns.steps_with_variants(conn, campaign_id)[0]["variants"][1]
    campaigns.update_variant(conn, b["id"], "Hi {{first_name}}", "Plain email", True)
    conn.commit()

    assert scheduler.plan(conn, ai_on) == 2  # only the two leads pinned to variant A
    pins = [r[0] for r in conn.execute("SELECT pinned_variant_id FROM enrollments ORDER BY id")]
    assert sorted(pins.count(v) for v in set(pins)) == [2, 2]  # the A/B split still holds
    assert len(statuses(conn)) == 2

    sender.run_send_cycle(conn, cfg, google)
    sent = conn.execute("SELECT variant_id FROM messages").fetchall()
    assert [r[0] for r in sent] == [b["id"], b["id"]]  # B goes out, A waits for its fields


def test_fields_are_reused_by_another_campaign(conn, cfg, ai_on, playbook_id):
    ai_campaign(conn, playbook_id, leads=1)
    scheduler.plan(conn, ai_on)
    fake = FakeProvider()
    scheduler.run(conn, cfg, ai_on, provider=fake)
    conn.execute("UPDATE enrollments SET status = 'completed'")

    second = campaigns.create(conn, "Second")
    variant = campaigns.steps_with_variants(conn, second)[0]["variants"][0]
    campaigns.update_variant(conn, variant["id"], "{{ai_subject}}", "{{ai_hook}}", True)
    conn.execute(
        "UPDATE campaigns SET status = 'active', playbook_id = ? WHERE id = ?", (playbook_id, second)
    )
    campaigns.enroll(conn, second, include_contacted=True)
    assert scheduler.plan(conn, ai_on) == 0
    assert scheduler.run(conn, cfg, ai_on, provider=fake) == 0
    assert len(fake.calls) == 1


def test_daily_limit_stops_research(conn, cfg, ai_on, playbook_id):
    db.save_settings(conn, {"ai_daily_limit": 1})
    settings = db.get_settings(conn)
    ai_campaign(conn, playbook_id, leads=3)
    scheduler.plan(conn, settings)
    assert scheduler.run(conn, cfg, settings, max_items=5, provider=FakeProvider()) == 1
    assert statuses(conn).count("pending") == 2
    assert db.get_kv(conn, "ai_paused") == "Daily research limit reached."


def test_retryable_errors_retry_then_fail(conn, cfg, ai_on, playbook_id):
    ai_campaign(conn, playbook_id, leads=1)
    scheduler.plan(conn, ai_on)
    fake = FakeProvider(error=providers.ProviderError("busy", retryable=True))
    for _ in range(3):
        scheduler.run(conn, cfg, ai_on, provider=fake)
    row = conn.execute("SELECT status, attempts, error FROM ai_fields").fetchone()
    assert (row["status"], row["attempts"], row["error"]) == ("failed", 3, "busy")


def test_ai_off_holds_without_research(conn, cfg, google, playbook_id):
    ai_campaign(conn, playbook_id, leads=1)
    sender.run_send_cycle(conn, cfg, google)
    assert statuses(conn) == []
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "active"
    assert "Turn on AI personalization" in " ".join(campaigns.go_live_blockers(conn, 1))


def test_campaign_preview_shows_placeholders(conn, cfg, ai_on, playbook_id):
    campaign_id = ai_campaign(conn, playbook_id, leads=1)
    result = campaigns.preview(conn, cfg, campaign_id, "")
    render = result["steps"][0]["variants"][0]["renders"][0]
    assert "[ai_hook: not written yet]" in render["body"]
    assert result["missing"] == {}


# --- writing --------------------------------------------------------------------------------


def test_prompt_contains_lead_data_and_reviewer_examples(conn, cfg, ai_on, playbook_id):
    add_leads(conn, 1, channel_url="https://www.youtube.com/@someone")
    lead = conn.execute("SELECT * FROM leads").fetchone()
    conn.execute(
        "INSERT INTO ai_examples (playbook_id, field, before, after, created_at) VALUES (?, 'hook', 'old', 'new', ?)",
        (playbook_id, db.iso(db.now())),
    )
    fake = FakeProvider()
    outcome = generate.write(
        conn, cfg, ai_on, playbooks.load(conn, playbook_id), lead, playbook_id=playbook_id, provider=fake
    )
    assert outcome["status"] == "needs_review"
    assert outcome["alternatives"] == {"subject": ["A", "B"]}
    call = fake.calls[0]
    assert "approved: new" in call["system"] and "Be specific." in call["system"]
    assert "someone" in call["user"] and "lead1@" not in call["user"]
    assert call["schema"]["properties"]["fields"]["required"] == ["hook", "greeting", "subject"]


def test_lead_without_input_is_skipped(conn, cfg, ai_on, playbook_id):
    add_leads(conn, 1)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    outcome = generate.write(
        conn, cfg, ai_on, playbooks.load(conn, playbook_id), lead, provider=FakeProvider()
    )
    assert outcome == {"status": "skipped", "skip_reason": "Lead has no value in the channel_url column."}


def test_checks_flag_problems():
    pb = playbooks.parse(PLAYBOOK)
    found = checks.field_checks(
        pb, {"hook": "We bring synergy to 48K viewers", "subject": "x"}, "views 47,900"
    )
    assert any("synergy" in c["text"] and not c["ok"] for c in found)
    assert any(c["ok"] and "Numbers match" in c["text"] for c in found)
    found = checks.field_checks(pb, {"hook": "Your 120K video"}, "views 48000")
    assert any(not c["ok"] and "120K" in c["text"] for c in found)
    email = checks.email_checks(pb, "Hi", "Go to example.com and www.other.io now")
    assert any(not c["ok"] and "2 links" in c["text"] for c in email)


# --- sources and providers ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, query",
    [
        ("https://www.youtube.com/@atolye", {"forHandle": "@atolye"}),
        ("youtube.com/channel/UC1234567890123456789012", {"id": "UC1234567890123456789012"}),
        ("@atolye", {"forHandle": "@atolye"}),
        ("https://youtube.com/user/oldname", {"forUsername": "oldname"}),
    ],
)
def test_channel_links(url, query):
    assert sources.channel_query(url) == query


def test_channel_link_errors():
    with pytest.raises(sources.SourceError, match="video link"):
        sources.channel_query("https://www.youtube.com/watch?v=abc")
    with pytest.raises(sources.SourceError, match="Not a YouTube"):
        sources.channel_query("https://example.com/@x")


def test_youtube_channel_research(conn):
    def handler(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "channels":
            return httpx.Response(200, json={"items": [{
                "id": "UCx", "snippet": {"title": "Atölye", "description": "Ben Kerem", "publishedAt": "2020-01-01T00:00:00Z"},
                "statistics": {"subscriberCount": "21400", "videoCount": "2", "viewCount": "100"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]})  # fmt: skip
        if path == "playlistItems":
            return httpx.Response(
                200,
                json={
                    "items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]
                },
            )
        return httpx.Response(200, json={"items": [
            {"id": "v1", "snippet": {"title": "Tezgah #1", "publishedAt": "2026-03-01T00:00:00Z"}, "statistics": {"viewCount": "48000"}},
            {"id": "v2", "snippet": {"title": "Tezgah #2", "publishedAt": "2026-04-01T00:00:00Z"}, "statistics": {"viewCount": "11000"}},
        ]})  # fmt: skip

    client = httpx.Client(transport=httpx.MockTransport(handler))
    add_leads(conn, 1)
    data = sources.fetch(conn, 1, "youtube_channel", "@atolye", youtube_key="k", client=client)
    assert data["channel"]["subscribers"] == 21400
    assert [v["views"] for v in data["recent_videos"]] == [48000, 11000]
    assert data["summary"]["median_views"] == 29500
    # Cached: a second fetch doesn't call the API.
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert sources.fetch(conn, 1, "youtube_channel", "@atolye", youtube_key="k", client=broken) == data


def test_web_page_refuses_private_addresses():
    with pytest.raises(sources.SourceError, match="public"):
        sources.web_page("http://127.0.0.1:8000/")


def test_keys_are_encrypted_and_env_wins(conn, cfg):
    keys.set_key(conn, cfg, "ai_api_key", "sk-ant-secret-1234")
    assert "secret" not in json.dumps(db.get_kv(conn, "ai_api_key"))
    assert keys.get(conn, cfg, "ai_api_key") == "sk-ant-secret-1234"
    assert keys.masked(conn, cfg, "ai_api_key") == "…1234"
    from dataclasses import replace

    assert keys.get(conn, replace(cfg, anthropic_api_key="env-key"), "ai_api_key") == "env-key"


def test_claude_code_provider_parses_structured_output(tmp_path):
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\ncat > /dev/null\n"
        'echo \'{"is_error": false, "result": "", "structured_output": {"ok": "yes"}, "total_cost_usd": 0.02, "usage": {"output_tokens": 5}}\'\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    result = providers.ClaudeCodeProvider("claude-haiku-5-5", binary=str(script)).generate(
        "sys", "hi", {"type": "object"}
    )
    assert result.data == {"ok": "yes"} and result.cost_usd == 0.02


def test_claude_code_provider_reports_errors(tmp_path):
    script = tmp_path / "claude"
    script.write_text('#!/bin/sh\necho \'{"is_error": true, "result": "Not logged in"}\'\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(providers.ProviderError, match="Not logged in"):
        providers.ClaudeCodeProvider("m", binary=str(script)).generate("s", "u", {})


def test_anthropic_provider_request_shape():
    class Block:
        type, text = "text", '{"fields": {}}'

    class Usage:
        input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens = 1000, 100, 0, 0

    class Response:
        content, usage, stop_reason, model = [Block()], Usage(), "end_turn", "claude-opus-5-5"

    sent = {}

    class Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return Response()

    class Client:
        class beta:  # noqa: N801
            messages = Messages()

    result = providers.AnthropicProvider("", "claude-opus-5-5", client=Client()).generate(
        "sys", "user", {"type": "object"}
    )
    assert result.data == {"fields": {}} and result.cost_usd == pytest.approx(0.006)
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["fallbacks"] == "default"


# --- web ------------------------------------------------------------------------------------


@pytest.fixture
def client(cfg, conn, google, monkeypatch):
    from outreach_wise.web import create_app

    monkeypatch.setattr("outreach_wise.web.time.sleep", lambda _: None)
    app = create_app(cfg, google).test_client()
    assert app.post("/login", data={"password": "correct horse"}).status_code == 302
    with app.session_transaction() as session:
        app.csrf = session["csrf"]
    return app


def test_ai_menu_is_hidden_until_enabled(client, conn, cfg):
    page = client.get("/settings").get_data(as_text=True)
    assert "AI personalization" in page and 'href="/playbooks"' not in page
    assert client.get("/playbooks").status_code == 302

    response = client.post(
        "/settings/ai",
        data={
            "csrf": client.csrf,
            "ai_enabled": "on",
            "ai_api_key": "sk-ant-test-9876",
            "ai_model": "claude-sonnet-5-5",
        },
    )
    assert response.status_code == 302
    settings = db.get_settings(conn)
    assert settings["ai_enabled"] and settings["ai_model"] == "claude-sonnet-5-5"
    assert keys.get(conn, cfg, "ai_api_key") == "sk-ant-test-9876"
    page = client.get("/settings").get_data(as_text=True)
    assert 'href="/playbooks"' in page and "sk-ant-test-9876" not in page and "…9876" in page


def test_ai_pages_render(client, conn, ai_on, playbook_id):
    campaign_id = ai_campaign(conn, playbook_id, leads=2)
    scheduler.plan(conn, ai_on)
    from outreach_wise.config import Config  # noqa: F401

    for path in [
        "/playbooks",
        "/playbooks/new",
        f"/playbooks/{playbook_id}",
        f"/playbooks/{playbook_id}/export",
        "/review",
        "/review?status=pending",
        f"/campaigns/{campaign_id}",
        f"/campaigns/{campaign_id}/preview",
    ]:
        assert client.get(path).status_code == 200, path
    page = client.get(f"/campaigns/{campaign_id}").get_data(as_text=True)
    assert "{{ai_hook}}" in page and "Test creators" in page


def test_editor_form_saves_a_valid_playbook(client, conn, ai_on):
    data = {
        "csrf": client.csrf, "mode": "form", "playbook_id": "", "name": "Podcasts", "input_column": "website",
        "language": "en", "sources": ["web_page"], "instructions": "# Rules\nBe brief.",
        "f_name": ["ai_hook", "subject"], "f_describe": ["One thing", "Subject"], "f_max_words": ["40", ""],
        "f_fallback": ["", ""], "f_alternatives": ["0", "3"],
        "email_min": "50", "email_max": "120", "subject_min": "", "subject_max": "",
        "banned": "synergy\nleverage", "one_link": "on",
    }  # fmt: skip
    response = client.post("/playbooks/save", data=data)
    assert response.status_code == 302
    saved = playbooks.parse(
        conn.execute("SELECT source FROM playbooks WHERE name = 'Podcasts'").fetchone()[0]
    )
    assert [f.name for f in saved.fields] == ["hook", "subject"]
    assert saved.checks.banned == ["synergy", "leverage"] and saved.checks.email_words == (50, 120)
    assert saved.get_field("subject").alternatives == 3


def test_review_approve_records_edits_and_releases_the_email(
    client, conn, cfg, google, ai_on, playbook_id, clock
):
    ai_campaign(conn, playbook_id, leads=1)
    sender.run_send_cycle(conn, cfg, google)  # held, queued
    scheduler.run(conn, cfg, ai_on, provider=FakeProvider())
    row = conn.execute("SELECT id FROM ai_fields").fetchone()
    assert "Your Tezgah series stood out." in client.get("/review").get_data(as_text=True)

    response = client.post(
        f"/review/{row['id']}/approve",
        data={
            "csrf": client.csrf,
            "val_hook": "Edited hook.",
            "val_greeting": "Hi Kerem,",
            "val_subject": "Tezgah?",
        },
    )
    assert response.status_code == 302
    approved = conn.execute("SELECT status, vals FROM ai_fields").fetchone()
    assert approved["status"] == "approved" and json.loads(approved["vals"])["hook"] == "Edited hook."
    assert conn.execute("SELECT field, before, after FROM ai_examples").fetchall()[0][:] == (
        "hook",
        "Your Tezgah series stood out.",
        "Edited hook.",
    )
    assert sender.run_send_cycle(conn, cfg, google) == 1  # released right away, no hour-long wait
    assert conn.execute("SELECT body FROM messages").fetchone()[0].startswith("Hi Kerem, Edited hook.")


def test_review_skip_and_regenerate(client, conn, cfg, ai_on, playbook_id):
    ai_campaign(conn, playbook_id, leads=1)
    scheduler.plan(conn, ai_on)
    scheduler.run(conn, cfg, ai_on, provider=FakeProvider())
    ai_id = conn.execute("SELECT id FROM ai_fields").fetchone()[0]
    client.post(f"/review/{ai_id}/regenerate", data={"csrf": client.csrf, "note": "Use the newest series"})
    row = conn.execute("SELECT status, regenerate_note FROM ai_fields").fetchone()
    assert tuple(row) == ("pending", "Use the newest series")
    fake = FakeProvider()
    scheduler.run(conn, cfg, ai_on, provider=fake)
    assert "Use the newest series" in fake.calls[0]["user"]
    client.post(f"/review/{ai_id}/skip", data={"csrf": client.csrf})
    assert conn.execute("SELECT status FROM ai_fields").fetchone()[0] == "skipped"


def test_campaign_ai_settings(client, conn, ai_on, playbook_id):
    campaign_id = ai_campaign(conn, playbook_id, leads=1)
    client.post(f"/campaigns/{campaign_id}/ai", data={"csrf": client.csrf, "playbook_id": ""})
    row = conn.execute("SELECT playbook_id, require_approval FROM campaigns").fetchone()
    assert tuple(row) == (None, 0)
    assert "Pick a playbook" in " ".join(campaigns.go_live_blockers(conn, campaign_id))
