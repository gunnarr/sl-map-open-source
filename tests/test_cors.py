"""Tests for the CORS policy in server.py.

P0 — CORS controls which third-party origins a browser will allow to read
responses from our API. A misconfigured Access-Control-Allow-Origin header
either:

  - blocks our own frontend (regression visible to every user), or
  - leaks our endpoints to arbitrary websites that can then scrape them.

The policy under test (server.py, _add_cors):

  - request Origin == https://<SITE_DOMAIN> (or listed in CORS_ORIGINS)
      -> ACAO mirrors the Origin and adds `Vary: Origin`
  - request Origin missing
      -> no ACAO header (same-origin browser path, native HTTP clients)
  - request Origin not in allowlist
      -> no ACAO header (browser blocks third-party JS from reading body)

`Access-Control-Allow-Methods` and `Access-Control-Allow-Headers` are
returned unconditionally and are unchanged by this policy.

We hit /api/version (and /api/visitors as a second lightweight endpoint)
rather than /api/vehicles to avoid the Trafiklab upstream call.
"""

import server

HOST = server.SITE_DOMAIN
ALLOWED_ORIGIN = f"https://{HOST}"


# ---------------------------------------------------------------------------
# Same-origin / no Origin header
# ---------------------------------------------------------------------------

class TestNoOriginHeader:
    """Browsers send no Origin on same-origin GETs; native clients send none.

    Our own map.html and the screensaver WKWebView are same-origin with the
    API, so the normal user path is `Origin` absent. We must not break it.
    """

    def test_no_origin_returns_200(self, client):
        """Request without Origin still succeeds — the policy is browser-side."""
        resp = client.get("/api/version")
        assert resp.status_code == 200

    def test_no_origin_omits_acao_header(self, client):
        """No Origin -> no Access-Control-Allow-Origin. Header must be absent."""
        resp = client.get("/api/version")
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_no_origin_omits_vary_origin(self, client):
        """Without an allowlisted Origin, we don't need to vary on it."""
        resp = client.get("/api/version")
        # Vary may or may not be present from Flask defaults; if present it
        # must not contain "Origin" since we didn't echo an Origin.
        vary = resp.headers.get("Vary", "")
        assert "Origin" not in vary

    def test_no_origin_on_visitors_also_omits_acao(self, client):
        """Sanity check that the policy applies to every endpoint, not just version."""
        resp = client.get("/api/visitors")
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers


# ---------------------------------------------------------------------------
# Allowed origin
# ---------------------------------------------------------------------------

