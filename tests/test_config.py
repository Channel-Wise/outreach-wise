import json

import pytest

from outreach_wise.config import ConfigError, load_config

BASE = {"SECRET_KEY": "x" * 40, "ADMIN_PASSWORD": "long enough", "BASE_URL": "http://localhost:8010"}
CLIENT = {
    "web": {
        "client_id": "id-123",
        "client_secret": "s3cret",
        "redirect_uris": ["http://localhost:8000/auth/callback"],
    }
}


def test_client_secrets_json_from_file(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(CLIENT))
    cfg = load_config({**BASE, "GOOGLE_CLIENT_SECRETS_JSON": str(path)})
    assert (cfg.google_client_id, cfg.google_client_secret) == ("id-123", "s3cret")
    assert cfg.google_configured and cfg.redirect_uri_mismatch


def test_client_secrets_json_inline_and_matching_redirect():
    client = {"web": {**CLIENT["web"], "redirect_uris": ["http://localhost:8010/oauth/callback"]}}
    cfg = load_config({**BASE, "GOOGLE_CLIENT_SECRETS_JSON": json.dumps(client)})
    assert cfg.google_client_id == "id-123" and not cfg.redirect_uri_mismatch


def test_explicit_variables_win_and_bad_json_fails(tmp_path):
    cfg = load_config(
        {
            **BASE,
            "GOOGLE_CLIENT_ID": "a",
            "GOOGLE_CLIENT_SECRET": "b",
            "GOOGLE_CLIENT_SECRETS_JSON": "missing.json",
        }
    )
    assert cfg.google_client_id == "a"
    with pytest.raises(ConfigError):
        load_config({**BASE, "GOOGLE_CLIENT_SECRETS_JSON": str(tmp_path / "missing.json")})


def test_registered_redirect_uri_on_base_url_is_reused():
    cfg = load_config(
        {**BASE, "BASE_URL": "http://localhost:8000", "GOOGLE_CLIENT_SECRETS_JSON": json.dumps(CLIENT)}
    )
    assert cfg.oauth_redirect_uri == "http://localhost:8000/auth/callback"
    assert not cfg.redirect_uri_mismatch

    from outreach_wise.web import create_app

    rules = {rule.rule for rule in create_app(cfg).url_map.iter_rules()}
    assert {"/oauth/callback", "/auth/callback"} <= rules
