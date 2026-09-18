"""Unit tests for :mod:`app.services.git_service`.

DB-less Tests: GitPython wird vollständig gemockt; es passieren keine
Netzwerk- oder Dateisystem-Operationen jenseits eines tmp_path. Diese
Tests sichern das Verhalten von ``clone_release_vars`` in den vier
Phase-C4-Szenarien ab:

1. Shallow Fetch eines konkreten Tags
2. Verhalten bei nicht-HTTPS (Schema-)URLs
3. Tag nicht auffindbar im Remote
4. Authentifizierungs-Fehler werden propagiert

Wir patchen ``git.Repo.init`` direkt (statt der ``git_service``-Symbole),
weil der Service ``import git`` macht und ``git.Repo.init`` per
Attribut auflöst. ``autospec`` deaktivieren wir, da ``git.Repo``
dynamische Properties wie ``head.commit`` exponiert, die das echte
Spec-Modell nur umständlich nachbildet — die Tests kontrollieren die
Mock-Surface explizit über ``MagicMock``-Konfiguration.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.git_service import GitService

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _make_repo_mock() -> MagicMock:
    """Build a ``git.Repo``-shaped MagicMock with the surface the
    service touches: ``create_remote``, ``head.commit.hexsha`` and the
    ``git.checkout`` shortcut. Caller mutates ``origin.fetch`` to
    inject error scenarios."""
    repo = MagicMock(name="Repo")
    origin = MagicMock(name="Origin")
    repo.create_remote.return_value = origin
    repo.head.commit.hexsha = "deadbeefcafebabe"
    repo.git.checkout = MagicMock(name="checkout")
    return repo


def _service(tmp_path, monkeypatch) -> GitService:
    """Instantiate the service with a redirected temp base path and a
    deterministic access token. Avoids relying on the real
    ``settings`` singleton."""
    svc = GitService()
    monkeypatch.setattr(svc, "base_path", tmp_path)
    monkeypatch.setattr(svc, "token", "test-token-xyz")
    return svc


# ----------------------------------------------------------------
# Tests
# ----------------------------------------------------------------
@patch("app.services.git_service.git.Repo")
def test_clone_repository_at_tag_uses_shallow_fetch(
    mock_repo_cls, tmp_path, monkeypatch
):
    """``clone_release_vars`` muss einen Shallow-Fetch (``depth=1``)
    auf ``refs/tags/<tag>`` ausführen und anschließend genau diesen
    Tag auschecken — sonst ist der Sparse-Checkout sinnlos großvolumig."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo

    svc = _service(tmp_path, monkeypatch)
    result_path = svc.clone_release_vars(
        git_url="https://github.com/acme/widgets.git",
        tag="v1.2.3",
        deployment_id="dep-001",
    )

    # Result path liegt unter base_path
    assert result_path.startswith(str(tmp_path))

    # Repo wurde initialisiert (nicht clone-d), Sparse-Setup folgt
    mock_repo_cls.init.assert_called_once()

    # Remote-URL trägt den Token (HTTPS-Authentifizierung)
    repo.create_remote.assert_called_once()
    remote_args = repo.create_remote.call_args
    assert remote_args.args[0] == "origin"
    assert "test-token-xyz@github.com/acme/widgets.git" in remote_args.args[1]

    # Fetch ist shallow und gezielt auf den Tag
    origin = repo.create_remote.return_value
    origin.fetch.assert_called_once_with(
        refspec="refs/tags/v1.2.3:refs/tags/v1.2.3", depth=1
    )

    # Checkout des Tag-Refs mit force=True
    repo.git.checkout.assert_called_once_with("refs/tags/v1.2.3", force=True)


