
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # Celery (optional - only needed for API runtime, not for migrations)
    CELERY_BROKER_URL: str = "amqp://admin:admin@rabbitmq:5672/"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/0"

    # Git
    TEMP_REPO_BASE_PATH: str = "/tmp/worker_repos"
    GIT_ACCESS_TOKEN: str = ""  # Token for HTTPS git authentication

    # Keycloak — single source of truth for authentication
    KEYCLOAK_SERVER_URL: str = "http://keycloak:8080"
    KEYCLOAK_REALM: str = "dhbw"
    KEYCLOAK_CLIENT_ID: str = "appstore-backend"
    KEYCLOAK_CLIENT_SECRET: str = ""  # Set via environment variable

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Symmetric Fernet key shared with the worker. Used to encrypt OpenStack
    # credentials at rest and to seal the envelope shipped through Celery.
    # Generate: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    CREDENTIAL_ENCRYPTION_KEY: str

    # SMTP (Gmail). Required for the post-deploy notification mails.
    # Use a Google "App password" (the regular password won't work with
    # 2FA enabled).
    #
    # SMTP_ENABLED is the explicit kill-switch — set it to False to turn
    # mail delivery into a no-op even when credentials are populated. It
    # lives separately from the credentials so operators can keep the
    # app-password in .env while disabling mail in dev/CI, and so the
    # resend-access endpoint can distinguish "we chose not to send"
    # (HTTP 503) from "SMTP refused" (HTTP 502).
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "Click-n-Deploy"

    # Public URL the deployment detail page is reachable under, used in
    # the owner-summary mail to deep-link back into the UI. No trailing
    # slash. Falls back to the first CORS origin in dev.
    APP_BASE_URL: str = "http://localhost:5173"

    # ----------------------------------------------------------------
    # LTI 1.3 — Moodle as the platform, this app store as the tool
    # ----------------------------------------------------------------
    # LTI_ENABLED is the kill-switch. With it off the /lti endpoints
    # answer 503 and nothing else in the app changes, so an environment
    # without a registered Moodle (staging, CI, a fresh checkout) boots
    # normally on empty defaults.
    LTI_ENABLED: bool = False

    # The five values Moodle hands out when the tool is registered.
    # LTI_PLATFORM_ISSUER must equal Moodle's wwwroot exactly — it is
    # the ``iss`` claim of every id_token and the key this tool's
    # configuration is looked up under.
    LTI_PLATFORM_ISSUER: str = ""
    LTI_CLIENT_ID: str = ""
    LTI_DEPLOYMENT_ID: str = ""
    LTI_JWKS_URL: str = ""        # .../mod/lti/certs.php
    LTI_AUTH_LOGIN_URL: str = ""  # .../mod/lti/auth.php
    LTI_TOKEN_URL: str = ""       # .../mod/lti/token.php

    # This tool's own RSA private key, PEM, base64-encoded so it fits on
    # one .env line. The public half is derived from it at startup
    # rather than configured separately — two values that must match
    # are two values that can drift.
    #
    # Generate:
    #   python -c "import base64;from cryptography.hazmat.primitives import serialization as s;from cryptography.hazmat.primitives.asymmetric import rsa;k=rsa.generate_private_key(public_exponent=65537,key_size=2048);print(base64.b64encode(k.private_bytes(s.Encoding.PEM,s.PrivateFormat.PKCS8,s.NoEncryption())).decode())"
    LTI_PRIVATE_KEY_B64: str = ""

    # Where PyLTI1p3 keeps ``state`` and ``nonce`` between the login and
    # the launch request. FastAPI has no session, so the library's
    # session-backed storage is not an option — this points at Redis,
    # deliberately on a different database than Celery's result backend.
    LTI_REDIS_URL: str = "redis://redis:6379/1"

    # The app store's own session token, issued after a successful
    # launch. Keycloak cannot be used inside the Moodle frame: its
    # silent refresh runs through a hidden iframe, and browsers block
    # third-party cookies there. Short-lived by design — there is no
    # refresh, the user re-launches from Moodle.
    LTI_SESSION_SECRET: str = ""
    LTI_SESSION_TTL_MINUTES: int = 120

    # A launch whose Moodle identity is unknown but whose e-mail
    # address already belongs to an account is refused and handed a
    # link challenge instead. Spending it requires a direct sign-in, so
    # the window only has to outlast one login round trip.
    LTI_LINK_CHALLENGE_TTL_MINUTES: int = 10

    # How long a pending deep-link selection stays valid. Moodle hands
    # the tool its return URL when the lecturer starts adding the
    # activity; the choice is made on our side and posted back. The
    # window has to outlast a human picking from a list, not a redirect,
    # which is why it is minutes rather than seconds — but it is still
    # one-shot, so a longer window is not a second chance.
    LTI_DEEP_LINK_TTL_MINUTES: int = 30

    # Frontend route the launch redirects to, with the session token.
    LTI_LAUNCH_REDIRECT_URL: str = "http://localhost:5173/lti/callback"

    # Frontend route a refused launch redirects to, with the challenge.
    # A launch is a form POST from Moodle that lands in the browser, so
    # the refusal has to be a page the person can act on rather than a
    # JSON body.
    LTI_LINK_REDIRECT_URL: str = "http://localhost:5173/lti/link"

    # Whether a Moodle course role of Instructor grants the global
    # TEACHER role here. Off by default: the teacher role controls
    # foreign OpenStack resources, so elevation stays an explicit
    # administrative decision per platform rather than something any
    # registered Moodle can hand out. The course role still travels
    # with the launch either way.
    LTI_TRUST_INSTRUCTOR_ROLE: bool = False

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
