"""Chi c'era già prima dell'obbligo di conferma email deve poter entrare.

La colonna confirmed è nata a 0 anche per gli account vecchi: il login
rispondeva «Conferma prima la tua email» e il reset della password non
sbloccava niente. Un codice arrivato sulla casella, invece, la dimostra.
"""
import hashlib
import secrets
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.database import conferma_account_precedenti, engine
from app.models import Notification, NotificationType, User
from app.routes import auth
from app.utils.lingue_ui import COOKIE
from app.utils.orari import now_italy_naive
from app.utils.notification_types import sostituisci_marchio_helpy
from app.utils.password import hash_password, is_legacy_hash, verify_password
from app.utils.rate_limit import reset_rate_limit

RADICE = Path(__file__).resolve().parent.parent
PASSWORD = "password-di-prova"


def _lingua(http, codice):
    http.cookies.delete(COOKIE, path="/")
    http.get(f"/lingua/{codice}", follow_redirects=False)


def _utente(**campi):
    email = campi.pop("email", f"vecchio-{secrets.token_hex(4)}@test.local")
    dati = dict(
        email=email,
        password_md5=hash_password(PASSWORD),
        nome="Mario",
        confirmed=0,
    )
    dati.update(campi)
    with Session(engine) as s:
        u = User(**dati)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id, u.email


def _cancella(uid):
    with Session(engine) as s:
        u = s.get(User, uid)
        if u:
            s.delete(u)
            s.commit()


def _stato(uid):
    with Session(engine) as s:
        u = s.get(User, uid)
        return u.confirmed, u.confirmation_code


