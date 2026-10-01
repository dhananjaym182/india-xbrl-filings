"""HTTP transport for NSE endpoints.

Two hard requirements measured against live endpoints (2026-10-01):

1. **HTTP/1.1 is required** for ``nsearchives.nseindia.com``. HTTP/2 to that
   host fails with an internal error (``curl: (92) INTERNAL_ERROR``). We force
   ``requests`` onto HTTP/1.1 with an ``HTTPAdapter`` that pins
   ``urllib3.util.connection.allowed_gai_family`` is *not* needed here because
   requests/urllib3 default to HTTP/1.1 -- the danger is other clients. This
   adapter exists to make the constraint explicit and testable.
2. **No cookies, no session priming.** The discovery APIs return HTTP 200 with
   only a browser User-Agent, byte-identical with and without cookie priming.
   A cookie layer is unnecessary complexity that rots; we do not build one.

We deliberately keep the transport surface tiny (``get`` + ``stream``) so tests
can substitute a fake transport with zero network access.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

#: BSE (api.bseindia.com / www.bseindia.com) sits behind Akamai with a
#: browser-fingerprint rule. Measured 2026-10-01: a Chrome UA *alone* is not
#: enough -- requests without ``Accept-Encoding`` (and, empirically, the
#: sec-ch-ua client hints) are denied with 403 even when the URL is
#: identical to one that just returned 200. The full header set below
#: passes; ``requests`` supplies ``Accept-Encoding: gzip, deflate`` itself.
#: A naive client (default python-requests UA) gets 403 on every path.
BSE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

BSE_HEADERS: dict[str, str] = {
    "User-Agent": BSE_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/corporates/ann",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "sec-ch-ua": '"Chromium";v="153", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

#: Seconds between the *starts* of two consecutive archive requests per worker.
DEFAULT_MIN_INTERVAL = 6.0

#: Backoff multiplier applied per failed attempt.
DEFAULT_BACKOFF_BASE = 6.0

#: Upper bound on a single backoff sleep.
MAX_BACKOFF_SECONDS = 120.0

#: Random jitter fraction (of the backoff) so concurrent workers do not retry
#: in lockstep and re-trigger the rate limit together.
JITTER_FRACTION = 0.25


@dataclass(frozen=True)
class Response:
    """Minimal transport response, independent of ``requests``."""

    status_code: int
    content: bytes
    url: str
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """The tiny HTTP surface the library needs. Mock this in tests."""

    def get(self, url: str, *, timeout: float = 30.0) -> Response:
        """GET a URL, returning the full body."""
        ...


class _ForceHttp11Adapter(requests.adapters.HTTPAdapter):
    """Pins HTTP/1.1 by disabling HTTP/2-capable negotiation.

    ``requests`` (urllib3) never speaks HTTP/2 on its own, so this is
    documentation-as-code: the day someone swaps in an httpx/h2 client, this
    module is the signpost reminding them why HTTP/2 fails against the archive
    host.
    """

    def send(self, *args: object, **kwargs: object) -> requests.Response:
        kwargs.pop("http2", None)  # refuse h2 upgrades even if requested
        return super().send(*args, **kwargs)  # type: ignore[arg-type]


class RequestsTransport:
    """Default transport: a requests.Session with browser UA and HTTP/1.1.

    No cookies, no session priming. Per-host header sets: NSE endpoints get
    :data:`DEFAULT_HEADERS`, BSE hosts get :data:`BSE_HEADERS` (the Akamai
    fingerprint gate -- see its docstring). Pacing and backoff live in
    :mod:`india_xbrl.pacing` where they can be tested.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)
        adapter = _ForceHttp11Adapter(pool_connections=4, pool_maxsize=16)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def get(self, url: str, *, timeout: float = 30.0) -> Response:
        headers = BSE_HEADERS if "bseindia.com" in url else None
        resp = self._session.get(url, timeout=timeout or self._timeout, headers=headers)
        return Response(
            status_code=resp.status_code,
            content=resp.content,
            url=resp.url,
            headers=dict(resp.headers),
        )

    def close(self) -> None:
        self._session.close()


class Clock(Protocol):
    """Time source, injectable for deterministic pacing tests."""

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...


class RealClock:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Records sleeps instead of performing them; advances a virtual clock."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._now = 0.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds

    def monotonic(self) -> float:
        return self._now
