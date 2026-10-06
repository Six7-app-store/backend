"""Regressionstests für die Korrektheitsfehler aus Phase 1 des Refactor-Plans
(``REFACTOR_PLAN.md``, Befunde B-xx).

Jeder Test ist zuerst gegen den fehlerhaften Stand rot gelaufen.
"""
import pytest


# ================================================================
# B-01 · Ein Nicht-Semver-Tag darf die Versionsliste nicht zerstören
# ================================================================
def _git_service_with_tags(tags):
    from app.services.git_service import GitService

    svc = GitService.__new__(GitService)
    svc.token = "T"
    svc._parse_git_url = lambda _url: {
        "platform": "github", "host": "github.com", "owner": "o", "repo": "r",
    }
    svc._fetch_github_tags = lambda _parsed: [{"version": t} for t in tags]
    svc._fetch_github_releases = lambda _parsed: {}
    return svc


@pytest.mark.unit
def test_get_versions_keeps_every_tag_when_some_are_not_semver():
    """B-01: ``sort_key`` mischte ``(1, 2, 0)`` mit ``('latest',)``; der
    Vergleich ``int < str`` warf ``TypeError``, und ``GET /apps/{id}`` zeigte
    für die App danach gar keine Version mehr."""
    svc = _git_service_with_tags(["v1.2.0", "latest", "v1.3.0-rc1", "v1.10.0"])

    versions = [v["version"] for v in svc.get_versions("https://github.com/o/r")]

    assert sorted(versions) == sorted(["v1.2.0", "latest", "v1.3.0-rc1", "v1.10.0"])
    # Echte Versionen zuerst, absteigend und numerisch (1.10 > 1.2), der Rest danach.
    assert versions[:2] == ["v1.10.0", "v1.2.0"]


@pytest.mark.unit
def test_get_versions_sorts_plain_semver_descending():
    svc = _git_service_with_tags(["v1.0.0", "v2.0.0", "v1.5.3"])

    versions = [v["version"] for v in svc.get_versions("https://github.com/o/r")]

    assert versions == ["v2.0.0", "v1.5.3", "v1.0.0"]


# ================================================================
# B-02 · Das Ausgabe-Schema einer App darf kein DNS auflösen
# ================================================================
@pytest.mark.unit
def test_app_response_does_not_resolve_dns():
    """B-02: ``AppResponse`` erbte den ``git_link``-Validator von ``AppBase``.
    FastAPI validiert Antworten, also lief pro App ein blockierender
    ``gethostbyname`` im Request; löste ein Host später privat auf, scheiterte
    die ganze Liste mit 500."""
    import datetime as dt
    import uuid
    from unittest.mock import patch

    from app.schemas import AppResponse

    with patch("socket.gethostbyname", side_effect=AssertionError("DNS lookup")) as dns:
        for _ in range(3):
            AppResponse.model_validate({
                "appId": uuid.uuid4(),
                "userId": uuid.uuid4(),
                "name": "a",
                "git_link": "https://github.com/o/r",
                "created_at": dt.datetime.now(),
                "is_private": False,
            })

    dns.assert_not_called()


@pytest.mark.unit
def test_app_response_accepts_a_link_that_now_resolves_privately():
    import datetime as dt
    import uuid
    from unittest.mock import patch

    from app.schemas import AppResponse

    with patch("socket.gethostbyname", return_value="10.0.0.5"):
        out = AppResponse.model_validate({
            "appId": uuid.uuid4(),
            "userId": uuid.uuid4(),
            "name": "a",
            "git_link": "https://git.example.org/o/r",
            "created_at": dt.datetime.now(),
            "is_private": False,
        })

    assert out.git_link == "https://git.example.org/o/r"


@pytest.mark.unit
def test_app_create_still_rejects_private_hosts():
    """Gegenprobe: Die Prüfung gehört auf die Eingabe und bleibt dort."""
    from unittest.mock import patch

    from pydantic import ValidationError

    from app.schemas import AppCreate

    with patch("socket.gethostbyname", return_value="10.0.0.5"), pytest.raises(ValidationError):
        AppCreate(name="a", git_link="https://git.example.org/o/r")
