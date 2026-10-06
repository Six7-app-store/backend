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
