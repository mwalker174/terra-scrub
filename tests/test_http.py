"""http._authed_session: the connection pool must hold every stat worker's connection."""
import google.auth
import google.auth.credentials

from terra_scrub import http


class _Creds(google.auth.credentials.Credentials):
    def refresh(self, request):  # never reached: no request is made
        raise AssertionError("offline test must not refresh credentials")


def test_pool_holds_verify_default_workers(monkeypatch):
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_Creds(), "p"))
    sess = http._authed_session()
    adapter = sess.get_adapter("https://storage.googleapis.com/storage/v1/b/x/o")
    # verify defaults to 24 workers; requests' own default of 10 would drop 14
    # connections per round and re-handshake TLS for each
    assert adapter._pool_maxsize >= 24
    assert adapter._pool_maxsize == http.POOL_MAXSIZE


def test_duration_and_step_times():
    from terra_scrub.util import duration, step_times
    assert [duration(x) for x in (0.44, 59.9, 60, 312, 3599, 3600, 7380)] == \
        ["0.4s", "59.9s", "1m00s", "5m12s", "59m59s", "1h00m", "2h03m"]
    assert step_times({}) == ""
    assert step_times({"snapshot": 312, "plan": 48.2}) == \
        "snapshot 5m12s, plan 48.2s (total 6m00s)"
    assert step_times({"plan": 1, "odd": 2, "snapshot": 3}, ("snapshot", "plan")) == \
        "snapshot 3.0s, plan 1.0s, odd 2.0s (total 6.0s)"
