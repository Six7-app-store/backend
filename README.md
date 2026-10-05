# Backend

[![Coverage](https://img.shields.io/endpoint?url=https://six7-click-n-deploy.github.io/backend/badge.json)](https://six7-click-n-deploy.github.io/backend/)

FastAPI-Backend des App Stores. Nimmt REST-Anfragen vom Frontend entgegen, validiert Keycloak-Tokens, persistiert in PostgreSQL und dispatcht Deployment-Tasks an den Celery-Worker via RabbitMQ.

## Setup

Dieses Repository wird nicht eigenständig gestartet. Der gesamte Stack — inklusive Backend — wird über das deployment-Repository hochgefahren. Vollständige Anleitung: [deployment/README.md](https://github.com/six7-click-n-deploy/deployment#readme).

Voraussetzung für alle folgenden Befehle: `make dev-up` aus dem `deployment/`-Verzeichnis wurde ausgeführt und der Stack läuft.

## Entwicklung

Alle `make`-Befehle werden aus dem `deployment/`-Verzeichnis des [deployment-Repos](https://github.com/six7-click-n-deploy/deployment) ausgeführt — dort liegt das Makefile.

```bash
# in app-store/deployment
make test-backend       # pytest im Backend-Container
make lint-backend       # ruff check
make lint-backend-fix   # ruff check --fix
make format-backend     # ruff format
make shell-backend      # interaktive Shell im Container
```

## Datenbank-Migrationen

Ebenfalls aus `deployment/`:

```bash
# in app-store/deployment
make migrate-dev                              # Schema auf head bringen
make migration-create MSG="add foo column"    # Autogenerate aus Models
make migration-history                        # Migrations-Historie
make migration-current                        # aktuelle Revision
make migration-downgrade                      # eine Revision zurück
```

## API-Dokumentation

Swagger-UI mit allen Endpoints: http://localhost:8000/docs (nach `make dev-up`).

## Technologie-Stack

- **FastAPI** + **Uvicorn** (ASGI, 4 Worker)
- **SQLAlchemy 2.0** ORM, **Alembic** für Migrationen
- **Pydantic** für Request-/Response-Validierung
- **python-keycloak** für OIDC-Token-Validierung
- **Celery** als Producer (Tasks gehen an Worker via RabbitMQ)
- **Ruff** für Linting und Formatting
- **pytest** mit `unit`/`integration`/`api`-Markern

## Code-Struktur

Der Code liegt in `app/` und folgt einem Schichtenmodell: ein Request läuft `routers/` (HTTP + Auth) → `crud/` (DB-Zugriff) → `models.py` (ORM), fachliche Logik steckt in `services/`.

```
app/
├── main.py          # FastAPI-App, Router-Registrierung, Lifespan (startet u.a. den Event-Listener-Thread)
├── config.py        # Pydantic-Settings aus Env-Variablen
├── database.py      # SQLAlchemy-Engine + Session-Dependency
├── models.py        # ORM-Modelle (eine Tabelle = eine Klasse)
├── schemas.py       # Pydantic Request-/Response-Schemas
├── celery_app.py    # Celery-Producer (dispatcht Tasks an den Worker)
├── routers/         # HTTP-Endpoints, ein File pro Ressource (siehe unten)
├── crud/            # DB-Operationen, keine HTTP- oder Business-Logik (siehe unten)
├── services/        # Fachlogik & externe Integrationen (siehe unten)
└── utils/           # Querschnitts-Helfer (Auth, Crypto, Permissions, Zeit)
```

**routers/** — jeweils unter dem gezeigten Prefix eingebunden (`main.py`):

| Router | Prefix | Zweck |
|---|---|---|
| `auth_keycloak` | `/auth` | Token-Validierung, User-Anlage bei erstem Login |
| `users` | `/users` | User-Verwaltung |
| `courses` | `/courses` | Kurse (Lehrveranstaltungen) |
| `apps` | `/apps` | App-Katalog: Registrierung, Variablen, Versionen |
| `admin_apps` | `/admin` | Admin-Freigabe von App-Versionen |
| `deployments` | `/deployments` | Deployments anlegen/steuern (Kern-Ressource) |
| `tasks` | `/tasks` | Celery-Task-Status |
| `teams` | `/teams` | Teams innerhalb eines Kurses |
| `quotas` | `/quotas` | OpenStack-Quota-Abfrage |
| `dashboard` | `/dashboard` | Aggregierte Übersichten fürs Frontend |
| `openstack_credentials` | `/me/openstack-credentials` | OpenStack-Zugangsdaten pro User |
| `openstack_resources` | `/me/openstack/resources` | Read-API für Networks/Flavors/Images (Wizard-Dropdowns) |

**crud/** — je ein File pro Ressource (`users`, `apps`, `courses`, `teams`, `tasks`, `app_version_approvals`, `openstack_credentials`), das reine DB-Operationen für das gleichnamige Modell kapselt — keine HTTP- oder Business-Logik. Zwei fallen aus dem Muster:

- `deployments` — mit Abstand am umfangreichsten, da Deployments die Kern-Ressource sind (Status-Übergänge, Task-Verknüpfung, Redeploy)
- `locks` — keine Ressource, sondern Per-User-Serialisierung über Postgres Advisory Locks (verhindert parallele Deployments desselben Users)

**services/** — Fachlogik und externe Integrationen:

| Service | Zweck |
|---|---|
| `celery_event_listener` | Daemon-Thread: konsumiert Celery-Events aus RabbitMQ, schreibt Task-Status in die DB |
| `reconciler` | Fängt hängende Tasks ab, deren Events verloren gingen (Fallback zum Listener) |
| `deployment_pubsub` | In-Process-Bridge Listener → SSE-Endpoint (Live-Status) |
| `deployment_status` | Verheiratet gecachten OpenTofu-State mit Live-OpenStack-Daten (Infrastructure-Tab) |
| `deployment_notifier` | Baut & versendet Post-Deploy-Mails (User- und Owner-Variante) |
| `email_service` | SMTP-Versand + Jinja2-Templating für die Mails |
| `task_service` | Zwei-Phasen-Dispatch: PENDING-Row in Tx anlegen, dann Celery-Enqueue |
| `lifecycle` | Single Source of Truth, welche Aktion in welchem Deployment-Status erlaubt ist |
| `git_service` | Git-Clone/Release-Infos, Sparse-Checkout für den Variablen-Scan |
| `openstack_client` | Gemeinsamer OpenStack-Client-Layer (per-User-`Connection`) |
| `openstack_validator` | Prüft Credentials gegen das Ziel-Keystone (Upsert + `/test`) |
| `clouds_yaml_parser` | Parst rohes `clouds.yaml` in ein Credential-Schema |
| `tofu_state_parser` | Liest den in `Task.tf_state` persistierten OpenTofu-State |

**utils/** — `keycloak_auth` (Token-Validierung), `permissions` + `capabilities` (rollenbasierte `can_*`/`ensure_*`-Checks), `crypto` (Fernet-Verschlüsselung at-rest, Key geteilt mit dem Worker), `app_image` (Data-URL ↔ Bytes), `time`.

## Mehr

- Architektur und projektübergreifende Doku: [.github-Repo](https://github.com/six7-click-n-deploy/.github)
- Worker-Service: [worker-Repo](https://github.com/six7-click-n-deploy/worker)
