from datetime import timedelta

from outreach_wise import campaigns, db, worker


def test_only_one_worker_holds_the_lease(conn, clock):
    ttl = timedelta(minutes=2)
    assert db.acquire_lease(conn, "worker", "a", ttl)
    assert not db.acquire_lease(conn, "worker", "b", ttl)
    assert db.acquire_lease(conn, "worker", "a", ttl)  # renewal
    clock.advance(minutes=3)  # "a" died without releasing
    assert db.acquire_lease(conn, "worker", "b", ttl)


def test_one_tick_syncs_enrolls_and_sends(conn, cfg, google, account):
    db.save_settings(conn, {"sheet_url": "https://docs.google.com/spreadsheets/d/abc/edit"})
    google.sheet = [["Email", "First Name"], ["jane@leads.io", "Jane"]]
    campaign_id = campaigns.create(conn, "Auto")
    first = campaigns.steps_with_variants(conn, campaign_id)[0]["variants"][0]
    campaigns.update_variant(conn, first["id"], "Hi {{first_name}}", "Hello!", True)
    conn.execute(
        "UPDATE campaigns SET status = 'active', auto_enroll = 1, mode = 'live' WHERE id = ?", (campaign_id,)
    )
    conn.commit()

    worker.tick(conn, cfg, google)

    assert conn.execute("SELECT status FROM leads").fetchone()[0] == "contacted"
    assert [s["mime"]["Subject"] for s in google.sent] == ["Hi Jane"]
    assert db.get_kv(conn, "job:sync") and db.get_kv(conn, "job:inbox")
