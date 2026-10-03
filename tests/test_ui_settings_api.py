"""Tests for the UI settings endpoints (accent colour and logos).

Endpoints under test: ``/ui-settings`` and ``/ui-settings/logos/{variant}``.
Reading needs no login — the login page uses it — writing is admin-only.
"""
import base64

import pytest

from app.utils.app_image import MAX_IMAGE_BYTES

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _data_url(data: bytes = PNG_BYTES, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ----------------------------------------------------------------
# READ
# ----------------------------------------------------------------
@pytest.mark.integration
def test_defaults_without_any_row(unauth_client):
    res = unauth_client.get("/ui-settings")

    assert res.status_code == 200
    assert res.json() == {
        "accentColor": None,
        "logos": {"light": False, "dark": False, "icon": False},
        "updatedAt": None,
    }


@pytest.mark.integration
def test_logo_without_upload_is_404(unauth_client):
    res = unauth_client.get("/ui-settings/logos/light")

    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "logo_not_set"


@pytest.mark.integration
def test_unknown_logo_variant_is_rejected(unauth_client):
    assert unauth_client.get("/ui-settings/logos/huge").status_code == 422


# ----------------------------------------------------------------
# ACCENT COLOUR
# ----------------------------------------------------------------
@pytest.mark.integration
def test_admin_sets_accent_color(admin_client):
    res = admin_client.patch("/ui-settings", json={"accentColor": "#1a73e8"})

    assert res.status_code == 200
    body = res.json()
    assert body["accentColor"] == "#1A73E8"
    assert body["updatedAt"] is not None
    assert admin_client.get("/ui-settings").json()["accentColor"] == "#1A73E8"


@pytest.mark.integration
def test_null_resets_accent_and_missing_field_keeps_it(admin_client):
    admin_client.patch("/ui-settings", json={"accentColor": "#1A73E8"})

    assert admin_client.patch("/ui-settings", json={}).json()["accentColor"] == "#1A73E8"
    assert admin_client.patch("/ui-settings", json={"accentColor": None}).json()[
        "accentColor"
    ] is None


@pytest.mark.integration
@pytest.mark.parametrize("value", ["red", "#12345", "#1234567", "#GGGGGG", "1A73E8"])
def test_invalid_accent_color_is_rejected(admin_client, value):
    res = admin_client.patch("/ui-settings", json={"accentColor": value})

    assert res.status_code == 422


@pytest.mark.integration
def test_non_admin_cannot_change_accent(client):
    res = client.patch("/ui-settings", json={"accentColor": "#1A73E8"})

    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "role_required"


# ----------------------------------------------------------------
# LOGOS
# ----------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.parametrize("variant", ["light", "dark", "icon"])
def test_admin_uploads_and_serves_logo(admin_client, variant):
    res = admin_client.put(f"/ui-settings/logos/{variant}", json={"image": _data_url()})

    assert res.status_code == 200
    assert res.json()["logos"][variant] is True

    logo = admin_client.get(f"/ui-settings/logos/{variant}")
    assert logo.status_code == 200
    assert logo.content == PNG_BYTES
    assert logo.headers["content-type"] == "image/png"
    assert "public" in logo.headers["cache-control"]
    assert logo.headers["x-content-type-options"] == "nosniff"


@pytest.mark.integration
def test_svg_logo_is_served_sandboxed(admin_client):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    admin_client.put("/ui-settings/logos/light", json={"image": _data_url(svg, "image/svg+xml")})

    logo = admin_client.get("/ui-settings/logos/light")

    assert logo.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in logo.headers["content-security-policy"]


@pytest.mark.integration
def test_uploading_one_logo_leaves_the_others(admin_client):
    admin_client.put("/ui-settings/logos/dark", json={"image": _data_url()})

    assert admin_client.get("/ui-settings").json()["logos"] == {
        "light": False,
        "dark": True,
        "icon": False,
    }


@pytest.mark.integration
def test_reset_logo_restores_default(admin_client):
    admin_client.put("/ui-settings/logos/icon", json={"image": _data_url()})

    res = admin_client.delete("/ui-settings/logos/icon")

    assert res.status_code == 200
    assert res.json()["logos"]["icon"] is False
    assert admin_client.get("/ui-settings/logos/icon").status_code == 404


@pytest.mark.integration
def test_new_upload_changes_updated_at(admin_client):
    first = admin_client.put("/ui-settings/logos/light", json={"image": _data_url()}).json()
    second = admin_client.put(
        "/ui-settings/logos/light", json={"image": _data_url(PNG_BYTES + b"\x00")}
    ).json()

    assert second["updatedAt"] != first["updatedAt"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "image,expected",
    [
        ("", 422),
        ("not-a-data-url", 422),
        ("data:text/plain;base64,aGVsbG8=", 422),
    ],
)
def test_invalid_logo_is_rejected(admin_client, image, expected):
    res = admin_client.put("/ui-settings/logos/light", json={"image": image})

    assert res.status_code == expected


@pytest.mark.integration
def test_too_large_logo_is_rejected(admin_client):
    res = admin_client.put(
        "/ui-settings/logos/light",
        json={"image": _data_url(b"\x00" * (MAX_IMAGE_BYTES + 1))},
    )

    assert res.status_code == 413


@pytest.mark.integration
def test_non_admin_cannot_upload_or_reset_logo(student_client):
    assert (
        student_client.put("/ui-settings/logos/light", json={"image": _data_url()}).status_code
        == 403
    )
    assert student_client.delete("/ui-settings/logos/light").status_code == 403
