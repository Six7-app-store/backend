"""FastAPI bindings for PyLTI1p3.

``pylti1p3.contrib`` ships adapters for Django and Flask only. The
library itself is framework-neutral: it declares what it needs from the
host framework as abstract classes, and each contrib package fills them
in. This module is that package for FastAPI/Starlette.

Nothing here makes LTI decisions — it only translates between
Starlette's request/response objects and the shapes PyLTI1p3 expects.
The protocol work lives in :mod:`app.services.lti_service` and
:mod:`app.routers.lti`.

Two adaptations are not obvious from the Flask original:

**Form data is async in Starlette.** PyLTI1p3 calls ``get_param()``
synchronously and there is no way to ``await`` inside the library, so
the endpoint must read the form first and hand the finished dict to
:class:`FastApiRequest`.

**There is no session.** PyLTI1p3's default storage for ``state`` and
``nonce`` is ``request.session``, which Flask provides and FastAPI does
not. :class:`RedisLaunchDataStorage` replaces it with the Redis instance
that already runs in the stack. ``FastApiRequest.session`` still exists
because :class:`~pylti1p3.session.SessionService` touches it in its
constructor, but with the cache storage passed in nothing is ever read
from it.
"""
from __future__ import annotations

import json
import typing as t

import redis
from fastapi.responses import HTMLResponse, RedirectResponse
from pylti1p3.cookie import CookieService
from pylti1p3.launch_data_storage.cache import CacheDataStorage
from pylti1p3.message_launch import MessageLaunch
from pylti1p3.oidc_login import OIDCLogin
from pylti1p3.redirect import Redirect
from pylti1p3.request import Request
from pylti1p3.session import SessionService
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

# Fallback lifetime for cache entries PyLTI1p3 writes without an
# explicit expiry. One hour outlives any launch round trip and keeps
# stray keys from accumulating in Redis for good.
_DEFAULT_CACHE_TTL_SECONDS = 3600


# ----------------------------------------------------------------
# REQUEST
# ----------------------------------------------------------------
class FastApiRequest(Request):
    """Wraps a Starlette request in the interface PyLTI1p3 expects.

    ``form_data`` must be supplied by the caller for POST requests —
    see the module docstring on async form parsing.
    """

    def __init__(
        self,
        request: StarletteRequest,
        form_data: t.Mapping[str, t.Any] | None = None,
    ) -> None:
        super().__init__()
        self._request = request
        self._form_data = dict(form_data) if form_data else {}
        # Only here to satisfy SessionService's constructor; the cache
        # storage takes over before anything is stored.
        self._session: dict = {}

    @property
    def session(self) -> dict:
        return self._session

    def is_secure(self) -> bool:
        # Behind a TLS-terminating proxy Starlette rewrites the scheme
        # from the forwarded headers, so this stays correct in prod as
        # long as uvicorn runs with --proxy-headers.
        return self._request.url.scheme == "https"

    def get_param(self, key: str) -> t.Any:
        # POST carries the LTI parameters in the body, GET in the query
        # string. Some platforms send the login initiation as GET, so
        # both have to work.
        if key in self._form_data:
            return self._form_data[key]
        return self._request.query_params.get(key)

    def get_cookie(self, key: str) -> str | None:
        return self._request.cookies.get(key)


# ----------------------------------------------------------------
# COOKIES
# ----------------------------------------------------------------
class FastApiCookieService(CookieService):
    """Collects cookies during the flow, writes them on the way out.

    PyLTI1p3 sets cookies at a point where no response object exists
    yet, so they are buffered and applied in ``update_response()``.
    """

    def __init__(self, request: FastApiRequest) -> None:
        self._request = request
        self._cookie_data_to_set: dict[str, dict] = {}

    def _get_key(self, key: str) -> str:
        return self._cookie_prefix + "-" + key

    def get_cookie(self, name: str) -> str | None:
        return self._request.get_cookie(self._get_key(name))

    def set_cookie(self, name: str, value: str | int, exp: int | None = 3600):
        self._cookie_data_to_set[self._get_key(name)] = {"value": value, "exp": exp}

    def update_response(self, response: Response) -> Response:
        secure = self._request.is_secure()
        for key, data in self._cookie_data_to_set.items():
            kwargs: dict[str, t.Any] = {
                "key": key,
                "value": str(data["value"]),
                "max_age": data["exp"],
                "path": "/",
                "httponly": True,
                "secure": secure,
            }
            # SameSite=None requires Secure. Over plain http (local dev)
            # the pair is invalid and browsers drop the cookie outright,
            # so fall back to Lax there — which is also why the launch
            # has to open in a new window rather than Moodle's iframe
            # when running without TLS.
            kwargs["samesite"] = "none" if secure else "lax"
            response.set_cookie(**kwargs)
        return response


