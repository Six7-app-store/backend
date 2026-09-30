"""Texts for the generated API documentation (``/docs``, ``/redoc``).

Kept out of ``main.py`` so the app wiring stays readable. Every tag a
router uses must be declared in ``OPENAPI_TAGS`` -- a unit test checks it.
"""

API_DESCRIPTION = """
REST API of the Click-n-Deploy App Store. The frontend uses it to browse the
app catalog, deploy apps into the caller's own OpenStack project and manage
courses and teams around those deployments.

## Authentication

Every endpoint except `/health`, `/auth/health` and the LTI handshake expects
`Authorization: Bearer <token>`. Two kinds of token are accepted:

* a **Keycloak access token** from the direct login, and
* an **LTI session token** this backend issues after a verified launch from
  Moodle (`POST /lti/launch`).

Both resolve to the same user. A few actions (linking a Moodle identity) must
be authorised by the account owner and refuse an LTI session with
`403 direct_login_required`.

## Roles

`student`, `teacher` and `admin`. Endpoints restricted by role answer
`403` with `{"code": "role_required", "required": [...]}`. Access to a single
resource (a deployment, a course) is decided per resource on top of that —
owner, team member, course teacher or admin.

## Errors

Errors use FastAPI's `{"detail": ...}` envelope. `detail` is either a plain
message or an object with a machine-readable `code` and a `message`, so the
frontend can tell failures apart without parsing text.

## Long-running work

Creating, destroying, pausing or resuming a deployment enqueues a Celery task
and returns immediately. Progress is streamed as Server-Sent Events from
`GET /deployments/{id}/stream`; the task history is at `/tasks`.
"""

OPENAPI_TAGS = [
    {"name": "Health", "description": "Unauthenticated liveness probe."},
    {
        "name": "Authentication",
        "description": "Auth subsystem probe. The current user is `GET /users/me`.",
    },
    {
        "name": "LTI",
        "description": "LTI 1.3 launch from Moodle (JWKS, OIDC login, launch, "
        "identity link), deep linking and mapping Moodle courses to courses. "
        "Answers `503` while `LTI_ENABLED=false`.",
    },
    {"name": "Users", "description": "Current user, user directory and per-user statistics."},
    {
        "name": "Courses",
        "description": "Courses are study groups (Studiengruppen), not Moodle courses — "
        "those are LTI contexts. Membership and course teachers.",
    },
    {
        "name": "Apps",
        "description": "App catalog: registering apps from a Git repository, their "
        "versions and the Terraform/Packer variables a deployment asks for.",
    },
    {
        "name": "Admin",
        "description": "Admin-only: reviewing submitted app versions and emergency "
        "deactivation of an app.",
    },
    {
        "name": "Deployments",
        "description": "Creating, inspecting, pausing, resuming and destroying "
        "deployments of an app version into the caller's OpenStack project, "
        "their cloud resources and a live event stream.",
    },
    {"name": "Tasks", "description": "Worker tasks belonging to a deployment and their status."},
    {"name": "Teams", "description": "Teams that share deployments."},
    {"name": "Quotas", "description": "Usage and limits of the caller's OpenStack project."},
    {"name": "Dashboard", "description": "Aggregated figures for the start page."},
    {
        "name": "OpenStack Credentials",
        "description": "The caller's own OpenStack credential. Stored encrypted, "
        "validated against Keystone before it is saved, and locked while the "
        "caller has deployments that are not destroyed.",
    },
    {
        "name": "OpenStack Resources",
        "description": "Read-only lookups in the caller's OpenStack project for the "
        "deployment wizard's dropdowns. Results are cached; `POST /refresh` clears it.",
    },
]