class TestAllowedOrigin:
    """https://<SITE_DOMAIN> is the frontend host."""

    def test_allowed_origin_is_echoed(self, client):
        """ACAO must mirror the exact request Origin, not a wildcard."""
        resp = client.get("/api/version", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.status_code == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN

    def test_allowed_origin_adds_vary_origin(self, client):
        """Vary: Origin tells caches the response depends on the Origin header.

        Without this, an intermediate cache could serve a response with the
        allowed-origin ACAO to a request from a different (disallowed) origin.
        """
        resp = client.get("/api/version", headers={"Origin": ALLOWED_ORIGIN})
        vary = resp.headers.get("Vary", "")
        assert "Origin" in vary

    def test_allowed_origin_not_wildcard(self, client):
        """Regression — we used to send '*'. The new policy must not."""
        resp = client.get("/api/version", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.headers.get("Access-Control-Allow-Origin") != "*"

    def test_allowed_origin_on_visitors(self, client):
        """Policy applies to every after_request response, not only version."""
        resp = client.get("/api/visitors", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.status_code == 200
        assert resp.headers.get("Access-Control-Allow-Origin") == ALLOWED_ORIGIN


# ---------------------------------------------------------------------------
# Disallowed origins
# ---------------------------------------------------------------------------

class TestDisallowedOrigins:
    """Third-party sites must not be told they're allowed to read our API.

    The server still returns 200 with the body — CORS is browser-side. The
    browser refuses to expose the body to JS because ACAO is missing.
    """

    def test_unknown_origin_returns_200_but_no_acao(self, client):
        """fikastockholm.com is a real-but-unrelated site — must be denied."""
        resp = client.get("/api/version", headers={"Origin": "https://fikastockholm.com"})
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_evil_example_origin_denied(self, client):
        """Hostile-sounding origin — denied like any other unknown."""
        resp = client.get("/api/version", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_http_variant_of_allowed_host_denied(self, client):
        """`http://` is not `https://` — schemes must match exactly."""
        resp = client.get("/api/version", headers={"Origin": f"http://{HOST}"})
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_null_origin_denied(self, client):
        """`Origin: null` is what sandboxed iframes / file:// pages send."""
        resp = client.get("/api/version", headers={"Origin": "null"})
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_disallowed_origin_not_echoed_as_value(self, client):
        """Defense in depth — if logic broke and echoed, ACAO would equal the bad origin."""
        bad = "https://attacker.test"
        resp = client.get("/api/version", headers={"Origin": bad})
        # Either absent (preferred) or definitely not the attacker origin.
        assert resp.headers.get("Access-Control-Allow-Origin") != bad


# ---------------------------------------------------------------------------
# Prefix / substring tricks
# ---------------------------------------------------------------------------

class TestExactMatchOnly:
    """Verifies the allowlist check is exact equality, not substring/prefix.

    A naive `if SITE_DOMAIN in origin` would allow
    `https://<SITE_DOMAIN>.evil.com`, which is owned by an attacker.
    """

    def test_suffix_attack_origin_denied(self, client):
        """`<host>.evil.com` contains the allowed host as a prefix."""
        resp = client.get(
            "/api/version",
            headers={"Origin": f"https://{HOST}.evil.com"},
        )
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_prefix_attack_origin_denied(self, client):
        """`evil<host>` contains the allowed host as a suffix."""
        resp = client.get(
            "/api/version",
            headers={"Origin": f"https://evil{HOST}"},
        )
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_subdomain_of_allowed_host_denied(self, client):
        """Subdomains are not auto-trusted — only the exact host is."""
        resp = client.get(
            "/api/version",
            headers={"Origin": f"https://x.{HOST}"},
        )
        assert resp.status_code == 200
        assert "Access-Control-Allow-Origin" not in resp.headers

    def test_trailing_slash_origin_denied(self, client):
        """An Origin header should never include a path, but if it does, deny.

        `Origin` per RFC 6454 is scheme://host[:port] only. Anything else is
        malformed and must not match.
        """
        resp = client.get(
            "/api/version",
            headers={"Origin": f"https://{HOST}/"},
        )
        assert "Access-Control-Allow-Origin" not in resp.headers


# ---------------------------------------------------------------------------
# Allow-Methods / Allow-Headers unchanged
# ---------------------------------------------------------------------------

class TestMethodsAndHeadersUnchanged:
    """ACAM and ACAH are harmless without ACAO and stay on every response."""

    EXPECTED_METHODS = "GET, POST, OPTIONS"
    EXPECTED_HEADERS = "Content-Type"

    def test_methods_present_with_no_origin(self, client):
        resp = client.get("/api/version")
        assert resp.headers.get("Access-Control-Allow-Methods") == self.EXPECTED_METHODS

    def test_headers_present_with_no_origin(self, client):
        resp = client.get("/api/version")
        assert resp.headers.get("Access-Control-Allow-Headers") == self.EXPECTED_HEADERS

    def test_methods_present_with_allowed_origin(self, client):
        resp = client.get("/api/version", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.headers.get("Access-Control-Allow-Methods") == self.EXPECTED_METHODS

    def test_headers_present_with_allowed_origin(self, client):
        resp = client.get("/api/version", headers={"Origin": ALLOWED_ORIGIN})
        assert resp.headers.get("Access-Control-Allow-Headers") == self.EXPECTED_HEADERS

    def test_methods_present_with_disallowed_origin(self, client):
        resp = client.get("/api/version", headers={"Origin": "https://evil.example"})
        assert resp.headers.get("Access-Control-Allow-Methods") == self.EXPECTED_METHODS

    def test_headers_present_with_disallowed_origin(self, client):
        resp = client.get("/api/version", headers={"Origin": "https://evil.example"})
        assert resp.headers.get("Access-Control-Allow-Headers") == self.EXPECTED_HEADERS


# ---------------------------------------------------------------------------
# Where the allowlist comes from
# ---------------------------------------------------------------------------

class TestAllowlistConfig:
    """https://<SITE_DOMAIN> always; CORS_ORIGINS adds more (comma-separated)."""

    def test_site_domain_only_by_default(self):
        assert server._build_allowed_origins("map.example.org", "") == {"https://map.example.org"}

    def test_cors_origins_adds_entries(self):
        got = server._build_allowed_origins("map.example.org", " http://localhost:8080, https://b.example/ ,")
        assert got == {"https://map.example.org", "http://localhost:8080", "https://b.example"}

    def test_live_allowlist_matches_site_domain(self):
        assert ALLOWED_ORIGIN in server._ALLOWED_ORIGINS
