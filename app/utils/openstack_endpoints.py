"""Operator policy for user-supplied Keystone authentication URLs."""
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.config import settings


def ensure_allowed_auth_url(auth_url: str) -> None:
    """Reject malformed URLs and, when configured, non-allowlisted URLs.

    Exact matching includes the scheme, port and path. This check does not
    replace an egress firewall for redirects or service-catalog endpoints.
    """
    try:
        parsed = urlsplit(auth_url)
        _ = parsed.port  # Access checks malformed and out-of-range ports.
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if settings.OPENSTACK_ENFORCE_ALLOWED_AUTH_URLS and not settings.OPENSTACK_ALLOWED_AUTH_URLS:
        raise HTTPException(status_code=503, detail={"reason": "openstack_auth_url_policy_missing"})
    if not valid or (
        settings.OPENSTACK_ALLOWED_AUTH_URLS
        and auth_url.rstrip("/") not in {url.rstrip("/") for url in settings.OPENSTACK_ALLOWED_AUTH_URLS}
    ):
        raise HTTPException(status_code=422, detail={"reason": "openstack_auth_url_not_allowed"})
