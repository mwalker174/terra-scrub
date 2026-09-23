"""The ONLY network primitive in terra-scrub's GET-only modules.

Everything that talks to GCS or Terra goes through :func:`api_get`, which issues
an HTTP GET and nothing else -- except the two single-object stats
(``plan.live_stat`` and ``verify``'s stat), which call ``session.get`` directly
because a 404 there is an expected answer, not an error to raise. Those are GETs
too. There is deliberately no code path for
upload/copy/patch/delete; ``tests/test_readonly_lint.py`` enforces that by AST.

Auth is Application Default Credentials (``gcloud auth application-default
login`` or a service-account key via ``GOOGLE_APPLICATION_CREDENTIALS``). It is
resolved lazily, on the first call, so importing this module never touches the
network or the credential store.
"""
from __future__ import annotations

import os

# Terra orchestration API (the "FireCloud" API). Overridable for dev/staging.
TERRA_API = os.environ.get("TERRA_API_URL", "https://api.firecloud.org").rstrip("/")
GCS_API = "https://storage.googleapis.com/storage/v1"

# Scopes requested when the credential type needs them (service accounts,
# workload identity). User ADC credentials ignore this and use the scopes they
# were minted with. userinfo.email/profile are what Terra checks.
_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)


def _authed_session():
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=list(_SCOPES))
    return google.auth.transport.requests.AuthorizedSession(creds)


def api_get(url, params=None, session=None, *, timeout=120):
    """The toolkit's network primitive: GET, with a timeout (seconds) so a stalled
    connection cannot hang a listing forever."""
    sess = session or _authed_session()
    r = sess.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r
