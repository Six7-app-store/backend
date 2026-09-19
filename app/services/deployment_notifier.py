"""Build and send post-deploy notification mails.

Two flavours:

* ``send_user_mails(...)`` — one mail per user with their own access
  details and the list of teammates.
* ``send_owner_mail(...)`` — one mail to the deployment owner with
  every team's VM data and every user's credentials in one place.

Both consume the worker's terraform outputs (``team_vms``,
``user_accounts``, ``teams_summary``) plus the deployment's team/user
membership from the DB. The output shape is documented inline below
so a future template change doesn't have to re-discover it from a
real run.

Recipient freshness: every recipient is pulled from Keycloak
(``refresh_user_from_keycloak``) right before the mail is composed.
A deploy can run for many minutes between the wizard pick and the
notification, and the team member's address may have changed in the
meantime — refusing to refresh would silently send credentials to a
stale address. The refresh is best-effort: when Keycloak is down or
the user was deleted upstream we fall back to the DB record so a
flaky identity provider can't tank the entire notification flow.

Failures are logged at the call site and never bubble up — sending
mail is best-effort, the deploy itself is already done.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.config import settings
from app.crud import deployments as crud_deployments
from app.models import App, Deployment, Team, User
from app.services import email_service
from app.utils.keycloak_auth import refresh_user_from_keycloak

logger = logging.getLogger(__name__)


def _display_name(user: User) -> str:
    """Render a user-friendly greeting name.

    Order of preference: firstName + lastName → firstName → username.
    Keeps username as the technical identifier; the templates can
    still reference ``user.username`` directly when they need the
    raw login. Never returns an empty string.
    """
    parts = [p for p in (getattr(user, "firstName", None), getattr(user, "lastName", None)) if p]
    if parts:
        return " ".join(parts)
    return user.username or user.email.split("@")[0]


# ----------------------------------------------------------------------------
# Outputs parsing
# ----------------------------------------------------------------------------
#
# Worker tasks return ``terraform_outputs`` as the raw JSON object
# Terraform's ``output -json`` produces, i.e. each top-level key is an
# output name with ``{value, type, sensitive}`` underneath. We only care
# about the ``value`` of three well-known outputs from the Online-IDE
# template (and any template that follows the same conventions):
#
#   team_vms.value:
#     {
#       "Team-1": {
#         "code_server_url": "http://1.2.3.4:8080",
#         "floating_ip":     "1.2.3.4",
#         "fixed_ip":        "10.100.x.y",
#         "instance_id":     "uuid",
#         "instance_name":   "online-ide-Team-1"
#       },
#       ...
#     }
#
#   user_accounts.value:
#     {
#       "Team-1-luca": {
#         "auth":     "<password-or-key-or-login-url>",
#         "ip":       "1.2.3.4",
#         "port":     8080,
#         "type":     "password" | "ssh_key" | "oauth" | "none",
#         "protocol": "ssh" | "rdp" | "vnc" | "web" | "none",
#         "username": "luca"
#       },
#       ...
#     }
#
#   ``type`` and ``protocol`` answer two different questions and an app
#   sets them independently:
#     * ``type``     — WHAT the ``auth`` value is (a password, a public
#                      key, a login URL, nothing).
#     * ``protocol`` — HOW the user reaches the machine. A Windows VM
#                      ships ``type = "password"`` like a Linux one but
#                      ``protocol = "rdp"``, so the platform must not
#                      hand out an ``ssh`` command for it.
#   ``protocol`` is optional; :func:`_resolve_protocol` infers it for
#   apps that predate the field.
#
#   Auth-type contract:
#     * ``password`` (default if omitted) — ``auth`` is the password
#       string. The mail prints a "Password" line.
#     * ``ssh_key``  — ``auth`` is the public key the user should use
#       (or a one-line hint about which key was pre-provisioned). The
#       mail prints an "SSH key" line in a monospace block.
#     * ``oauth``    — ``auth`` is the login URL the user should click
#       (e.g. an external IdP). The mail prints "Login via …" with
#       the URL.
#     * ``none``     — no credential is shipped (open dashboard, no
#       auth wall). ``auth`` may be omitted; the mail leaves out the
#       credentials block entirely and only ships URL/IP.
#   An unknown ``type`` falls back to ``password`` rendering for
#   safety (the mail still shows whatever ``auth`` value the app
#   produced rather than silently dropping it).
#
#   teams_summary.value: {"Team-1": 1, ...}  — member counts; not used
#                                              directly but useful as a
#                                              sanity check.
#
# When a template doesn't expose one of these (e.g. an app without
# per-user credentials), the helpers return empty dicts and the mail
# silently omits those sections.


def _output_value(outputs: dict[str, Any] | None, key: str) -> Any:
    """Pluck ``outputs[key].value`` out, tolerating missing keys."""
    if not outputs:
        return None
    bag = outputs.get(key)
    if isinstance(bag, dict):
        return bag.get("value")
    return None


def _team_vms(outputs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    val = _output_value(outputs, "team_vms")
    return val if isinstance(val, dict) else {}


def _user_accounts(outputs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    val = _output_value(outputs, "user_accounts")
    return val if isinstance(val, dict) else {}


def _vm_for_team(outputs: dict[str, Any] | None, team_name: str) -> dict[str, Any] | None:
    """Pick the VM block for one team, normalised to the keys the
    template expects (``url``, ``floating_ip``, ``fixed_ip``,
    ``instance_name``). Templates that emit different keys (e.g.
    ``url`` instead of ``code_server_url``) are tolerated because we
    only normalise what we recognise.
    """
    raw = _team_vms(outputs).get(team_name)
    if not isinstance(raw, dict):
        return None
    return {
        "url": raw.get("code_server_url") or raw.get("url"),
        "floating_ip": raw.get("floating_ip"),
        "fixed_ip": raw.get("fixed_ip"),
        "instance_name": raw.get("instance_name"),
    }


# ----------------------------------------------------------------------------
# Connection protocol
# ----------------------------------------------------------------------------

#: Protocols an app may declare in ``user_accounts.<key>.protocol``.
PROTOCOLS = ("ssh", "rdp", "vnc", "web", "none")

#: Ports that identify a protocol on their own. Only consulted when the
#: app declared no ``protocol`` — an explicit value always wins, so an
#: app is free to serve RDP on a non-standard port.
_PORT_PROTOCOLS = {22: "ssh", 3389: "rdp", 5900: "vnc"}

#: Legacy slot some templates wrote before ``protocol`` existed.
_AUTHTYPE_PROTOCOLS = {"ssh": "ssh", "url": "web", "http": "web", "web": "web"}


def _resolve_protocol(raw: dict[str, Any], *, has_team_url: bool = False) -> str:
    """Decide how the user connects to their machine.

    An explicit ``protocol`` wins. Everything below it only exists so
    apps deployed before the field existed keep rendering exactly as
    they did: the legacy ``authtype`` slot, then an ``ssh_key``
    credential (which can only mean SSH), then the well-known port,
    then "it has an ip:port, so it is probably a web UI" — which is
    what the deployment page already assumed. The final fallback stays
    ``ssh`` for the same reason.
    """
    declared = str(raw.get("protocol") or "").strip().lower()
    if declared in PROTOCOLS:
        return declared

    legacy = str(raw.get("authtype") or "").strip().lower()
    if legacy in _AUTHTYPE_PROTOCOLS:
        return _AUTHTYPE_PROTOCOLS[legacy]

    if raw.get("type") == "ssh_key":
        return "ssh"

    port = raw.get("port")
    if isinstance(port, int) and port in _PORT_PROTOCOLS:
        return _PORT_PROTOCOLS[port]

    if raw.get("ip") and port:
        return "web"
    if has_team_url:
        return "web"
    return "ssh"


def _connect_string(protocol: str, raw: dict[str, Any], url: str | None) -> str | None:
    """Build the one line a user can copy to reach their machine.

    ``ssh`` yields a ready-to-run command (``-p`` omitted on port 22),
    ``rdp``/``vnc`` the ``host:port`` an RDP/VNC client expects, ``web``
    the URL. ``none`` and incomplete accounts yield ``None`` so the mail
    and the UI can leave the line out instead of printing a half one.
    """
    ip = raw.get("ip")
    port = raw.get("port")
    username = raw.get("username")

    if protocol == "ssh":
        if not ip or not username:
            return None
        port_flag = f"-p {port} " if port and port != 22 else ""
        return f"ssh {port_flag}{username}@{ip}"
    if protocol in ("rdp", "vnc"):
        if not ip:
            return None
        default_port = 3389 if protocol == "rdp" else 5900
        return f"{ip}:{port or default_port}"
    if protocol == "web":
        return url or (f"http://{ip}:{port}" if ip and port else None)
    return None


def _normalise_account_key(value: str | None) -> str:
    """Normalise an account/username key for fuzzy matching.

    Worker templates derive Linux-friendly account names from emails
    by replacing every non-``[a-z0-9]`` character with a single ``-``.
    A user with email ``luca.baeck@gmail.com`` becomes ``luca-baeck``
    in the terraform output, while our DB has ``luca`` as the
    username and ``luca.baeck`` as the email's local-part. Comparing
    those literally misses every match where the template applied any
    transformation. We collapse ``.``/``-``/``_``/spaces to a single
    ``-`` and lowercase, so all four forms map to the same canonical
    string and the matcher succeeds without us having to know which
    transformation a given template applied.
    """
    if not value:
        return ""
    out = []
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in ".-_ ":
            out.append("-")
        # Other characters drop entirely.
    # Collapse runs of "-" into one so "a..b" and "a-b" both end up
    # as "a-b".
    canonical = "".join(out)
    while "--" in canonical:
        canonical = canonical.replace("--", "-")
    return canonical.strip("-")


def _find_account_for_user(
    outputs: dict[str, Any] | None,
    team_name: str,
    user: User,
) -> tuple[str, dict[str, Any]] | None:
    """Locate the user's raw account entry in the worker's outputs.

    Templates key per-user accounts as ``"<team>-<account-name>"``
    where ``<account-name>`` is some derivation of the user's email
    or username — the Online-IDE template, for example, takes the
    email's local-part and substitutes non-alphanumerics with ``-``.

    To find the right entry without coupling to one specific naming
    scheme we build a set of candidate identifiers from everything
    we know about the user (username, email local-part, full name)
    plus a normalised form of each, then walk every account in the
    output and check for any overlap. ``_normalise_account_key``
    makes ``luca.baeck``, ``luca-baeck`` and ``LUCA_BAECK`` all
    compare equal.

    Returns ``(key, raw_entry)`` on a match — the ORIGINAL output key
    alongside the untouched account dict — or ``None``. Kept separate
    from :func:`_access_for_user` so callers that need the raw entry
    (e.g. the per-member ``/my-access`` endpoint, whose response mirrors
    the terraform ``user_accounts`` shape) reuse the exact same matching
    logic as the mail path without re-deriving the normalised form.
    """
    accounts = _user_accounts(outputs)
    candidates_raw: set[str] = {
        user.username or "",
        (user.email or "").split("@")[0],
    }
    if user.firstName and user.lastName:
        candidates_raw.add(f"{user.firstName} {user.lastName}")
    candidates = {_normalise_account_key(c) for c in candidates_raw if c}
    candidates.discard("")

    for key, raw in accounts.items():
        if not isinstance(raw, dict):
            continue
        # Strip the team-name prefix when present so a key like
        # ``"Team-1-luca-baeck"`` becomes ``"luca-baeck"`` before
        # normalisation. The prefix itself is normalised the same
        # way so a team named ``"Team 1"`` (space) also matches.
        team_prefix = _normalise_account_key(team_name) + "-"
        normalised_key = _normalise_account_key(key)
        suffix = normalised_key[len(team_prefix):] if normalised_key.startswith(team_prefix) else normalised_key

        candidates_with_inner_username = candidates | {_normalise_account_key(raw.get("username"))}
        candidates_with_inner_username.discard("")

        if suffix in candidates_with_inner_username:
            return key, raw
    return None


def _access_for_user(
    outputs: dict[str, Any] | None,
    team_name: str,
    user: User,
) -> dict[str, Any] | None:
    """Build the normalised access dict the mail templates consume.

    Thin wrapper over :func:`_find_account_for_user`: locates the user's
    raw account entry, then maps it onto the ``username`` /
    ``auth_type`` / ``auth_value`` shape the Jinja mail templates expect.
    Returns ``None`` when no entry matches the user.
    """
    match = _find_account_for_user(outputs, team_name, user)
    if match is None:
        return None
    key, raw = match
    team_prefix = _normalise_account_key(team_name) + "-"
    normalised_key = _normalise_account_key(key)
    suffix = normalised_key[len(team_prefix):] if normalised_key.startswith(team_prefix) else normalised_key

    # Normalise the ``type`` slot once. Unknown values fall back to
    # ``password`` rendering so unforeseen app outputs still produce a
    # useful mail rather than dropping silently.
    raw_type = raw.get("type")
    auth_type = raw_type if raw_type in ("password", "ssh_key", "oauth", "none") else "password"
    auth_value = raw.get("auth")
    # ``password`` field is kept for backwards-compatibility with any
    # template path that still reads it directly; it is only populated
    # for the password type so a missing value renders as None (templates
    # check ``auth_type`` before pulling ``password``).
    password = auth_value if auth_type == "password" else None
    # ``protocol`` is the app's statement about HOW to connect; the
    # mail renders ``connect`` verbatim so it never builds an ``ssh``
    # command for an RDP machine.
    team_url = (_vm_for_team(outputs, team_name) or {}).get("url")
    protocol = _resolve_protocol(raw, has_team_url=bool(team_url))
    return {
        "username": raw.get("username") or suffix,
        "password": password,
        "ip": raw.get("ip"),
        "port": raw.get("port"),
        "auth_type": auth_type,
        # ``auth_value`` carries the raw credential regardless of type —
        # templates use it together with ``auth_type`` to decide WHERE to
        # show it (password field, SSH-key block, OAuth login link, ...).
        "auth_value": auth_value,
        "protocol": protocol,
        # Ready-to-use connect line for ``protocol`` — an ssh command, an
        # ``ip:port`` for RDP/VNC, a URL for web apps, ``None`` when the
        # app ships no reachable endpoint.
        "connect": _connect_string(protocol, raw, team_url),
        # Convenience fallback so the user-mail can show a URL even when
        # the per-user output doesn't carry one — use the team VM's URL.
        "url": team_url,
    }



# ----------------------------------------------------------------------------
# Senders
# ----------------------------------------------------------------------------


def _team_members(db: Session, team: Team) -> list[User]:
    """Resolve team members via the join table. ``Team.user_to_teams``
    is the relationship; we go through ``crud_deployments`` so the
    join logic stays centralised (and works regardless of session
    state)."""
    return crud_deployments.get_team_members(db, team.teamId)


def _send_user_mail(
    *,
    db: Session,
    user: User,
    teammates: list[User],
    team_name: str,
    deployment: Deployment,
    app: App,
    access: dict[str, Any],
) -> None:
    # Re-pull from Keycloak immediately before composing the mail.
    # The DB row may have been written minutes (or longer) ago when
    # the wizard picker first stored the membership; meanwhile the
    # user might have changed their address upstream. Refreshing here
    # keeps the recipient honest. ``refresh_user_from_keycloak`` is
    # best-effort: if KC is unreachable the helper logs and returns
    # the DB row, so the mail still goes out to the last-known-good
    # address rather than failing entirely.
    #
    # The notify caller already pre-refreshed every team member to
    # keep the ``teammates`` list consistent. We refresh the recipient
    # once more here because this helper is also called from the
    # ``resend_user_access`` path, which doesn't run that pre-loop —
    # making the refresh a property of the sender keeps both call
    # sites honest. The extra KC hit on the notify path is one
    # roundtrip per user mail, which is negligible compared to the
    # SMTP work that follows.
    user = refresh_user_from_keycloak(db, user)
    ctx = {
        "user": user,
        "user_display_name": _display_name(user),
        "teammates": [
            {"user": m, "display_name": _display_name(m)}
            for m in teammates
            if m.userId != user.userId
        ],
        "team_name": team_name,
        "deployment": {
            "name": deployment.name,
            "git_url": app.git_link,
            "release_tag": deployment.releaseTag,
            "app_name": app.name,
        },
        "access": access,
    }
    email_service.send_email(
        to=user.email,
        subject=f"[{deployment.name}] Your access details",
        html_body=email_service.render("user_invite.html", **ctx),
        text_body=email_service.render("user_invite.txt", **ctx),
    )


def _send_owner_mail(
    *,
    db: Session,
    owner: User,
    deployment: Deployment,
    app: App,
    teams_payload: list[dict[str, Any]],
) -> None:
    # Same refresh contract as ``_send_user_mail`` — the owner of a
    # deployment is also a Keycloak user whose address may have
    # changed since the deployment was created.
    owner = refresh_user_from_keycloak(db, owner)
    ctx = {
        "owner": owner,
        "owner_display_name": _display_name(owner),
        "deployment": {
            "name": deployment.name,
            "git_url": app.git_link,
            "release_tag": deployment.releaseTag,
            "app_name": app.name,
        },
        "teams": teams_payload,
        "detail_url": f"{settings.APP_BASE_URL.rstrip('/')}/deployments/{deployment.deploymentId}",
    }
    email_service.send_email(
        to=owner.email,
        subject=f"[{deployment.name}] Deployment summary",
        html_body=email_service.render("owner_summary.html", **ctx),
        text_body=email_service.render("owner_summary.txt", **ctx),
    )


def notify_deployment_succeeded(
    db: Session,
    deployment_id: UUID,
    terraform_outputs: dict[str, Any] | None,
) -> None:
    """Top-level entry point — call from the celery event listener
    after a successful DEPLOY task.

    Loads the deployment with all relations, walks its teams, and
    sends one mail per user plus one summary mail to the owner.
    Silent no-op when the deployment is missing or the outputs are
    empty (e.g. a deploy that produced no resources to credential).
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        logger.warning("notify: deployment %s not found", deployment_id)
        return

    app = deployment.app
    owner = deployment.user
    if not app or not owner:
        logger.warning(
            "notify: deployment %s missing app or owner relation", deployment_id
        )
        return

    if not terraform_outputs:
        logger.info(
            "notify: deployment %s has no terraform outputs, skipping mails",
            deployment_id,
        )
        return

    # Build the per-team payload once. Used for the owner mail and for
    # picking each user's individual access.
    teams_payload: list[dict[str, Any]] = []
    for team in deployment.teams or []:
        members = _team_members(db, team)
        # Refresh every team member from Keycloak up front so the
        # teammates section in each per-user mail and the owner
        # summary's member list both reflect the current upstream
        # records, not whatever was on file when the deployment was
        # created. ``_send_user_mail`` does its own refresh of the
        # specific recipient too, but doing it once here keeps the
        # ``teammates`` rendering consistent and avoids N redundant
        # KC roundtrips per user mail.
        members = [refresh_user_from_keycloak(db, m) for m in members]
        member_payload: list[dict[str, Any]] = []
        for member in members:
            access = _access_for_user(terraform_outputs, team.name, member)
            if access is None:
                # No credential output for this user — still include
                # them in the owner summary so the owner sees who's on
                # the team, but skip the per-user mail (no useful
                # content to send).
                member_payload.append({
                    "user": member,
                    "display_name": _display_name(member),
                    "access": {
                        "username": "—",
                        "password": "—",
                        "auth_type": "password",
                        "auth_value": None,
                    },
                })
                continue
            member_payload.append({
                "user": member,
                "display_name": _display_name(member),
                "access": access,
            })

            # Per-user mail — fire-and-forget; failures already logged
            # inside ``email_service.send_email``.
            try:
                _send_user_mail(
                    db=db,
                    user=member,
                    teammates=members,
                    team_name=team.name,
                    deployment=deployment,
                    app=app,
                    access=access,
                )
            except Exception as e:
                logger.warning(
                    "notify: user mail to %s for deployment %s failed: %s",
                    member.email, deployment_id, e,
                )

        teams_payload.append({
            "name": team.name,
            "vm": _vm_for_team(terraform_outputs, team.name),
            "members": member_payload,
        })

    # Owner summary last so it includes everything we managed to
    # resolve.
    try:
        _send_owner_mail(
            db=db,
            owner=owner,
            deployment=deployment,
            app=app,
            teams_payload=teams_payload,
        )
    except Exception as e:
        logger.warning(
            "notify: owner mail for deployment %s failed: %s",
            deployment_id, e,
        )


