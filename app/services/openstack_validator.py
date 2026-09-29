"""Authorize an OpenStack credential payload against the target Keystone.

Used by the upsert and `/test` endpoints. The 15-second socket boundary
keeps a stuck Keystone from blocking the FastAPI worker; openstacksdk
otherwise has no consistent timeout knob across releases.
"""
from __future__ import annotations

import socket

import openstack
from openstack import exceptions as os_exc

from app.schemas import OpenStackCredentialUpsert
from app.services.openstack_client import build_connect_kwargs

_TIMEOUT_SECONDS = 15


def validate(payload: OpenStackCredentialUpsert) -> tuple[bool, str | None]:
    """Try to authorize. Returns (ok, error_message).

    The error message is short and human-readable — safe to surface in the
    UI. Never echoes the secret back.
    """
    prev_default_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_TIMEOUT_SECONDS)
    try:
        conn = openstack.connect(**build_connect_kwargs(payload.model_dump()))
        # Force a token round-trip; .authorize() returns the token string.
        conn.authorize()
        return True, None
    except os_exc.HttpException as e:
        status = getattr(e, "status_code", None) or getattr(e, "http_status", None)
        if status in (401, 403):
            return False, "Invalid credentials"
        if status == 404:
            return False, "Project or domain not found"
        return False, f"OpenStack rejected request (HTTP {status or '?'})"
    except (TimeoutError, socket.gaierror) as e:
        return False, f"Could not reach auth_url: {e}"
    except os_exc.SDKException as e:
        return False, f"OpenStack SDK error: {type(e).__name__}"
    except Exception as e:
        # Unknown error — return the type only, never the message (might
        # contain the request body).
        return False, f"Unexpected error: {type(e).__name__}"
    finally:
        socket.setdefaulttimeout(prev_default_timeout)
