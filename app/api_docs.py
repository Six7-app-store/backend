"""Texts for the generated API documentation (``/docs``, ``/redoc``).

Kept out of ``main.py`` so the app wiring stays readable. Every tag a
router uses must be declared in ``OPENAPI_TAGS`` -- a unit test checks it.
"""

API_DESCRIPTION = """
REST API of the Click-n-Deploy App Store: browse the app catalog, deploy apps
into the caller's own OpenStack project and manage courses and teams.

**Auth** — `Authorization: Bearer <token>`, either a Keycloak access token or
an LTI session token (issued after a verified Moodle launch, `POST /lti/launch`).
Both resolve to the same user; a few actions (linking a Moodle identity) refuse
an LTI session with `403 direct_login_required`.

**Roles** — `student`, `teacher`, `admin`; role-gated endpoints answer `403
{"code": "role_required", ...}`. Resource access is decided per resource on top.

**Errors** — FastAPI's `{"detail": ...}` envelope; `detail` may carry a
machine-readable `code`.
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
        "and their cloud resources. These actions enqueue a Celery task and "
        "return at once; progress streams via SSE from `GET /deployments/{id}/stream`.",
    },
    {"name": "Tasks", "description": "Worker tasks belonging to a deployment and their status."},
    {"name": "Teams", "description": "Teams that share deployments."},
    {"name": "Quotas", "description": "Usage and limits of the caller's OpenStack project."},
    {"name": "Dashboard", "description": "Aggregated figures for the start page."},
    {
        "name": "UI Settings",
        "description": "The instance's look: accent colour and logos. Readable without "
        "authentication (the login page uses them), changeable by admins only.",
    },
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