# ----------------------------------------------------------------------------
# Single-user resend
# ----------------------------------------------------------------------------


class ResendError(Exception):
    """Resend prerequisites weren't met (no successful deploy, user not in team, no credentials)."""


def resend_user_access(
    db: Session,
    deployment_id: UUID,
    team_id: UUID,
    user_id: UUID,
) -> bool:
    """Re-send the access mail for one specific user of a deployment.

    Loads the deployment's latest successful DEPLOY task to recover
    the original ``terraform_outputs`` (those carry the user-specific
    credentials), then sends the same per-user mail
    ``notify_deployment_succeeded`` would have sent — to that user
    only. Used by the "Resend access" button in the Teams card on the
    deployment detail page.

    Raises ``ResendError`` with a structured reason when:

    * the deployment doesn't exist or has no successful deploy task
      (nothing to resend yet)
    * the team or the user isn't part of this deployment (caller
      shouldn't have asked, but defend in depth)
    * the deploy didn't produce a credential for this user (template
      doesn't issue per-user accounts, or the matcher missed)

    Returns ``True`` on a successful SMTP handover, ``False`` if the
    mail was sent but SMTP rejected it (logged inside ``send_email``).
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise ResendError("deployment_not_found")
    app = deployment.app
    if not app:
        raise ResendError("deployment_app_missing")

    # Find the team scoped to this deployment.
    team: Team | None = next(
        (t for t in (deployment.teams or []) if t.teamId == team_id),
        None,
    )
    if team is None:
        raise ResendError("team_not_in_deployment")

    members = _team_members(db, team)
    user: User | None = next((m for m in members if m.userId == user_id), None)
    if user is None:
        raise ResendError("user_not_in_team")

    # Pull the most recent successful DEPLOY task's outputs — the same
    # JSON the original notify ran against. The crud helper filters to
    # DEPLOY + SUCCESS and returns None for missing/unparseable outputs;
    # both collapse to "nothing to resend yet" (409 at the router).
    outputs = crud_deployments.get_latest_successful_deploy_outputs(
        db, deployment_id
    )
    if outputs is None:
        raise ResendError("no_successful_deploy")

    access = _access_for_user(outputs, team.name, user)
    if access is None:
        raise ResendError("no_credentials_for_user")

    try:
        _send_user_mail(
            db=db,
            user=user,
            teammates=members,
            team_name=team.name,
            deployment=deployment,
            app=app,
            access=access,
        )
        return True
    except Exception as e:
        logger.warning(
            "resend: user mail to %s for deployment %s failed: %s",
            user.email, deployment_id, e,
        )
        return False


# ----------------------------------------------------------------------------
# Per-member self-access lookup (read-only)
# ----------------------------------------------------------------------------


def get_user_access(
    db: Session,
    deployment_id: UUID,
    user_id: UUID,
) -> dict[str, Any] | None:
    """Return the terraform output slices a single member is allowed to see.

    Powers the ``GET /deployments/{id}/my-access`` endpoint: a team member
    (typically a student) may retrieve THEIR OWN access credentials without
    the owner-view that gates the full ``outputs`` payload. Only the caller's
    own account entry is ever returned — teammates' credentials are never
    included.

    The result mirrors the raw terraform ``user_accounts`` / ``team_vms``
    output shape (one key each: the member's account and their team's VM)
    so the frontend's existing account-matching pipeline consumes it
    unchanged:

        {"user_accounts": {"<key>": {...}}, "team_vms": {"<team>": {...}}}

    Returns ``None`` when the deployment is missing, the user isn't part of
    any of its teams, there's no successful deploy yet, or the deploy issued
    no per-user credential for this user. The router maps ``None`` onto an
    empty-map 200 response so the UI can render a clean "no credentials yet"
    state instead of an error.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        return None

    # Find which team of this deployment the user belongs to. We need the
    # Team (for its name, the account-key prefix) and the User object (its
    # identifiers drive the fuzzy account match). Mirrors the team/user
    # resolution in ``resend_user_access``.
    matched_team: Team | None = None
    matched_user: User | None = None
    for team in deployment.teams or []:
        members = crud_deployments.get_team_members(db, team.teamId)
        member = next((m for m in members if m.userId == user_id), None)
        if member is not None:
            matched_team = team
            matched_user = member
            break
    if matched_team is None or matched_user is None:
        return None

    outputs = crud_deployments.get_latest_successful_deploy_outputs(
        db, deployment_id
    )
    if outputs is None:
        return None

    match = _find_account_for_user(outputs, matched_team.name, matched_user)
    if match is None:
        return None

    key, raw = match
    # Return the RAW account entry keyed by its ORIGINAL output key so the
    # frontend's ``enrichedTeams`` matching (which derives the same key
    # from team + email) resolves it exactly as it does for the owner-view
    # ``user_accounts`` map. Include only the caller's own team VM block.
    result: dict[str, Any] = {"user_accounts": {key: raw}, "team_vms": {}}
    team_vm = _team_vms(outputs).get(matched_team.name)
    if isinstance(team_vm, dict):
        result["team_vms"][matched_team.name] = team_vm
    return result

