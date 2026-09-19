"""Unit tests for ``app.services.deployment_notifier``.

DB-less: every external dependency is mocked. We exercise the
top-level notify entry point plus the single-user resend helper and
the SMTP-failure path. The SMTP layer is stubbed at
``email_service.send_email`` so no template rendering or socket I/O
runs — the notifier only cares that ``send_email`` is invoked once
per recipient with the right ``to`` address.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import deployment_notifier
from app.services.deployment_notifier import ResendError


def _make_user(*, username: str, email: str, first: str = "", last: str = "") -> SimpleNamespace:
    """Build a minimal User-shaped object for the notifier's helpers.

    The notifier only reads ``userId``, ``username``, ``email``,
    ``firstName``, ``lastName`` — a SimpleNamespace is enough and
    avoids dragging in the ORM model + a real DB.
    """
    return SimpleNamespace(
        userId=uuid.uuid4(),
        username=username,
        email=email,
        firstName=first,
        lastName=last,
    )


def _terraform_outputs_for(team_name: str, user) -> dict:
    """Return a minimal ``{value: ...}`` outputs blob that
    ``_access_for_user`` can match against the given user.

    Account key is ``"<team>-<username>"`` — the same shape the
    Online-IDE template emits, so the fuzzy matcher in the notifier
    picks it up by username.
    """
    account_key = f"{team_name}-{user.username}"
    return {
        "team_vms": {
            "value": {
                team_name: {
                    "code_server_url": "http://1.2.3.4:8080",
                    "floating_ip": "1.2.3.4",
                    "fixed_ip": "10.0.0.5",
                    "instance_name": f"online-ide-{team_name}",
                }
            }
        },
        "user_accounts": {
            "value": {
                account_key: {
                    "auth": "s3cret",
                    "ip": "1.2.3.4",
                    "port": 8080,
                    "type": "password",
                    "username": user.username,
                }
            }
        },
    }


@pytest.mark.unit
def test_notify_deployment_succeeded_sends_emails_to_all_members():
    """``notify_deployment_succeeded`` walks each team's members, sends
    one per-user mail, then one owner-summary mail. With one team of
    two members + one owner we expect three send_email calls and the
    ``to`` addresses must cover every member plus the owner."""
    owner = _make_user(username="owner", email="owner@dhbw.de", first="Olivia", last="Owner")
    alice = _make_user(username="alice", email="alice@dhbw.de", first="Alice", last="A")
    bob = _make_user(username="bob", email="bob@dhbw.de", first="Bob", last="B")

    team = SimpleNamespace(teamId=uuid.uuid4(), name="Team-1")
    app_obj = SimpleNamespace(
        appId=uuid.uuid4(), name="my-app", git_link="https://example.com/r.git",
    )
    deployment = SimpleNamespace(
        deploymentId=uuid.uuid4(),
        name="d-1",
        releaseTag="v1.0.0",
        teams=[team],
        app=app_obj,
        user=owner,
    )

    # Outputs cover both team members so every per-user mail finds a
    # credential and we exercise the happy path for both.
    outputs = {
        "team_vms": _terraform_outputs_for("Team-1", alice)["team_vms"],
        "user_accounts": {
            "value": {
                f"Team-1-{alice.username}": {
                    "auth": "pw-alice", "ip": "1.2.3.4", "port": 8080,
                    "type": "password", "username": alice.username,
                },
                f"Team-1-{bob.username}": {
                    "auth": "pw-bob", "ip": "1.2.3.4", "port": 8080,
                    "type": "password", "username": bob.username,
                },
            }
        },
    }

    db = MagicMock()

    with (
        patch(
            "app.services.deployment_notifier.crud_deployments.get_deployment_with_details",
            return_value=deployment,
        ),
        patch(
            "app.services.deployment_notifier.crud_deployments.get_team_members",
            return_value=[alice, bob],
        ),
        # Pass-through refresh: returns the user it was handed.
        patch(
            "app.services.deployment_notifier.refresh_user_from_keycloak",
            side_effect=lambda _db, u: u,
        ),
        patch(
            "app.services.deployment_notifier.email_service.send_email",
            return_value=True,
        ) as m_send,
        # Skip Jinja entirely — templates aren't on this code path.
        patch(
            "app.services.deployment_notifier.email_service.render",
            return_value="<rendered/>",
        ),
    ):
        deployment_notifier.notify_deployment_succeeded(
            db, deployment.deploymentId, outputs,
        )

    # 2 user mails + 1 owner summary = 3 sends total
    assert m_send.call_count == 3
    sent_to = sorted(call.kwargs["to"] for call in m_send.call_args_list)
    assert sent_to == sorted([alice.email, bob.email, owner.email])


@pytest.mark.unit
def test_resend_user_access_to_single_recipient():
    """``resend_user_access`` must hit SMTP exactly once and address
    only the requested user — not the team, not the owner.
    """
    owner = _make_user(username="owner", email="owner@dhbw.de")
    alice = _make_user(username="alice", email="alice@dhbw.de")
    bob = _make_user(username="bob", email="bob@dhbw.de")

    team = SimpleNamespace(teamId=uuid.uuid4(), name="Team-1")
    app_obj = SimpleNamespace(
        appId=uuid.uuid4(), name="my-app", git_link="https://example.com/r.git",
    )
    deployment = SimpleNamespace(
        deploymentId=uuid.uuid4(),
        name="d-1",
        releaseTag="v1.0.0",
        teams=[team],
        app=app_obj,
        user=owner,
    )

    outputs = _terraform_outputs_for("Team-1", alice)

    # The notifier reads the latest successful DEPLOY task and decodes
    # ``outputs`` (str or dict). We give it a stringified JSON so the
    # ``json.loads`` branch is exercised.
    last_task = SimpleNamespace(outputs=json.dumps(outputs))

    db = MagicMock()
    # Build the query-chain mock for ``last_deploy``:
    # db.query(Task).filter(...).order_by(...).first() → last_task
    query_chain = MagicMock()
    query_chain.filter.return_value.order_by.return_value.first.return_value = last_task
    db.query.return_value = query_chain

    with (
        patch(
            "app.services.deployment_notifier.crud_deployments.get_deployment_with_details",
            return_value=deployment,
        ),
        patch(
            "app.services.deployment_notifier.crud_deployments.get_team_members",
            return_value=[alice, bob],
        ),
        patch(
            "app.services.deployment_notifier.refresh_user_from_keycloak",
            side_effect=lambda _db, u: u,
        ),
        patch(
            "app.services.deployment_notifier.email_service.send_email",
            return_value=True,
        ) as m_send,
        patch(
            "app.services.deployment_notifier.email_service.render",
            return_value="<rendered/>",
        ),
    ):
        ok = deployment_notifier.resend_user_access(
            db, deployment.deploymentId, team.teamId, alice.userId,
        )

    assert ok is True
    assert m_send.call_count == 1
    # Only Alice — never Bob, never the owner.
    assert m_send.call_args.kwargs["to"] == alice.email


@pytest.mark.unit
def test_resend_user_access_raises_when_outputs_missing():
    """When the latest DEPLOY task is absent (or carries no outputs),
    ``resend_user_access`` raises ``ResendError('no_successful_deploy')``
    rather than attempting to send a half-baked mail.
    """
    owner = _make_user(username="owner", email="owner@dhbw.de")
    alice = _make_user(username="alice", email="alice@dhbw.de")

    team = SimpleNamespace(teamId=uuid.uuid4(), name="Team-1")
    app_obj = SimpleNamespace(
        appId=uuid.uuid4(), name="my-app", git_link="https://example.com/r.git",
    )
    deployment = SimpleNamespace(
        deploymentId=uuid.uuid4(),
        name="d-1",
        releaseTag="v1.0.0",
        teams=[team],
        app=app_obj,
        user=owner,
    )

    db = MagicMock()
    # No DEPLOY task at all — ``.first()`` returns None.
    query_chain = MagicMock()
    query_chain.filter.return_value.order_by.return_value.first.return_value = None
    db.query.return_value = query_chain

    with (
        patch(
            "app.services.deployment_notifier.crud_deployments.get_deployment_with_details",
            return_value=deployment,
        ),
        patch(
            "app.services.deployment_notifier.crud_deployments.get_team_members",
            return_value=[alice],
        ),
        patch(
            "app.services.deployment_notifier.refresh_user_from_keycloak",
            side_effect=lambda _db, u: u,
        ),
        patch(
            "app.services.deployment_notifier.email_service.send_email",
            return_value=True,
        ) as m_send,pytest.raises(ResendError) as exc_info
    ):
        deployment_notifier.resend_user_access(
            db, deployment.deploymentId, team.teamId, alice.userId,
        )

    assert str(exc_info.value) == "no_successful_deploy"
    # No mail must have gone out on the failure path.
    m_send.assert_not_called()


@pytest.mark.unit
def test_notify_handles_smtp_failure_gracefully():
    """SMTP raising in ``send_email`` must NOT abort the notification
    loop. The notifier catches the exception per-mail, logs it, and
    moves on — every recipient is still attempted, and the entry
    point returns without re-raising.
    """
    owner = _make_user(username="owner", email="owner@dhbw.de")
    alice = _make_user(username="alice", email="alice@dhbw.de")
    bob = _make_user(username="bob", email="bob@dhbw.de")

    team = SimpleNamespace(teamId=uuid.uuid4(), name="Team-1")
    app_obj = SimpleNamespace(
        appId=uuid.uuid4(), name="my-app", git_link="https://example.com/r.git",
    )
    deployment = SimpleNamespace(
        deploymentId=uuid.uuid4(),
        name="d-1",
        releaseTag="v1.0.0",
        teams=[team],
        app=app_obj,
        user=owner,
    )

    outputs = {
        "team_vms": {
            "value": {
                "Team-1": {
                    "code_server_url": "http://1.2.3.4:8080",
                    "floating_ip": "1.2.3.4",
                    "instance_name": "vm-1",
                }
            }
        },
        "user_accounts": {
            "value": {
                f"Team-1-{alice.username}": {
                    "auth": "pw-a", "ip": "1.2.3.4", "port": 8080,
                    "type": "password", "username": alice.username,
                },
                f"Team-1-{bob.username}": {
                    "auth": "pw-b", "ip": "1.2.3.4", "port": 8080,
                    "type": "password", "username": bob.username,
                },
            }
        },
    }

    db = MagicMock()

    with (
        patch(
            "app.services.deployment_notifier.crud_deployments.get_deployment_with_details",
            return_value=deployment,
        ),
        patch(
            "app.services.deployment_notifier.crud_deployments.get_team_members",
            return_value=[alice, bob],
        ),
        patch(
            "app.services.deployment_notifier.refresh_user_from_keycloak",
            side_effect=lambda _db, u: u,
        ),
        patch(
            "app.services.deployment_notifier.email_service.send_email",
            side_effect=Exception("SMTP unavailable"),
        ) as m_send,
        patch(
            "app.services.deployment_notifier.email_service.render",
            return_value="<rendered/>",
        ),
    ):
        # Must NOT raise — the notifier is best-effort by contract.
        deployment_notifier.notify_deployment_succeeded(
            db, deployment.deploymentId, outputs,
        )

    # All three mails were attempted even though every one of them
    # raised: 2 per-user mails + 1 owner summary.
    assert m_send.call_count == 3


# ---------------------------------------------------------------------------
# Connection protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw, expected",
    [
        # An explicit ``protocol`` always wins — including over a port
        # that would suggest something else.
        ({"protocol": "rdp", "ip": "1.2.3.4", "port": 8080}, "rdp"),
        ({"protocol": "RDP ", "ip": "1.2.3.4", "port": 22}, "rdp"),
        ({"protocol": "none", "ip": "1.2.3.4", "port": 8080}, "none"),
        # Unknown values fall through to inference rather than being
        # rendered as a protocol nobody can use.
        ({"protocol": "telnet", "ip": "1.2.3.4", "port": 3389}, "rdp"),
        # Legacy ``authtype`` from templates written before ``protocol``.
        ({"authtype": "ssh", "ip": "1.2.3.4", "port": 8080}, "ssh"),
        ({"authtype": "url", "ip": "1.2.3.4", "port": 8080}, "web"),
        # An ssh_key credential can only mean SSH.
        ({"type": "ssh_key", "ip": "1.2.3.4", "port": 8080}, "ssh"),
        # Well-known ports.
        ({"ip": "1.2.3.4", "port": 3389}, "rdp"),
        ({"ip": "1.2.3.4", "port": 5900}, "vnc"),
        ({"ip": "1.2.3.4", "port": 22}, "ssh"),
        # Anything else with an ip:port is the web UI the deployment
        # page already assumed it was.
        ({"ip": "1.2.3.4", "port": 8080}, "web"),
        # No port: SSH, the historical default.
        ({"ip": "1.2.3.4", "username": "luca"}, "ssh"),
    ],
)
def test_resolve_protocol(raw, expected):
    assert deployment_notifier._resolve_protocol(raw) == expected


@pytest.mark.unit
def test_resolve_protocol_uses_team_url_only_as_last_resort():
    """A team VM URL means "web" for an account with nothing else to go
    on, but never overrides the account's own signals."""
    assert deployment_notifier._resolve_protocol({}, has_team_url=True) == "web"
    assert deployment_notifier._resolve_protocol({}, has_team_url=False) == "ssh"
    assert (
        deployment_notifier._resolve_protocol({"port": 3389}, has_team_url=True) == "rdp"
    )


