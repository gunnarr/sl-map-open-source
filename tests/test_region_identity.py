"""Region config = shareable config.json + optional identity.private.json.

config.json describes the region and its transit data. Who runs the site
(domain, contact addresses, author) can live in identity.private.json next to
it, merged on top at startup, so config.json itself can be published.
"""

import json

import server


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_config_alone(tmp_path):
    _write(tmp_path / "config.json", {"branding": {"app_name": "X"}, "identity": {"launch_date": "2026-01-01"}})
    assert server._load_region_config(tmp_path) == {
        "branding": {"app_name": "X"}, "identity": {"launch_date": "2026-01-01"}}


def test_identity_overlay_is_merged_deeply(tmp_path):
    _write(tmp_path / "config.json", {"branding": {"app_name": "X"}, "identity": {"launch_date": "2026-01-01"}})
    _write(tmp_path / "identity.private.json",
           {"identity": {"site_domain": "map.example.org"}, "branding": {"footer_link_text": "me"}})
    assert server._load_region_config(tmp_path) == {
        "branding": {"app_name": "X", "footer_link_text": "me"},
        "identity": {"launch_date": "2026-01-01", "site_domain": "map.example.org"},
    }


def test_overlay_values_win(tmp_path):
    _write(tmp_path / "config.json", {"identity": {"site_domain": "public.example"}})
    _write(tmp_path / "identity.private.json", {"identity": {"site_domain": "mine.example"}})
    assert server._load_region_config(tmp_path)["identity"]["site_domain"] == "mine.example"


def test_missing_region_dir_gives_empty_config(tmp_path):
    assert server._load_region_config(tmp_path / "nope") == {}


def test_private_json_is_never_served(client, app):
    region = server.BASE_DIR / "regions" / "sl"
    region.mkdir(parents=True)
    _write(region / "identity.private.json", {"identity": {"email_to": "someone@example.org"}})
    _write(region / "config.json", {"region_id": "sl"})
    assert client.get("/regions/sl/identity.private.json").status_code == 404
    assert client.get("/identity.private.json").status_code == 404
    assert client.get("/regions/sl/config.json").status_code == 200


def test_shared_region_configs_carry_no_contact_addresses():
    for config in (server.BASE_DIR / "regions").glob("*/config.json"):
        identity = json.loads(config.read_text(encoding="utf-8")).get("identity", {})
        assert not any(k.startswith("email_") for k in identity), config
        assert "site_domain" not in identity, config
