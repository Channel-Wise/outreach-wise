import json

import pytest
from conftest import make_campaign

from outreach_wise import campaigns, db, inbox, lists, sender, sheets
from outreach_wise.rendering import MissingVariable, render, required_variables


def msg(sender="Jane <jane@leads.io>", subject="Re: Hi", snippet="Sounds good", **headers):
    return {
        "id": "x",
        "thread_id": "t",
        "snippet": snippet,
        "internal_date": 0,
        "labels": [],
        "headers": {"from": sender, "subject": subject, **headers},
    }


# --- classification ---------------------------------------------------------------------------


def test_classify_reply_bounce_auto_reply_unsubscribe():
    assert inbox.classify(msg()) == "reply"
    assert inbox.classify(msg(sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>")) == "bounce"
    assert inbox.classify(msg(subject="Undeliverable: Hi")) == "bounce"
    assert inbox.classify(msg(subject="Automatic reply: Hi")) == "auto_reply"
    assert inbox.classify(msg(**{"auto-submitted": "auto-replied"})) == "auto_reply"
    assert inbox.classify(msg(snippet="Please remove me from your list")) == "unsubscribe"
    assert inbox.classify(msg(subject="unsubscribe", snippet="")) == "unsubscribe"


def test_our_own_footer_in_quoted_text_is_not_an_unsubscribe():
    snippet = 'Interested, let&#39;s talk! On Wed, Oct 21, 2026 at 10:00 Me &lt;me@x.io&gt; wrote: reply "unsubscribe"'
    assert inbox.classify(msg(snippet=snippet)) == "reply"


def test_quoted_footer_is_ignored_in_any_language():
    turkish = (
        "ilgileniyorum, konuşalım Orhun from Channel Wise &lt;me@sender.io&gt;, 12 Kas 2026 Per, 15:20 "
        'tarihinde şunu yazdı: Hi Eren, ... Just reply "unsubscribe"'
    )
    assert inbox.classify(msg(snippet=turkish), "me@sender.io") == "reply"
    real = "unsubscribe Orhun from Channel Wise &lt;me@sender.io&gt;, 12 Kas 2026 Per, 15:20 tarihinde şunu yazdı:"
    assert inbox.classify(msg(snippet=real), "me@sender.io") == "unsubscribe"


@pytest.mark.parametrize(
    "text",
    [
        "Abonelikten çık",
        "ABONELİKTEN ÇIKMAK İSTİYORUM",
        "Beni listeden çıkarın lütfen.",
        "Beni mail listenizden çıkartır mısınız?",
        "beni listenizden cikarir misiniz",  # typed without Turkish letters
        "Listenizden çıkarmanızı rica ederim.",
        "Lütfen beni listenizden silin",
        "Beni çıkarın",
        "Bana e-posta göndermeyin",
        "Lütfen bir daha mail atmayın",
        "Maillerinizi göndermeyi bırakın",
        "Sizden e-posta almak istemiyorum",
        "Bir daha yazmayın",
        "Benimle iletişime geçmeyin",
        "Aboneliğimi iptal edin",
    ],
)
def test_turkish_unsubscribe_requests(text):
    assert inbox.classify(msg(snippet=text)) == "unsubscribe"
    assert inbox.classify(msg(subject=text, snippet="")) == "unsubscribe"


@pytest.mark.parametrize(
    "text",
    [
        "İlgilenmiyorum, teşekkürler",  # a no, which stops the sequence, but not an unsubscribe
        "Bana mail atın, detayları konuşalım",
        "Beni listeden çıkarmayın lütfen",
        "Abonelikten çıkmak istemiyorum",
        "Kanalımda abonelikten çıkanlar arttı",  # a creator talking about their own channel
        "Aboneliğimi iptal ettim, Premium çok pahalı",
        "Videoyu oynatma listesinden çıkardım",
        "Mail atmayı unutmayın!",
        "Bu e-postayı yanlışlıkla aldıysanız lütfen siliniz ve başkalarına göndermeyiniz.",  # a disclaimer
    ],
)
def test_turkish_replies_that_are_not_unsubscribes(text):
    assert inbox.classify(msg(snippet=text)) == "reply"


def test_quoted_turkish_footer_is_not_an_unsubscribe():
    footer = 'Bir daha e-posta almak istemiyorsanız "abonelikten çık" yazmanız yeterli.'
    assert inbox.asks_to_unsubscribe(footer)  # so only the quote detection keeps it out
    for quote in (
        "Kimden: Orhun Gönderildi: 12 Kasım 2026 Perşembe 15:20 Kime: Eren Konu: Kanalınız",  # Outlook
        "-----Özgün İleti----- Kimden: Orhun",
        "12.11.2026 15:20 tarihinde Orhun yazdı:",  # Thunderbird: a name, not our address
    ):
        snippet = f"Perşembe uygun, konuşalım. {quote} Merhaba Eren, ... {footer}"
        assert inbox.classify(msg(snippet=snippet), "me@sender.io") == "reply", quote
    real = f"Beni listeden çıkarın. Kimden: Orhun Gönderildi: 12 Kasım 2026 {footer}"
    assert inbox.classify(msg(snippet=real), "me@sender.io") == "unsubscribe"


# --- polling ----------------------------------------------------------------------------------


def test_poll_matches_threads_and_ignores_unrelated_mail(conn, cfg, google, account, clock):
    make_campaign(conn, live=True, leads=2)
    conn.execute("UPDATE accounts SET daily_limit = 10")
    sender.run_send_cycle(conn, cfg, google)
    clock.advance(hours=1)
    conn.execute("UPDATE accounts SET next_send_at = NULL")
    sender.run_send_cycle(conn, cfg, google)
    assert len(google.sent) == 2

    google.deliver(thread_id="t1", sender="colleague@leads.io", snippet="Forwarding to my boss", at=clock())
    google.deliver(
        thread_id="t2",
        sender="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification",
        snippet="Address not found",
        at=clock(),
    )
    google.deliver(
        thread_id="zzz", sender="newsletter@shop.com", subject="Big sale", snippet="private", at=clock()
    )
    account = conn.execute("SELECT * FROM accounts").fetchone()
    counts = inbox.poll(conn, cfg, google, account)
    assert counts == {"reply": 1, "bounce": 1, "ignored": 1}

    assert [r[0] for r in conn.execute("SELECT status FROM leads ORDER BY id")] == ["replied", "bounced"]
    assert db.is_suppressed(conn, "lead2@leads.io")
    marks = conn.execute(
        "SELECT to_email, replied_at IS NOT NULL, bounced_at IS NOT NULL FROM messages ORDER BY id"
    )
    assert [tuple(r) for r in marks] == [("lead1@leads.io", 1, 0), ("lead2@leads.io", 0, 1)]
    ignored = conn.execute("SELECT * FROM inbound WHERE kind = 'ignored'").fetchone()
    assert ignored["subject"] is None and ignored["snippet"] is None and ignored["from_email"] is None

    # Polling again does not double count.
    assert inbox.poll(conn, cfg, google, conn.execute("SELECT * FROM accounts").fetchone()) == {}


def test_unsubscribe_link_flow_suppresses_lead(conn, cfg):
    make_campaign(conn, leads=1)
    lead = conn.execute("SELECT * FROM leads").fetchone()
    inbox.apply_outcome(conn, lead["id"], "unsubscribe", db.now())
    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "unsubscribed"
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "unsubscribed"
    assert db.is_suppressed(conn, lead["email"])


# --- sheet sync -------------------------------------------------------------------------------


def test_parse_rows_normalizes_and_deduplicates():
    rows = [
        ["E-mail", "First Name", "Channel URL"],
        ["Jane@Leads.io ", "Jane", "https://youtube.com/@jane"],
        ["jane@leads.io", "Duplicate", ""],
        ["not-an-email", "Bob", ""],
        ["", "", ""],
        ["sam@leads.io", "Sam"],  # short row
    ]
    leads, stats = sheets.parse_rows(rows, "e-mail")
    assert [lead["email"] for lead in leads] == ["jane@leads.io", "sam@leads.io"]
    assert leads[0]["fields"] == {
        "e_mail": "Jane@Leads.io",
        "first_name": "Jane",
        "channel_url": "https://youtube.com/@jane",
    }
    assert stats == {"rows": 4, "no_email": 0, "invalid": 1, "duplicates": 1, "header": True}


def test_headers_keep_turkish_and_accented_letters():
    headers = ["Kanal Adı", "Abone Sayısı", "İletişim", "Şehir/İlçe", "Öğrenci Sayısı", "Ülke", "Prénom"]
    assert [sheets.normalize_key(h) for h in headers] == [
        "kanal_adi",
        "abone_sayisi",
        "iletisim",
        "sehir_ilce",
        "ogrenci_sayisi",
        "ulke",
        "prenom",
    ]
    # What they were before; Python lowercases "İ" to "i" plus a combining dot.
    assert [sheets.legacy_key(h) for h in headers[:3]] == ["kanal_ad", "abone_say_s", "i_leti_im"]


def test_ascii_headers_keep_their_names():
    headers = [
        "E-mail",
        "First Name",
        "Channel URL",
        "  Subscribers (k) ",
        "KANAL ADI",
        "#",
        "".join(map(chr, range(128))),
    ]
    assert [sheets.normalize_key(h) for h in headers] == [sheets.legacy_key(h) for h in headers]
    assert sheets.normalize_key("  Subscribers (k) ") == "subscribers_k"


def test_turkish_sheet_on_a_fresh_install(conn, cfg, google, account):
    db.save_settings(conn, {"sheet_url": "abc123", "sheet_email_column": "E-posta"})
    google.sheet = [
        ["E-posta", "Kanal Adı", "Abone Sayısı"],
        ["eren@example.com", "Eren'in Mutfağı", "120000"],
    ]
    sheets.sync(conn, cfg, google)
    fields = json.loads(conn.execute("SELECT fields FROM leads").fetchone()["fields"])
    assert fields == {"e_posta": "eren@example.com", "kanal_adi": "Eren'in Mutfağı", "abone_sayisi": "120000"}
    assert campaigns.renamed_columns(conn) == {}  # nothing was ever stored under the old names


def test_sync_moves_old_keys_and_old_names_keep_working(conn, cfg, google, account, monkeypatch):
    db.save_settings(conn, {"sheet_url": "abc123", "sheet_email_column": "E-posta"})
    header = ["E-posta", "Kanal Adı", "Abone Sayısı"]
    google.sheet = [
        header,
        ["eren@example.com", "Eren'in Mutfağı", "120000"],
        ["ayse@example.com", "Ayşe Gezer", "8000"],
    ]
    with monkeypatch.context() as before:  # how earlier versions named these columns
        before.setattr(sheets, "normalize_key", sheets.legacy_key)
        sheets.sync(conn, cfg, google)
        older, _ = lists.create(conn, "Eski dosya")  # and a file imported into another list
        sheets.import_rows(
            conn, older, [header, ["can@example.com", "Can'ın Atölyesi", "500"]], "File", "E-posta"
        )
    campaign_id = campaigns.create(conn, "Kanallar")
    first = campaigns.steps_with_variants(conn, campaign_id)[0]["variants"][0]["id"]
    campaigns.update_variant(
        conn, first, "{{kanal_ad}}", "Merhaba {{kanal_ad}}, {{abone_say_s}} abone!", True
    )
    campaigns.enroll(conn, campaign_id)

    def fields(email):
        return json.loads(conn.execute("SELECT fields FROM leads WHERE email = ?", (email,)).fetchone()[0])

    assert fields("eren@example.com")["kanal_ad"] == "Eren'in Mutfağı"

    # After the upgrade: Eren's data changed, Ayşe left the sheet, Mehmet is new.
    google.sheet = [
        header,
        ["eren@example.com", "Eren'in Yeni Mutfağı", "125000"],
        ["mehmet@example.com", "Mehmet Kodluyor", "3000"],
    ]
    sheets.sync(conn, cfg, google)
    assert fields("eren@example.com") == {
        "e_posta": "eren@example.com",
        "kanal_adi": "Eren'in Yeni Mutfağı",  # no stale kanal_ad left behind by the merge
        "abone_sayisi": "125000",
    }
    assert fields("ayse@example.com") == {
        "e_posta": "ayse@example.com",
        "kanal_adi": "Ayşe Gezer",
        "abone_sayisi": "8000",
    }
    assert fields("can@example.com") == {
        "e_posta": "can@example.com",
        "kanal_adi": "Can'ın Atölyesi",
        "abone_sayisi": "500",
    }
    assert campaigns.renamed_columns(conn) == {"kanal_ad": "kanal_adi", "abone_say_s": "abone_sayisi"}

    # Emails written with the old names render every lead, including the new one, with fresh values.
    campaigns.enroll(conn, campaign_id)
    preview = campaigns.preview(conn, cfg, campaign_id, footer="", sample=4)
    assert [r["body"] for r in preview["steps"][0]["variants"][0]["renders"]] == [
        "Merhaba Eren'in Yeni Mutfağı, 125000 abone!",
        "Merhaba Ayşe Gezer, 8000 abone!",
        "Merhaba Can'ın Atölyesi, 500 abone!",
        "Merhaba Mehmet Kodluyor, 3000 abone!",
    ]
    assert preview["missing"] == {}
    assert preview["renamed"] == {"abone_say_s": "abone_sayisi", "kanal_ad": "kanal_adi"}

    # Reported once, in the activity log.
    sheets.sync(conn, cfg, google)
    notes = [r[0] for r in conn.execute("SELECT message FROM events WHERE message LIKE '%is now%'")]
    assert len(notes) == 1 and "{{kanal_ad}} is now {{kanal_adi}}" in notes[0]


def test_sheet_without_header_row_uses_column_letters():
    rows = [["Jane", "jane@leads.io", "Jane's Channel"], ["Sam", "SAM@leads.io"], ["Bob", "not-an-email"]]
    leads, stats = sheets.parse_rows(rows)
    assert stats == {"rows": 3, "no_email": 0, "invalid": 1, "duplicates": 0, "header": False}
    assert [lead["email"] for lead in leads] == ["jane@leads.io", "sam@leads.io"]
    assert leads[0]["fields"] == {"column_a": "Jane", "email": "jane@leads.io", "column_c": "Jane's Channel"}


def test_sheet_without_header_row_uses_configured_names():
    rows = [["jane@leads.io", "Jane", "Cooking"], ["sam@leads.io", "Sam", ""]]
    leads, _ = sheets.parse_rows(rows, column_names="email, First Name, niche")
    assert leads[0]["fields"] == {"email": "jane@leads.io", "first_name": "Jane", "niche": "Cooking"}
    assert leads[1]["fields"] == {"email": "sam@leads.io", "first_name": "Sam"}


def test_column_letters():
    assert [sheets.column_letter(i) for i in (0, 1, 25, 26, 27, 51, 52)] == [
        "a",
        "b",
        "z",
        "aa",
        "ab",
        "az",
        "ba",
    ]


def test_parse_rows_requires_an_email_column():
    with pytest.raises(sheets.SyncError):
        sheets.parse_rows([["Name"], ["Jane"]])


def test_sync_upserts_and_keeps_status(conn, cfg, google, account):
    db.save_settings(conn, {"sheet_url": "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0"})
    google.sheet = [["Email", "Name"], ["a@leads.io", "A"], ["b@leads.io", "B"]]
    assert sheets.sync(conn, cfg, google)["created"] == 2
    conn.execute("UPDATE leads SET status = 'replied' WHERE email = 'a@leads.io'")
    google.sheet = [["Email", "Name"], ["a@leads.io", "A2"], ["b@leads.io", "B"], ["c@leads.io", "C"]]
    stats = sheets.sync(conn, cfg, google)
    assert (stats["created"], stats["updated"]) == (1, 1)
    row = conn.execute("SELECT status, fields FROM leads WHERE email = 'a@leads.io'").fetchone()
    assert row["status"] == "replied" and '"A2"' in row["fields"]


def test_sync_auto_enrolls_into_active_campaigns(conn, cfg, google, account):
    campaign_id = make_campaign(conn, leads=1)
    conn.execute("UPDATE campaigns SET auto_enroll = 1 WHERE id = ?", (campaign_id,))
    db.save_settings(conn, {"sheet_url": "abc123"})
    google.sheet = [["Email"], ["new@leads.io"]]
    assert sheets.sync(conn, cfg, google)["enrolled"] == 1


def test_spreadsheet_id_from_url():
    assert sheets.spreadsheet_id("https://docs.google.com/spreadsheets/d/1AbC-d_9/edit#gid=0") == "1AbC-d_9"
    assert sheets.spreadsheet_id("1AbC-d_9") == "1AbC-d_9"


# --- enrollment & templates ---------------------------------------------------------------------


def test_enroll_skips_suppressed_and_leads_in_other_active_campaigns(conn):
    first = make_campaign(conn, leads=3)
    conn.execute(
        "INSERT INTO leads (email, fields, created_at, updated_at) VALUES ('x@blocked.io', '{}', '', '')"
    )
    db.suppress(conn, "@blocked.io", "manual")
    second = campaigns.create(conn, "Second")
    assert campaigns.count_eligible(conn, second) == 0
    conn.execute("UPDATE enrollments SET status = 'completed' WHERE campaign_id = ?", (first,))
    assert campaigns.count_eligible(conn, second) == 3


def test_delete_step_renumbers(conn):
    campaign_id = make_campaign(conn, leads=0)
    campaigns.add_step(conn, campaign_id)
    steps = campaigns.steps_with_variants(conn, campaign_id)
    assert campaigns.delete_step(conn, steps[1]["id"]) is None
    assert [s["position"] for s in campaigns.steps_with_variants(conn, campaign_id)] == [1, 2]


def test_render_variables_and_fallbacks():
    assert render("Hi {{ first_name | there }}!", {}) == "Hi there!"
    assert render("Hi {{First_Name}}", {"first_name": "Jane"}) == "Hi Jane"
    assert render("{{x|}}ok", {}) == "ok"
    with pytest.raises(MissingVariable):
        render("Hi {{first_name}}", {"first_name": "  "})
    assert required_variables("{{a}} {{b|x}}") == {"a"}


def test_ab_significance():
    assert campaigns.p_value(30, 100, 10, 100) < 0.05
    assert campaigns.p_value(11, 100, 10, 100) > 0.5


def test_rows_without_an_email_are_skipped_quietly():
    rows = [
        ["jane@leads.io", "Jane"],
        ["", "Pending"],
        ["sam@leads.io", "Sam"],
        ["", ""],
        ["   ", "Also pending"],
    ]
    leads, stats = sheets.parse_rows(rows)
    assert [lead["email"] for lead in leads] == ["jane@leads.io", "sam@leads.io"]
    assert (stats["no_email"], stats["invalid"]) == (2, 0)