@pytest.mark.unit
def test_connect_string_per_protocol():
    ssh = {"ip": "1.2.3.4", "port": 2222, "username": "luca"}
    assert deployment_notifier._connect_string("ssh", ssh, None) == "ssh -p 2222 luca@1.2.3.4"
    assert (
        deployment_notifier._connect_string("ssh", {**ssh, "port": 22}, None)
        == "ssh luca@1.2.3.4"
    )
    # RDP/VNC hand out the host:port a client dials, never a command.
    assert deployment_notifier._connect_string("rdp", {"ip": "1.2.3.4", "port": 3389}, None) == "1.2.3.4:3389"
    assert deployment_notifier._connect_string("rdp", {"ip": "1.2.3.4"}, None) == "1.2.3.4:3389"
    assert deployment_notifier._connect_string("vnc", {"ip": "1.2.3.4"}, None) == "1.2.3.4:5900"
    # Web prefers the team URL and falls back to the per-user ip:port.
    assert (
        deployment_notifier._connect_string("web", {"ip": "1.2.3.4", "port": 80}, "http://x/ide")
        == "http://x/ide"
    )
    assert (
        deployment_notifier._connect_string("web", {"ip": "1.2.3.4", "port": 8080}, None)
        == "http://1.2.3.4:8080"
    )


