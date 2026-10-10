"""Dal pulsante Segnala fino alla lista admin, anche su un database vecchio.

Il database dei test nasce vuoto e le tabelle le crea l'avvio: lì la
segnalazione si vedeva già. Lo staging invece aveva già le tabelle di prima,
senza community_reports. Qui si rifà quel database, si riavvia, e si controlla
che la stessa richiesta del pulsante compaia per chi verifica e per chi
amministra, con il numero nel menu.
"""
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

# Eseguito anche come script, senza conftest: un .env non deve cambiare database.
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import CommunityQuestion, CommunityReport, Notification, User
from app.utils.password import hash_password
from tests.test_matrice_call_contestazioni import _login

ROOT = Path(__file__).resolve().parents[1]
MOTIVO = "Questa domanda contiene un insulto e non riguarda la categoria scelta."


def _badge(html):
    """Il numero accanto alla voce Segnalazioni del menu admin. None se manca."""
    trovato = re.search(
        r'href="/admin/reports"[^>]*>.*?class="nav-badge">\s*(\d+)\s*<',
        html,
        re.S,
    )
    return int(trovato.group(1)) if trovato else None


def _prepara(session, password):
    def utente(ruolo, tipo):
        persona = User(
            email=f"arrivo-{ruolo}-{secrets.token_hex(4)}@test.local",
            password_md5=hash_password(password),
            confirmed=1,
            nome=ruolo.capitalize(),
            cognome="Prova",
            user_type_id=tipo,
        )
        session.add(persona)
        return persona

    normale = utente("normale", 1)
    verificatore = utente("verificatore", 2)
    amministratore = utente("amministratore", 3)
    session.commit()
    for persona in (normale, verificatore, amministratore):
        session.refresh(persona)
    domanda = CommunityQuestion(
        user_id=amministratore.id,
        title=f"Domanda da segnalare {secrets.token_hex(3)}",
        description="Testo abbastanza lungo da essere una domanda vera della community.",
        validation=True,
    )
    session.add(domanda)
    session.commit()
    session.refresh(domanda)
    return {
        "password": password,
        "normale": normale.email,
        "verificatore": verificatore.email,
        "amministratore": amministratore.email,
        "ids": [normale.id, verificatore.id, amministratore.id],
        "domanda": domanda.id,
        "titolo": domanda.title,
    }


def _pulisci(dati):
    with Session(engine) as session:
        for riga in session.exec(select(CommunityReport).where(
            CommunityReport.question_id == dati["domanda"]
        )).all():
            session.delete(riga)
        session.commit()
        domanda = session.get(CommunityQuestion, dati["domanda"])
        if domanda:
            session.delete(domanda)
        for avviso in session.exec(select(Notification).where(Notification.user_id.in_(dati["ids"]))).all():
            session.delete(avviso)
        session.commit()
        for uid in dati["ids"]:
            persona = session.get(User, uid)
            if persona:
                session.delete(persona)
        session.commit()


def _la_segnalazione_e_nel_menu(http, dati):
    """La richiesta che manda il pulsante Segnala, poi la lista e il badge."""
    _login(http, dati["normale"], dati["password"])
    pagina = http.get("/community")
    assert pagina.status_code == 200
    assert 'class="btn-report"' in pagina.text
    assert f"apriSegnalazione({dati['domanda']})" in pagina.text

    inviata = http.post(
        f"/api/community/{dati['domanda']}/report",
        json={"reason": MOTIVO},
    )
    assert inviata.status_code == 201, inviata.text

    # chi ha segnalato non è admin: la lista non è la sua
    assert http.get("/admin/reports", follow_redirects=False).status_code == 302

    for ruolo in ("verificatore", "amministratore"):
        _login(http, dati[ruolo], dati["password"])
        lista = http.get("/admin/reports")
        assert lista.status_code == 200, ruolo
        assert MOTIVO in lista.text, ruolo
        assert dati["titolo"] in lista.text, ruolo
        assert _badge(lista.text) == 1, ruolo
        pannello = http.get("/admin/")
        assert pannello.status_code == 200, ruolo
        assert 'href="/admin/reports"' in pannello.text, ruolo
        assert _badge(pannello.text) == 1, ruolo


def test_sul_database_nuovo_la_segnalazione_compare_nel_menu(csrf_client):
    password = secrets.token_urlsafe(12)
    with Session(engine) as session:
        dati = _prepara(session, password)
    try:
        _la_segnalazione_e_nel_menu(csrf_client, dati)
    finally:
        _pulisci(dati)


def test_su_un_database_vecchio_la_segnalazione_compare_nel_menu(tmp_path):
    """Come lo staging: tabelle già presenti, senza la tabella e la colonna nuove."""
    database = tmp_path / "vecchio.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{database}"
    env["SESSION_SECRET"] = "test-session-secret"
    env["JWT_SECRET"] = "test-jwt-secret"
    env.pop("STAGING_PASSWORD", None)
    env["PYTHONPATH"] = str(ROOT)
    # un .env locale non deve dirottare questo processo sul database vero
    env["DOTENV_DISABLED"] = "1"
    processo = subprocess.run(
        [sys.executable, str(Path(__file__)), "--vecchio"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert processo.returncode == 0, processo.stdout + "\n" + processo.stderr


def _vecchio():
    from sqlalchemy import inspect, text
    from fastapi.testclient import TestClient

    from app.database import create_db_and_tables, engine
    from app.main import app

    create_db_and_tables()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS community_reports"))
        conn.execute(text('ALTER TABLE "user" DROP COLUMN ui_language'))
    assert "community_reports" not in set(inspect(engine).get_table_names())

    # è quello che fa l'avvio dell'app su un database già esistente
    create_db_and_tables()
    assert "community_reports" in set(inspect(engine).get_table_names())
    colonne = {c["name"] for c in inspect(engine).get_columns("user")}
    assert "ui_language" in colonne

    with TestClient(app) as http:
        http.get("/login")
        password = secrets.token_urlsafe(12)
        with Session(engine) as session:
            dati = _prepara(session, password)
        _la_segnalazione_e_nel_menu(http, dati)


if __name__ == "__main__" and "--vecchio" in sys.argv:
    _vecchio()