@patch("app.services.git_service.git.Repo")
def test_clone_repository_rejects_non_https_url(
    mock_repo_cls, tmp_path, monkeypatch
):
    """Eine nicht-HTTPS-URL (hier: ``ftp://``) lässt den Fetch
    fehlschlagen — der Service muss den Fehler in einen
    ``"Failed to clone"``-Exception kapseln und das angelegte
    Repo-Verzeichnis aufräumen."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo
    origin = repo.create_remote.return_value
    # GitPython hebt bei unbekanntem Schema einen GitCommandError —
    # wir simulieren das mit einem RuntimeError, da der Service
    # ohnehin breit Exception fängt und neu wirft.
    origin.fetch.side_effect = RuntimeError(
        "fatal: unsupported protocol: ftp"
    )

    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(Exception) as excinfo:
        svc.clone_release_vars(
            git_url="ftp://example.com/acme/widgets.git",
            tag="v1.0.0",
            deployment_id="dep-002",
        )

    assert "Failed to clone" in str(excinfo.value)
    # Cleanup: das Repo-Verzeichnis darf nach Fehler nicht zurückbleiben
    assert not (tmp_path / "deploy_dep-002").exists()


@patch("app.services.git_service.git.Repo")
def test_clone_repository_tag_not_found_raises(
    mock_repo_cls, tmp_path, monkeypatch
):
    """Existiert der angeforderte Tag im Remote nicht, propagiert
    GitPython einen Fetch-Fehler; der Service muss ihn als
    ``"Failed to clone"`` umverpacken und nicht stillschweigend
    weiterlaufen (sonst würde ein leeres Repo durchgereicht)."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo
    origin = repo.create_remote.return_value
    origin.fetch.side_effect = RuntimeError(
        "fatal: couldn't find remote ref refs/tags/v9.9.9"
    )

    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(Exception) as excinfo:
        svc.clone_release_vars(
            git_url="https://github.com/acme/widgets.git",
            tag="v9.9.9",
            deployment_id="dep-003",
        )

    msg = str(excinfo.value)
    assert "Failed to clone" in msg
    assert "v9.9.9" in msg or "couldn't find remote ref" in msg

    # Checkout darf nicht mehr aufgerufen worden sein — Fetch hat ja
    # bereits abgebrochen
    repo.git.checkout.assert_not_called()


@patch("app.services.git_service.git.Repo")
def test_clone_repository_authentication_failure_propagates(
    mock_repo_cls, tmp_path, monkeypatch
):
    """Bei abgelaufenem oder falschem Token wirft GitPython eine
    Authentifizierungs-Exception. Der Service muss diese in seine
    ``Failed to clone``-Hülle einpacken, dabei aber die Original-
    Ursache via ``__cause__`` durchreichen (``raise ... from e``),
    damit die Logs das Auth-Problem nachvollziehen können."""
    repo = _make_repo_mock()
    mock_repo_cls.init.return_value = repo
    origin = repo.create_remote.return_value
    auth_error = RuntimeError(
        "fatal: Authentication failed for 'https://github.com/acme/widgets.git/'"
    )
    origin.fetch.side_effect = auth_error

    svc = _service(tmp_path, monkeypatch)

    with pytest.raises(Exception) as excinfo:
        svc.clone_release_vars(
            git_url="https://github.com/acme/widgets.git",
            tag="v1.0.0",
            deployment_id="dep-004",
        )

    # Wrapped error mentions clone failure
    assert "Failed to clone" in str(excinfo.value)
    # Original cause ist via __cause__ erreichbar (raise ... from e)
    assert excinfo.value.__cause__ is auth_error
    assert "Authentication failed" in str(excinfo.value.__cause__)


# ----------------------------------------------------------------
# SSRF guard — _assert_safe_git_url / _get_authenticated_url
# ----------------------------------------------------------------
class TestSsrfGuard:
    """_get_authenticated_url must reject private-IP destinations before
    injecting the access token, regardless of how the URL is written."""

    def test_private_ipv4_loopback_blocked(self, tmp_path, monkeypatch):
        svc = _service(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="private address"):
            svc._get_authenticated_url("https://127.0.0.1/owner/repo.git")

    def test_private_rfc1918_10_blocked(self, tmp_path, monkeypatch):
        svc = _service(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="private address"):
            svc._get_authenticated_url("https://10.0.0.1/owner/repo.git")

    def test_private_rfc1918_192_168_blocked(self, tmp_path, monkeypatch):
        svc = _service(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="private address"):
            svc._get_authenticated_url("https://192.168.1.50/owner/repo.git")

    def test_link_local_blocked(self, tmp_path, monkeypatch):
        svc = _service(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="private address"):
            svc._get_authenticated_url("https://169.254.169.254/latest/meta-data/")

    def test_public_github_allowed(self, tmp_path, monkeypatch):
        svc = _service(tmp_path, monkeypatch)
        # Should not raise; token should be injected
        result = svc._get_authenticated_url("https://github.com/owner/repo.git")
        assert "test-token-xyz@github.com" in result

    def test_public_gitlab_allowed(self, tmp_path, monkeypatch):
        svc = _service(tmp_path, monkeypatch)
        result = svc._get_authenticated_url("https://gitlab.com/owner/repo.git")
        assert "test-token-xyz@gitlab.com" in result