@pytest.mark.unit
def test_connect_string_is_none_when_unusable():
    """No half-built lines: an incomplete account or an app that ships
    no endpoint yields ``None`` so the mail leaves the row out."""
    assert deployment_notifier._connect_string("ssh", {"ip": "1.2.3.4"}, None) is None
    assert deployment_notifier._connect_string("ssh", {"username": "luca"}, None) is None
    assert deployment_notifier._connect_string("rdp", {"port": 3389}, None) is None
    assert deployment_notifier._connect_string("none", {"ip": "1.2.3.4"}, None) is None
    assert deployment_notifier._connect_string("web", {}, None) is None


@pytest.mark.unit
def test_access_for_user_carries_protocol_and_connect():
    """A Windows app ships ``type = "password"`` like a Linux one; only
    ``protocol`` tells the platform not to print an ssh command."""
    user = _make_user(username="luca", email="luca@dhbw.de")
    outputs = {
        "user_accounts": {
            "value": {
                "Team-1-luca": {
                    "auth": "s3cret",
                    "ip": "10.200.5.60",
                    "port": 3389,
                    "type": "password",
                    "protocol": "rdp",
                    "username": "luca",
                }
            }
        }
    }

    access = deployment_notifier._access_for_user(outputs, "Team-1", user)

    assert access is not None
    assert access["protocol"] == "rdp"
    assert access["connect"] == "10.200.5.60:3389"
    # The credential slot is untouched — the two are independent.
    assert access["auth_type"] == "password"
    assert access["password"] == "s3cret"