def _azzera_creazione(uid):
    """La colonna ha un default: per provare created_at assente serve l'SQL."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text('UPDATE "user" SET created_at = NULL WHERE id = :id'), {"id": uid})


@pytest.fixture
def http(csrf_client):
    reset_rate_limit()
    yield csrf_client
    csrf_client.get("/logout")
    _lingua(csrf_client, "it")
    reset_rate_limit()


class TestAccountPrecedenti:
    def test_senza_codice_in_sospeso_diventa_confermato_e_puo_entrare(self, http):
        vecchio, email = _utente(created_at=datetime(2026, 10, 9, 18, 0))
        recente, _ = _utente(confirmation_code="123456", created_at=datetime(2026, 10, 11, 9, 0))
        google, _ = _utente(
            confirmation_code="999999",
            google_id=f"sub-{secrets.token_hex(4)}",
            created_at=datetime(2026, 10, 11, 9, 0),
        )
        try:
            conferma_account_precedenti()
            assert _stato(vecchio)[0] == 1
            assert _stato(recente) == (0, "123456")
            assert _stato(google)[0] == 1
            conferma_account_precedenti()
            assert _stato(recente)[0] == 0

            risposta = http.post("/api/login", data={"email": email, "password": PASSWORD})
            assert risposta.status_code == 200, risposta.text
        finally:
            _cancella(vecchio)
            _cancella(recente)
            _cancella(google)

    def test_la_password_vecchia_si_migra_e_non_blocca(self, http):
        md5 = hashlib.md5(PASSWORD.encode("utf-8")).hexdigest()
        uid, email = _utente(password_md5=md5, created_at=datetime(2026, 10, 9, 18, 0))
        try:
            conferma_account_precedenti()
            risposta = http.post("/api/login", data={"email": email, "password": PASSWORD})
            assert risposta.status_code == 200, risposta.text
            with Session(engine) as s:
                salvata = s.get(User, uid).password_md5
            assert not is_legacy_hash(salvata)
            assert verify_password(PASSWORD, salvata) == (True, False)
        finally:
            _cancella(uid)


class TestCodiceInSospeso:
    """Il caso dello staging: account vecchio, codice già in sospeso, e chi è nuovo
    deve poter scrivere quel codice direttamente nel login."""

    def test_anche_con_un_codice_vecchio_la_migrazione_conferma(self):
        prima = datetime(2026, 10, 9, 15, 17)
        uid, _ = _utente(
            created_at=prima,
            confirmation_code="111111",
            confirmation_code_created_at=prima,
        )
        dopo = datetime(2026, 10, 11, 9, 0)
        recente, _ = _utente(
            created_at=dopo,
            confirmation_code="222222",
            confirmation_code_created_at=dopo,
        )
        senza_data, _ = _utente(confirmation_code="333333")
        _azzera_creazione(senza_data)
        try:
            conferma_account_precedenti()
            assert _stato(uid) == (1, None)
            assert _stato(senza_data) == (1, None)
            assert _stato(recente) == (0, "222222")
            conferma_account_precedenti()
            assert _stato(recente) == (0, "222222")
            assert _stato(uid) == (1, None)
        finally:
            _cancella(uid)
            _cancella(recente)
            _cancella(senza_data)

    def test_la_password_giusta_di_un_account_vecchio_non_si_blocca(self, http):
        prima = datetime(2026, 10, 9, 15, 17)
        uid, email = _utente(
            created_at=prima,
            confirmation_code="111111",
            confirmation_code_created_at=prima,
        )
        try:
            sbagliata = http.post("/api/login", data={"email": email, "password": "no-non-va-1"})
            assert sbagliata.status_code == 401
            assert _stato(uid) == (0, "111111")

            # Nessuna migrazione prima: è chi entra col codice ancora in sospeso.
            giusto = http.post("/api/login", data={"email": email, "password": PASSWORD})
            assert giusto.status_code == 200, giusto.text
            assert _stato(uid) == (1, None)

            senza, email_senza = _utente(confirmation_code="444444")
            _azzera_creazione(senza)
            try:
                entra = http.post("/api/login", data={"email": email_senza, "password": PASSWORD})
                assert entra.status_code == 200, entra.text
                assert _stato(senza) == (1, None)
            finally:
                _cancella(senza)
        finally:
            _cancella(uid)

    def test_un_iscritto_nuovo_resta_fuori_e_conferma_dalla_pagina(self, http, monkeypatch):
        monkeypatch.setattr(auth, "generate_verification_code", lambda: "654321")
        monkeypatch.setattr(auth, "send_verification_email", lambda *a, **k: True)
        uid, email = _utente(
            created_at=datetime(2026, 10, 11, 10, 0),
            confirmation_code="000000",
            confirmation_code_created_at=datetime(2026, 10, 1),
        )
        try:
            _lingua(http, "en")
            blocco = http.post("/api/login", data={"email": email, "password": PASSWORD})
            assert blocco.status_code == 403
            assert blocco.json()["requires_verification"] is True
            assert _stato(uid) == (0, "000000")

            pagina = http.get("/login").text
            assert 'id="codiceConferma"' in pagina
            assert 'id="confermaCodice"' in pagina
            assert "Confirm email" in pagina
            assert "/conferma-email" in pagina
            assert "/api/verify-email" in pagina
            assert "Conferma email" not in pagina

            _lingua(http, "it")
            italiana = http.get("/login").text
            assert "Conferma email" in italiana
            assert "Confirm email" not in italiana

            reinvio = http.post("/api/resend-verification", data={"email": email})
            assert reinvio.status_code == 200, reinvio.text
            verifica = http.post("/api/verify-email", data={"email": email, "code": "654321"})
            assert verifica.status_code == 200, verifica.text
            assert verifica.json()["redirect_url"] == "/profile"
            profilo = http.get("/profile")
            assert "schedaAccount" in profilo.text
            assert _stato(uid) == (1, None)
        finally:
            _cancella(uid)

    def test_il_link_della_mail_conferma_e_fa_entrare(self, http):
        uid, email = _utente(
            created_at=datetime(2026, 10, 11, 10, 0),
            confirmation_code="654321",
            confirmation_code_created_at=now_italy_naive(),
        )
        try:
            _lingua(http, "it")
            italiano, _ = auth.corpo_email_verifica("Mario", "654321", "it", email)
            inglese, _ = auth.corpo_email_verifica("Mario", "654321", "en", email)
            assert "/conferma-email?" in italiano
            assert "code=654321" in italiano
            assert "pagina di conferma" in italiano
            assert "/conferma-email?" in inglese
            assert "code=654321" in inglese
            assert "confirmation page" in inglese
            assert "sign-up page" not in inglese
            assert "Ciao" not in inglese

            solo_pagina = http.get("/conferma-email", params={"email": email})
            assert solo_pagina.status_code == 200
            assert 'id="codiceConferma"' in solo_pagina.text
            assert _stato(uid) == (0, "654321")

            sbagliato = http.get("/conferma-email", params={"email": email, "code": "000000"})
            assert sbagliato.status_code == 200
            assert "Codice non valido" in sbagliato.text
            assert _stato(uid) == (0, "654321")

            risposta = http.get(
                "/conferma-email",
                params={"email": email, "code": "654321"},
                follow_redirects=False,
            )
            assert risposta.status_code == 303
            assert risposta.headers["location"].endswith("/profile")
            profilo = http.get("/profile")
            assert "schedaAccount" in profilo.text
            assert _stato(uid) == (1, None)
        finally:
            http.get("/logout")
            _cancella(uid)


class TestResetConferma:
    def test_il_codice_giusto_conferma_lemail(self, http, monkeypatch):
        monkeypatch.setattr(auth, "generate_verification_code", lambda: "542607")
        monkeypatch.setattr(auth, "send_reset_password_email", lambda *a, **k: True)
        uid, email = _utente(confirmation_code="111111")
        try:
            assert http.post("/api/request-password-reset", data={"email": email}).status_code == 200
            risposta = http.post("/api/reset-password", data={
                "email": email, "code": "542607", "new_password": "NuovaPassword1",
            })
            assert risposta.status_code == 200, risposta.text
            with Session(engine) as s:
                u = s.get(User, uid)
                assert u.confirmed == 1
                assert u.confirmation_code is None
                assert verify_password("NuovaPassword1", u.password_md5)[0]
        finally:
            _cancella(uid)

    def test_il_codice_sbagliato_non_conferma(self, http, monkeypatch):
        monkeypatch.setattr(auth, "generate_verification_code", lambda: "542607")
        monkeypatch.setattr(auth, "send_reset_password_email", lambda *a, **k: True)
        uid, email = _utente()
        try:
            http.post("/api/request-password-reset", data={"email": email})
            risposta = http.post("/api/reset-password", data={
                "email": email, "code": "000000", "new_password": "NuovaPassword1",
            })
            assert risposta.status_code == 400
            assert _stato(uid)[0] == 0
        finally:
            _cancella(uid)


class TestLoginBloccato:
    def test_il_messaggio_segue_la_lingua_e_c_e_il_reinvio(self, http, monkeypatch):
        monkeypatch.setattr(auth, "send_verification_email", lambda *a, **k: True)
        uid, email = _utente(confirmation_code="123456")
        try:
            _lingua(http, "en")
            pagina = http.get("/login").text
            assert "Resend confirmation email" in pagina
            assert "Reinvia l'email di conferma" not in pagina
            assert 'id="reinviaConferma"' in pagina

            risposta = http.post("/api/login", data={"email": email, "password": PASSWORD})
            assert risposta.status_code == 403
            corpo = risposta.json()
            assert corpo["requires_verification"] is True
            assert corpo["email"] == email
            assert corpo["error"].startswith("Confirm your email first")
            assert "Conferma prima" not in corpo["error"]

            _lingua(http, "it")
            pagina = http.get("/login").text
            assert "email di conferma" in pagina
            assert "Resend confirmation email" not in pagina
            risposta = http.post("/api/login", data={"email": email, "password": PASSWORD})
            assert risposta.status_code == 403
            assert risposta.json()["error"].startswith("Conferma prima la tua email")
        finally:
            _cancella(uid)

    def test_il_reinvio_e_limitato(self, http, monkeypatch):
        monkeypatch.setattr(auth, "send_verification_email", lambda *a, **k: True)
        uid, email = _utente(confirmation_code="123456")
        try:
            _lingua(http, "en")
            for _ in range(3):
                ok = http.post("/api/resend-verification", data={"email": email})
                assert ok.status_code == 200, ok.text
            blocco = http.post("/api/resend-verification", data={"email": email})
            assert blocco.status_code == 429
            assert "Too many attempts" in blocco.json()["error"]
        finally:
            _cancella(uid)


class TestMarchio:
    def test_helpy_negli_oggetti_diventa_ispiramy(self):
        chiave = f"helpy_test_{secrets.token_hex(4)}"
        with Session(engine) as s:
            tipo = NotificationType(
                type_key=chiave,
                name="Avviso Helpy",
                description="Prova Helpy",
                in_app=False,
                send_email=True,
                email_subject="Qualcuno vuole contattarti su Helpy!",
                email_template="community_contact.html",
                is_active=True,
            )
            s.add(tipo)
            s.commit()
            s.refresh(tipo)
            tipo_id = tipo.id
            utente = User(
                email=f"helpy-{secrets.token_hex(4)}@test.local",
                password_md5="x",
                confirmed=1,
                nome="Ada",
            )
            s.add(utente)
            s.commit()
            s.refresh(utente)
            uid = utente.id
            avviso = Notification(
                user_id=uid,
                type="community_contact",
                title="Messaggio su Helpy",
                message="Qualcuno vuole contattarti su Helpy!",
            )
            s.add(avviso)
            s.commit()
            s.refresh(avviso)
            avviso_id = avviso.id
        try:
            assert sostituisci_marchio_helpy() >= 1
            with Session(engine) as s:
                tipo = s.get(NotificationType, tipo_id)
                assert tipo.email_subject == "Qualcuno vuole contattarti su Ispiramy!"
                assert tipo.name == "Avviso Ispiramy"
                assert "Helpy" not in (tipo.description or "")
                avviso = s.get(Notification, avviso_id)
                assert avviso.title == "Messaggio su Ispiramy"
                assert "Helpy" not in avviso.message
            assert sostituisci_marchio_helpy() == 0
        finally:
            with Session(engine) as s:
                for modello, id_ in ((Notification, avviso_id), (NotificationType, tipo_id), (User, uid)):
                    riga = s.get(modello, id_)
                    if riga:
                        s.delete(riga)
                s.commit()

    def test_i_testi_visibili_non_dicono_helpy(self):
        for cartella in ("templates", "traduzioni"):
            for percorso in (RADICE / "app" / cartella).rglob("*"):
                if percorso.suffix in {".html", ".json"}:
                    assert "Helpy" not in percorso.read_text(encoding="utf-8"), percorso
        for nome in (
            "migration_add_reviews.sql",
            "migration_add_reviews_postgres.sql",
            "migration_add_community_contact_notification.sql",
            "migration_add_community_contact_notification_postgres.sql",
        ):
            testo = (RADICE / "sql_update" / nome).read_text(encoding="utf-8")
            assert "Helpy" not in testo
            assert "Ispiramy" in testo
