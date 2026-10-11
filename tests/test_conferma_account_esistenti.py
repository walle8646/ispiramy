"""Chi c'era già prima dell'obbligo di conferma email deve poter entrare.

La colonna confirmed è nata a 0 anche per gli account vecchi: il login
rispondeva «Conferma prima la tua email» e il reset della password non
sbloccava niente. Un codice arrivato sulla casella, invece, la dimostra.
"""
import hashlib
import secrets
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.database import conferma_account_precedenti, engine
from app.models import Notification, NotificationType, User
from app.routes import auth
from app.utils.lingue_ui import COOKIE
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


@pytest.fixture
def http(csrf_client):
    reset_rate_limit()
    yield csrf_client
    csrf_client.get("/logout")
    _lingua(csrf_client, "it")
    reset_rate_limit()


class TestAccountPrecedenti:
    def test_senza_codice_in_sospeso_diventa_confermato_e_puo_entrare(self, http):
        vecchio, email = _utente()
        recente, _ = _utente(confirmation_code="123456")
        google, _ = _utente(confirmation_code="999999", google_id=f"sub-{secrets.token_hex(4)}")
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
        uid, email = _utente(password_md5=md5)
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
