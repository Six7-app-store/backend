# Backend — Click-n-Deploy App Store

FastAPI + SQLAlchemy + Alembic + Celery. Läuft nicht standalone, sondern
nur als Service im Stack aus `deployment/docker-compose.dev.yml`.

## Befehle

- Stack starten: `make dev-up` (im `deployment/`-Repo)
- Tests: `make test-backend-isolated` — fährt die Suite im Container gegen
  die isolierte DB `postgres-test`. `MODE=host` läuft stattdessen am Host
  gegen `localhost:55433`. Voller Lauf ca. 3,5 Minuten.
- Einzelne Datei: `docker exec backend-dev poetry run pytest tests/test_x.py --no-cov`
  (ohne `--no-cov` reißt der 50%-Gate den Einzellauf)
- Lint: `docker exec backend-dev poetry run ruff check .`
- Migration erzeugen: `docker exec backend-dev alembic revision --autogenerate -m "..."`
- Migration anwenden: `docker exec backend-dev alembic upgrade head`
- Logs: `docker logs -f backend-dev`

**Immer `poetry run` im Container.** Das venv liegt unter `/app/.venv` und ist
nicht im `PATH` — ein blankes `pytest` oder `python -m pytest` scheitert mit
`No module named pytest` und sieht aus, als gäbe es keine Tests.

Ohne `TEST_DATABASE_URL` leert die Suite die **Dev**-Datenbank. Deshalb immer
über das make-Target gehen, nie `pytest` blank.

## Konventionen

- Authentifizierung immer über `get_current_user` aus `app/utils/auth.py`.
  Nie `keycloak_auth` direkt importieren — die Dependency trägt beide Pfade
  (Keycloak-Bearer und LTI-Session) und unterscheidet sie am Issuer.
- Neue externe Identität = neue Zeile in `user_identities`
  (provider, issuer, subject). Niemals eine neue Spalte auf `users`: ein
  LTI-`sub` ist nur innerhalb einer Plattform eindeutig.
- Jede Modelländerung braucht eine Alembic-Migration im selben Commit.
- Ein `Course` ist eine **Studiengruppe**, kein Moodle-Kurs. Der Moodle-Kurs
  ist `LtiContext`. Die beiden nie gleichsetzen und `LtiContext.courseId`
  nie automatisch füllen — die Zuordnung ist eine bewusste Entscheidung.
- `LTI_ENABLED=false` ist der Kill-Switch: die `/lti`-Endpunkte antworten
  dann mit 503 und sonst ändert sich nichts.
- Zeilenlänge 100 (ruff), `alembic/` ist vom Linting ausgenommen.

## Definition of Done

- `ruff check .` und die Testsuite grün
- Coverage nicht gesunken (`fail_under = 50` in `pyproject.toml`)
- Modelländerung? Migration im selben Commit
- Architekturentscheidung getroffen? ADR unter `deployment/docs/adr/`

## Nicht anfassen

- `alembic/versions/` — bestehende Migrationen nie bearbeiten, nur neue
  erzeugen. Eine geänderte Migration zerlegt jede Datenbank, die sie
  bereits gefahren hat. Ein PreToolUse-Hook blockt das.
- `.env` und alles auf `*.pem`
- Kein Prod-Deploy, kein `git push --force`
