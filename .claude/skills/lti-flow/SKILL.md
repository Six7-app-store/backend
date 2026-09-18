---
name: lti-flow
description: Wie der Moodle-LTI-1.3-Launch funktioniert — Endpunkte, Sitzungsmodell, Rollenauflösung und die Fallen beim lokalen Testen.
---

# LTI-1.3-Launch

Moodle ist die Plattform, der App Store das Tool. Laden, sobald etwas an
`app/routers/lti.py`, `app/services/lti_service.py`, `app/utils/lti_*.py` oder
an den Modellen `UserIdentity` / `LtiContext` angefasst wird.

## Der Ablauf

```
Moodle  ──POST──►  /lti/login      OIDC-Initiierung
                        │          state + nonce → Redis DB 1
                        ▼
                   /lti/launch     id_token gegen Moodles JWKS geprüft
                        │
      Identität bekannt ├──►  eigenes Session-Token  ──►  /lti/callback
                        │
      E-Mail vergeben   └──►  Link-Challenge         ──►  /lti/link
```

`/lti/jwks` veröffentlicht den öffentlichen Teil unseres Schlüssels. Moodle
holt ihn dort ab, um unsere Anfragen zu prüfen.

## Warum ein eigenes Session-Token

Keycloak kann die Sitzung im Moodle-Frame nicht tragen: der stille Refresh
läuft über ein verstecktes Iframe, und Browser blocken dort Third-Party-Cookies.
Deshalb stellt das Tool nach einem erfolgreichen Launch ein eigenes,
kurzlebiges Token aus.

Es gibt **keinen Refresh**. Läuft es ab, startet die Person den Launch in
Moodle neu. Deshalb darf ein 401 auf einer LTI-Sitzung nie zum Keycloak-Login
umleiten — das Frontend schickt stattdessen nach `/lti/expired`.

## Warum Redis DB 1

PyLTI1p3 legt `state` und `nonce` zwischen Login und Launch ab. FastAPI hat
keine Session, also übernimmt das Redis. **DB 1**, weil Celery DB 0 hält. Nicht
zusammenlegen.

## Rollen

`_resolve_role` in `lti_service.py` entscheidet die **globale** Rolle:

- Eine Moodle-Kursrolle stuft ein bestehendes Konto **nie herab**. Wer hier
  Admin ist, bleibt Admin.
- `Instructor` vergibt die globale Dozentenrolle nur, wenn
  `LTI_TRUST_INSTRUCTOR_ROLE=true` gesetzt ist. Default ist `false`, weil diese
  Rolle fremde OpenStack-Ressourcen steuert — die Freigabe bleibt eine bewusste
  Admin-Entscheidung.
- Die Kursrolle selbst reist unabhängig davon im Session-Token mit.

## Identität

`UserIdentity(provider, issuer, subject)` — die drei zusammen. **Niemals nur
`subject`:** ein LTI-`sub` ist nur innerhalb einer Plattform eindeutig. Zwei
Moodle-Instanzen können denselben `sub` für verschiedene Personen vergeben.

Ist die Moodle-Identität unbekannt, die E-Mail aber schon vergeben, wird der
Launch **abgelehnt** und eine Link-Challenge ausgestellt. Sie wird an
`POST /lti/link` nach einer direkten Anmeldung eingelöst. Eine Adresse allein
ist kein Eigentumsnachweis.

Umgekehrt übernimmt `sync_user_from_keycloak` ein per LTI angelegtes Konto über
die E-Mail, statt ein zweites anzulegen — `users.email` ist UNIQUE, sonst
scheitert jede authentifizierte Anfrage dieser Person mit `IntegrityError`.

## Kurs ≠ Kurs

`Course` ist bei uns eine **Studiengruppe**. Der Moodle-Kurs ist `LtiContext`.
`LtiContext.courseId` ist nullable und bleibt leer, bis jemand die Zuordnung
bewusst vornimmt. Nicht automatisch füllen — das würde eine Zuordnung
behaupten, die niemand bestätigt hat.

## Lokal testen

Anleitung: `deployment/docs/moodle-lti-dev.md`. Die drei Fallen:

1. **`host.docker.internal`, nicht `localhost`.** Nur unter diesem Namen
   erreichen Browser und Container dieselbe Maschine. Vite muss ihn in
   `allowedHosts` haben, sonst antwortet es mit einem nackten 403, das wie ein
   kaputter Launch aussieht.
2. **Ausnahme `/lti/link`.** Diese Seite verlangt eine direkte
   Keycloak-Anmeldung, deren PKCE `crypto.subtle` braucht — das gibt der
   Browser über http nur für `localhost` her. Deshalb zeigt
   `LTI_LINK_REDIRECT_URL` bewusst dorthin.
3. **`LTI_PLATFORM_ISSUER` muss exakt Moodles `wwwroot` sein.** Es ist der
   `iss`-Claim jedes id_tokens und der Schlüssel, unter dem die Konfiguration
   nachgeschlagen wird. Ein Zeichen daneben und der Launch scheitert mit einer
   Meldung, die nicht danach aussieht.

## Kill-Switch

`LTI_ENABLED=false` → die `/lti`-Endpunkte antworten mit 503, sonst ändert sich
nichts. Eine Umgebung ohne registriertes Moodle startet auf leeren Defaults.
Beim Ändern der Konfiguration immer prüfen, dass dieser Pfad heil bleibt.
