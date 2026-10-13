"""/health must notice when the worker count drifts from what was configured.

On 2026-08-26 a gunicorn worker deadlocked in a futex immediately after fork,
before it had loaded the app. It never served a request and never died. The
master kept counting it, so the service ran a worker short for 29 days and
nothing said a word — it surfaced only because someone counted processes by
hand while chasing an unrelated problem.

Uptime-Kuma polls /health, so a non-200 there is the alarm that would have
caught it the same day. nginx does not route on /health, so failing it cannot
take the site down.

The grace period matters: gunicorn spawns new workers before retiring the old
ones, so the count is legitimately high for a few seconds on every reload, and
reloads happen daily from cron. Alarming on that would train us to ignore it.
"""

import time

import pytest

import server


@pytest.fixture(autouse=True)
def _reset():
    server._worker_mismatch_since = None
    yield
    server._worker_mismatch_since = None


class TestParsingTheConfiguredCount:
    """The expected count comes from the master's own command line, so it
    cannot drift from the unit file the way a hardcoded constant would."""

    def test_space_separated_long_flag(self):
        assert server._parse_worker_count(
            [b"gunicorn", b"server:app", b"--workers", b"3"]) == 3

    def test_equals_form(self):
        assert server._parse_worker_count(
            [b"gunicorn", b"--workers=5", b"server:app"]) == 5

    def test_short_flag(self):
        assert server._parse_worker_count(
            [b"gunicorn", b"-w", b"2", b"server:app"]) == 2

    def test_absent_flag_is_unknown(self):
        """gunicorn defaults to 1, but we will not guess on its behalf."""
        assert server._parse_worker_count([b"gunicorn", b"server:app"]) is None

    def test_garbage_value_is_unknown(self):
        assert server._parse_worker_count(
            [b"gunicorn", b"--workers", b"abc"]) is None

    def test_trailing_flag_without_value(self):
        assert server._parse_worker_count([b"gunicorn", b"--workers"]) is None


class TestTheCheck:

    def test_matching_count_is_ok(self, monkeypatch):
        monkeypatch.setattr(server, "_worker_census", lambda: (3, 3))
        entry, ok = server._check_workers()
        assert ok is True
        assert entry["status"] == "ok"
        assert entry["live"] == 3 and entry["expected"] == 3

    def test_mismatch_inside_grace_does_not_alarm(self, monkeypatch):
        """A reload legitimately shows extra workers for a few seconds."""
        monkeypatch.setattr(server, "_worker_census", lambda: (4, 3))
        entry, ok = server._check_workers()
        assert ok is True, "a fresh mismatch must not fail the healthcheck"
        assert entry["status"] == "settling"

    def test_mismatch_past_grace_fails_the_check(self, monkeypatch):
        monkeypatch.setattr(server, "_worker_census", lambda: (4, 3))
        server._check_workers()
        server._worker_mismatch_since = time.time() - server.WORKER_MISMATCH_GRACE - 1

        entry, ok = server._check_workers()

        assert ok is False, "a persistent mismatch is the whole point"
        assert entry["status"] == "degraded"
        assert entry["live"] == 4 and entry["expected"] == 3
        assert "message" in entry

    def test_the_august_case_too_few_workers(self, monkeypatch):
        """Drift in either direction counts — a dead worker can show up as a
        surplus child or as a shortfall depending on whether the master has
        reaped it yet."""
        monkeypatch.setattr(server, "_worker_census", lambda: (2, 3))
        server._check_workers()
        server._worker_mismatch_since = time.time() - server.WORKER_MISMATCH_GRACE - 1

        entry, ok = server._check_workers()
        assert ok is False
        assert entry["status"] == "degraded"

    def test_recovery_clears_the_timer(self, monkeypatch):
        counts = iter([(4, 3), (3, 3), (4, 3)])
        monkeypatch.setattr(server, "_worker_census", lambda: next(counts))

        server._check_workers()                       # mismatch starts the clock
        server._check_workers()                       # recovery
        assert server._worker_mismatch_since is None

        entry, ok = server._check_workers()           # new mismatch, fresh clock
        assert ok is True
        assert entry["status"] == "settling"

    def test_unavailable_census_never_fails_the_check(self, monkeypatch):
        """No /proc (macOS dev, a container without it) must not fail /health
        — an unknown is not a fault."""
        monkeypatch.setattr(server, "_worker_census", lambda: (None, None))
        entry, ok = server._check_workers()
        assert ok is True
        assert entry["status"] == "unknown"


class TestThroughTheEndpoint:
    """These need every *other* check green, or the 503 proves nothing.

    conftest clears the API keys, so the freshness check fails by default and
    /health is already 503 before this feature exists. A status-code assertion
    without this fixture would pass against unmodified code.
    """

    @pytest.fixture()
    def healthy_upstream(self, monkeypatch):
        monkeypatch.setattr(server, "_fetch_vehicle_positions", lambda: {
            "timestamp": int(time.time()),
            "vehicles": [{"id": "v1", "lat": 59.33, "lon": 18.07}],
        })
        monkeypatch.setattr(server, "_trip_lookup", {"t1": [0, 0, 401, 0]})
        return True

    def test_baseline_is_green_without_this_feature(self, client, monkeypatch, healthy_upstream):
        """Guard for the two tests below: with the census unknown, /health is
        200. If this ever fails, their 503 assertions stop meaning anything."""
        monkeypatch.setattr(server, "_worker_census", lambda: (None, None))
        assert client.get("/health").status_code == 200

    def test_health_reports_the_worker_check(self, client, monkeypatch, healthy_upstream):
        monkeypatch.setattr(server, "_worker_census", lambda: (3, 3))
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["checks"]["workers"]["status"] == "ok"

    def test_persistent_mismatch_makes_health_fail(self, client, monkeypatch, healthy_upstream):
        monkeypatch.setattr(server, "_worker_census", lambda: (4, 3))
        server._check_workers()
        server._worker_mismatch_since = time.time() - server.WORKER_MISMATCH_GRACE - 1

        resp = client.get("/health")

        assert resp.status_code == 503, "Uptime-Kuma only alarms on non-200"
        assert resp.get_json()["checks"]["workers"]["status"] == "degraded"

    def test_settling_mismatch_leaves_health_green(self, client, monkeypatch, healthy_upstream):
        """A reload blip must not page anyone."""
        monkeypatch.setattr(server, "_worker_census", lambda: (4, 3))
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.get_json()["checks"]["workers"]["status"] == "settling"
