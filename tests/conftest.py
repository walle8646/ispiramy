"""Configurazione comune dei test.

Il DATABASE_URL viene puntato su uno SQLite temporaneo PRIMA di importare
qualsiasi modulo dell'app: `app.database` legge la variabile a import-time, e
senza questo i test scriverebbero sul database di sviluppo.
"""
import os
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.gettempdir()) / "ispiramy_pytest.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP_DB.as_posix()}")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
# STAGING_PASSWORD attiverebbe la Basic Auth su ogni richiesta
os.environ.pop("STAGING_PASSWORD", None)

# Il .env locale contiene le chiavi vere (Stripe, OpenAI, AWS, database...).
# Alcuni moduli chiamano load_dotenv() all'import, stripe_config perfino con
# override=True: nei test quei valori sovrascrivevano le impostazioni qui
# sopra, compreso DATABASE_URL, e facevano chiamare i servizi veri. In CI il
# .env non esiste; in locale si', quindi qui non si carica mai.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def schema_database():
    """Crea le tabelle una volta per sessione, prima di qualsiasi test.

    Prima lo faceva solo il fixture `client`, tramite l'evento di startup
    dell'app: i test che usano il database direttamente funzionavano solo se
    un'esecuzione precedente aveva lasciato il file sul disco, e fallivano
    partendo da zero o eseguiti da soli.
    """
    from app.database import create_db_and_tables

    create_db_and_tables()


@pytest.fixture(autouse=True)
def pulisci_notifiche_orfane():
    """Toglie le notifiche rimaste a utenti cancellati dai test.

    SQLite riusa gli id: una notifica lasciata a un utente cancellato finiva
    all'utente creato dal test successivo con lo stesso id. Da quando tutti i
    tipi di notifica sono inseriti all'avvio, ogni test che passa da
    send_notification ne lascia qualcuna.
    """
    yield
    from sqlalchemy import text
    from app.database import engine

    with engine.begin() as conn:
        conn.execute(text(
            'DELETE FROM notifications WHERE user_id NOT IN (SELECT id FROM "user")'
        ))


@pytest.fixture(scope="session")
def client():
    """TestClient con gli hook di startup/shutdown eseguiti (crea le tabelle)."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def csrf_client(client):
    """Client con token CSRF gia' negoziato, pronto per le richieste POST."""
    resp = client.get("/login")
    token = ""
    marker = 'name="csrf-token" content="'
    if marker in resp.text:
        token = resp.text.split(marker, 1)[1].split('"', 1)[0]
    client.headers.update({"X-CSRF-Token": token})
    return client