# ----------------------------------------------------------------
# REDIRECTS
# ----------------------------------------------------------------
class FastApiRedirect(Redirect):
    def __init__(
        self,
        location: str,
        cookie_service: FastApiCookieService | None = None,
    ) -> None:
        super().__init__()
        self._location = location
        self._cookie_service = cookie_service

    def do_redirect(self) -> Response:
        return self._process_response(RedirectResponse(self._location, status_code=302))

    def do_js_redirect(self) -> Response:
        # Used when a plain 302 would lose the cookie (some in-frame
        # cases). The location is JSON-encoded, not string-interpolated,
        # so a crafted target_link_uri cannot break out of the string
        # literal and inject script.
        location = json.dumps(self._location)
        return self._process_response(
            HTMLResponse(
                f'<script type="text/javascript">window.location={location};</script>'
            )
        )

    def set_redirect_url(self, location: str) -> None:
        self._location = location

    def get_redirect_url(self) -> str:
        return self._location

    def _process_response(self, response: Response) -> Response:
        if self._cookie_service:
            self._cookie_service.update_response(response)
        return response


# ----------------------------------------------------------------
# LAUNCH DATA STORAGE
# ----------------------------------------------------------------
class ConsumingCacheDataStorage(CacheDataStorage):
    """A cache storage whose ``check_value()`` spends the key it checks.

    The base class answers ``check_value()`` with a plain existence
    lookup and leaves the key in place. Its only caller is
    ``SessionService.check_nonce()``, so an unspent nonce stays valid
    for its whole lifetime — and a captured ``id_token`` can then be
    replayed until its own ``exp`` passes, which is exactly what the
    nonce exists to prevent.

    Consuming the key on the first check is what makes it single-use.
    Backends therefore have to offer ``get_and_delete()`` as one atomic
    operation: two launches arriving together must not both see the
    nonce and both be let through.
    """

    def check_value(self, key: str) -> bool:
        key = self._prepare_key(key)
        return self._get_cache().get_and_delete(key) is not None


class _RedisShim:
    """The calls ``ConsumingCacheDataStorage`` makes on its ``_cache``.

    PyLTI1p3 stores dicts; Redis stores bytes. Values round-trip through
    JSON, and a missing key must come back as ``None`` rather than
    raising — ``check_value()`` relies on that to decide whether a nonce
    has already been spent.
    """

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    @staticmethod
    def _decode(raw: t.Any) -> t.Any:
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def get(self, key: str) -> t.Any:
        return self._decode(self._client.get(key))

    def get_and_delete(self, key: str) -> t.Any:
        # GETDEL is a single round trip and atomic: of two concurrent
        # replays exactly one gets the value, the other gets None.
        return self._decode(self._client.getdel(key))

    def set(self, key: str, value: t.Any, exp: int | None = None) -> None:
        payload = json.dumps(value)
        # Never write without an expiry. PyLTI1p3 omits ``exp`` on some
        # calls, and a key set without one stays in Redis forever.
        self._client.setex(key, exp or _DEFAULT_CACHE_TTL_SECONDS, payload)


class RedisLaunchDataStorage(ConsumingCacheDataStorage):
    def __init__(self, client: redis.Redis, **kwargs) -> None:
        self._cache = _RedisShim(client)
        super().__init__(**kwargs)


# ----------------------------------------------------------------
# LOGIN / LAUNCH
# ----------------------------------------------------------------
class FastApiSessionService(SessionService):
    pass


class FastApiOIDCLogin(OIDCLogin):
    def __init__(
        self,
        request: FastApiRequest,
        tool_config,
        session_service=None,
        cookie_service=None,
        launch_data_storage=None,
    ) -> None:
        cookie_service = cookie_service or FastApiCookieService(request)
        session_service = session_service or FastApiSessionService(request)
        super().__init__(
            request, tool_config, session_service, cookie_service, launch_data_storage
        )

    def get_redirect(self, url: str) -> FastApiRedirect:
        return FastApiRedirect(url, self._cookie_service)

    def get_response(self, html: str) -> Response:
        # The cookie-check interstitial. It has to carry the cookies it
        # is meant to verify, hence update_response().
        response = HTMLResponse(html)
        return self._cookie_service.update_response(response)


class FastApiMessageLaunch(MessageLaunch):
    def __init__(
        self,
        request: FastApiRequest,
        tool_config,
        session_service=None,
        cookie_service=None,
        launch_data_storage=None,
        requests_session=None,
    ) -> None:
        cookie_service = cookie_service or FastApiCookieService(request)
        session_service = session_service or FastApiSessionService(request)
        super().__init__(
            request,
            tool_config,
            session_service,
            cookie_service,
            launch_data_storage,
            requests_session,
        )

    def _get_request_param(self, key: str) -> t.Any:
        return self._request.get_param(key)
