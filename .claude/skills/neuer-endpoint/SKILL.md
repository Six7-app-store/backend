---
name: neuer-endpoint
description: Muster für einen neuen FastAPI-Endpunkt — Router, Schema, CRUD, Berechtigungsprüfung, Test und der passende Frontend-Aufruf.
---

# Neuer Endpunkt

Ein Endpunkt ist bei uns nie eine Datei. Es sind fünf Stellen, und wer eine
vergisst, merkt es erst im Review.

## Die fünf Stellen

| Stelle | Datei | Was dort hinkommt |
|---|---|---|
| 1. Schema | `app/schemas.py` | Request- und Response-Modell (Pydantic) |
| 2. CRUD | `app/crud/<ressource>.py` | Datenbankzugriff, keine HTTP-Logik |
| 3. Router | `app/routers/<ressource>.py` | Route, Dependencies, Statuscodes |
| 4. Test | `tests/test_<ressource>.py` | mindestens Erfolg + ein verweigerter Zugriff |
| 5. Frontend | `frontend/src/api/<ressource>.api.ts` | falls die Oberfläche ihn braucht |

Schritt 5 wird am häufigsten vergessen. Das Backend liefert dann einen
Endpunkt, den niemand aufruft — oder das Frontend ruft einen auf, der anders
heißt als gedacht. Gegenprobe: `curl http://localhost:8000/openapi.json` zeigt,
was das Backend tatsächlich anbietet, und lässt sich gegen `src/api/*.ts`
abgleichen.

## Vorlage für den Router

Such dir einen bestehenden Endpunkt derselben Art und folge ihm.
`app/routers/courses.py` ist ein gutes Vorbild für CRUD auf einer Ressource.

```python
@router.get("/{course_id}", response_model=CourseWithUsers)
def get_course(
    course_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Kurze Beschreibung — landet in der OpenAPI-Doku."""
    course = crud_courses.get_course(db, course_id)
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    ensure_view_course(current_user, course, db=db)
    return course
```

**Authentifizierung immer über `get_current_user` aus `app/utils/auth.py`.**
Nie `keycloak_auth` direkt importieren — die Dependency trägt beide Pfade,
Keycloak-Bearer und LTI-Session, und unterscheidet sie am Issuer. Wer am
Keycloak-Helfer vorbeigeht, sperrt alle aus, die über Moodle kommen.

## Berechtigung: zwei Ebenen, nicht eine

- **Rolle** über `app/utils/permissions.py`: `require_admin`, `require_staff`,
  `require_roles(...)` — als Dependency am Endpunkt.
- **Objektbezug** über `app/utils/capabilities.py`: `ensure_edit_course`,
  `ensure_view_app`, `ensure_deployment_access` — im Funktionskörper, sobald das
  Objekt geladen ist.

Die Rolle allein reicht selten. „Ist Dozent" heißt nicht „ist Dozent *dieses*
Kurses". Wenn es für deinen Fall noch keine `ensure_*`-Funktion gibt, kommt sie
nach `capabilities.py` — nicht als `if` in den Router.

## Statuscodes

- `404` wenn das Objekt nicht existiert
- `403` wenn es existiert, aber nicht zugänglich ist
- `400` bei fehlerhafter Eingabe, die Pydantic nicht abfängt
- `201` bei Erzeugung, mit dem erzeugten Objekt im Body

Ob `404` oder `403` bei einem fremden Objekt richtig ist, hängt davon ab, ob die
bloße Existenz schon eine Information ist. Bei uns: im Zweifel `404`.

## Test

Zwei Fälle sind das Minimum:

```python
@pytest.mark.unit
def test_get_course_returns_it(client, seeded_course):
    r = client.get(f"/courses/{seeded_course.courseId}")
    assert r.status_code == 200

@pytest.mark.unit
def test_get_course_denies_foreign_user(client_as_student, foreign_course):
    r = client_as_student.get(f"/courses/{foreign_course.courseId}")
    assert r.status_code in (403, 404)
```

Die Fixtures in `tests/conftest.py` überschreiben `get_current_user` — wenn du
einen anderen Auth-Weg baust, muss die Fixture mit.

Marker nicht vergessen: `-m unit` und `-m integration` trennen die CI-Läufe.

## Fertig, wenn

- `docker exec backend-dev poetry run ruff check .` grün
- `make test-backend-isolated` grün
- Coverage nicht gesunken
- `src/api/*.ts` nachgezogen, falls die Oberfläche den Endpunkt braucht
