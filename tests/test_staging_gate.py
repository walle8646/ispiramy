"""Il lucchetto dello staging e l'app installabile.

Lo staging sta dietro utente e password. Il sistema operativo, pero', chiede
il manifest e l'icona per conto suo, senza credenziali: si prendeva un 401, e
su iPhone il risultato era un'icona vuota sulla schermata Home e un'app che
all'apertura non caricava niente.
"""
from app.main import (
    COOKIE_STAGING,
    STAGING_AUTH_EXEMPT,
    _lasciapassare,
    _lasciapassare_valido,
)


def test_i_file_dell_app_non_chiedono_la_password():
    """Manifest, service worker e icone non sono segreti: sono il logo e
    quattro righe di configurazione, e senza di loro l'app non si installa."""
    for percorso in ("/manifest.webmanifest", "/sw.js", "/static/icone/", "/senza-rete"):
        assert percorso in STAGING_AUTH_EXEMPT, f"{percorso} resta dietro il lucchetto"


def test_i_webhook_restano_esenti():
    """Stripe non sa fare Basic Auth: se lo chiudessimo fuori, i pagamenti
    resterebbero in sospeso senza che nessuno se ne accorga."""
    assert "/api/stripe/webhook" in STAGING_AUTH_EXEMPT


def test_il_lasciapassare_vale_solo_se_e_nostro():
    """L'app installata su iPhone ha una memoria separata da Safari: senza
    ricordo, la password la chiedeva a ogni avvio."""
    assert _lasciapassare_valido(_lasciapassare()) is True
    assert _lasciapassare_valido("") is False
    assert _lasciapassare_valido("finto") is False
    # un cookie firmato con un'altra chiave non deve passare
    assert _lasciapassare_valido(_lasciapassare()[:-3] + "xyz") is False


def test_il_gate_guarda_anche_il_cookie():
    import io
    import os

    sorgente = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "app", "main.py"), encoding="utf-8").read()
    gate = sorgente[sorgente.index("class StagingAuthMiddleware"):]
    gate = gate[:gate.index("# Middleware (Starlette")]
    assert f'request.cookies.get({COOKIE_STAGING}' in gate or "COOKIE_STAGING" in gate
    assert "set_cookie" in gate
    # il cookie viaggia solo in https e non si legge da JavaScript
    assert "httponly=True" in gate and "secure=True" in gate
