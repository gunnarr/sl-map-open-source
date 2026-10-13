"""Region selection must reach the client and its static data.

Regression for the 2026-09-21 review, finding 12: the backend resolved
trip_lookup.json through REGION_DIR, but `/` and the static route went
straight to BASE_DIR. REGION=skane therefore served Stockholm's map.html and
the root stations file next to Skåne's traffic data.
"""

from unittest.mock import patch


class TestRegionAwareAssets:

    def test_root_prefers_the_regions_own_client(self, client, app, tmp_path):
        import server
        region = tmp_path / "regions" / "skane"
        region.mkdir(parents=True)
        (region / "map.html").write_text("<title>Skåne LIVE</title>")
        (tmp_path / "map.html").write_text("<title>SL Live Map</title>")

        with patch.object(server, "REGION_DIR", region), \
             patch.object(server, "BASE_DIR", tmp_path):
            body = client.get("/").get_data(as_text=True)

        assert "Skåne LIVE" in body

    def test_root_falls_back_when_the_region_has_no_copy(self, client, app, tmp_path):
        import server
        region = tmp_path / "regions" / "skane"
        region.mkdir(parents=True)
        (tmp_path / "map.html").write_text("<title>SL Live Map</title>")

        with patch.object(server, "REGION_DIR", region), \
             patch.object(server, "BASE_DIR", tmp_path):
            body = client.get("/").get_data(as_text=True)

        assert "SL Live Map" in body

    def test_static_file_prefers_the_region(self, client, app, tmp_path):
        import server
        region = tmp_path / "regions" / "skane"
        region.mkdir(parents=True)
        (region / "stations.json").write_text('{"region":"skane"}')
        (tmp_path / "stations.json").write_text('{"region":"sl"}')

        with patch.object(server, "REGION_DIR", region), \
             patch.object(server, "BASE_DIR", tmp_path):
            body = client.get("/stations.json").get_data(as_text=True)

        assert "skane" in body

    def test_default_region_is_unaffected(self, client, app, tmp_path):
        """With REGION_DIR == BASE_DIR nothing changes."""
        import server
        (tmp_path / "map.html").write_text("<title>SL Live Map</title>")

        with patch.object(server, "REGION_DIR", tmp_path), \
             patch.object(server, "BASE_DIR", tmp_path):
            body = client.get("/").get_data(as_text=True)

        assert "SL Live Map" in body
