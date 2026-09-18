---
name: neue-migration
description: Ablauf für eine Alembic-Migration, wenn sich ein SQLAlchemy-Modell in app/models.py geändert hat — erzeugen, prüfen, anwenden, zurückrollen.
---

# Neue Alembic-Migration

Gilt, sobald sich etwas an `app/models.py` ändert: neue Tabelle, neue Spalte,
geänderter Typ, neuer Constraint, neuer Index.

**Eine Modelländerung ohne Migration im selben Commit ist unvollständig.** Der
nächste, der `alembic upgrade head` fährt, bekommt ein Schema, das nicht zum
Code passt.

## Ablauf

```bash
# 1. Stack muss laufen
make dev-up                       # im deployment/-Repo

# 2. Migration erzeugen lassen
docker exec backend-dev alembic revision --autogenerate -m "kurze beschreibung"

# 3. Erzeugte Datei LESEN und korrigieren  ← nicht überspringen
#    alembic/versions/<datum>-<hash>_<beschreibung>.py

# 4. Anwenden
docker exec backend-dev alembic upgrade head

# 5. Rückwärts testen und wieder vor
docker exec backend-dev alembic downgrade -1
docker exec backend-dev alembic upgrade head
```

Schritt 5 ist der, den man weglässt und später bereut. Eine Migration, die
nicht zurückläuft, blockiert jedes Rollback in Staging und Produktion.

## Was `--autogenerate` regelmäßig falsch macht

Autogenerate vergleicht Modelle mit dem Schema und rät den Rest. Diese Fälle
immer von Hand nachziehen:

- **Umbenennungen** werden als `drop_column` + `add_column` erzeugt — das wirft
  die Daten weg. Zu `alter_column` mit `new_column_name` umschreiben.
- **Server-Defaults** erkennt es nicht zuverlässig. Bei `server_default`
  nachsehen, ob es in der Migration steht.
- **Enum-Typen** in Postgres: ein neuer Wert in einem `Enum` erzeugt oft gar
  nichts. Der Wert muss per `ALTER TYPE ... ADD VALUE` ergänzt werden.
- **NOT NULL auf einer Tabelle mit Zeilen** scheitert ohne `server_default`
  oder ein vorheriges `UPDATE`.
- **Indizes und Unique-Constraints**, die nur im Modell als `__table_args__`
  stehen, fehlen manchmal.

## Tabu

Bestehende Migrationen in `alembic/versions/` werden **nie** bearbeitet. Eine
geänderte Migration zerlegt jede Datenbank, die sie schon gefahren hat: dort ist
die alte Fassung bereits gelaufen und in `alembic_version` vermerkt, die neue
läuft nie. Das Schema driftet auseinander, ohne dass es auffällt.

Ein PreToolUse-Hook blockt solche Änderungen. Wenn er anschlägt, ist die
Antwort immer: neue Migration erzeugen, nicht die alte anfassen.

Korrektur einer fehlerhaften Migration, die schon gemerged ist → eine **neue**
Migration, die das Falsche geraderückt.

## Danach

- Tests laufen lassen: `make test-backend-isolated`
- Betrifft die Änderung das Datenmodell grundlegend (neue Entität, geänderte
  Beziehung)? Dann gehört ein ADR nach `deployment/docs/adr/` dazu.
