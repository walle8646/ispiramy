"""I parametri con cui si apre il pagamento su Stripe.

Stripe ha smesso di accettare payment_method_types alla creazione della
sessione: i metodi di pagamento si scelgono dal Dashboard. Finche' il
parametro e' rimasto nel codice, ogni prenotazione moriva con "Errore nella
creazione del pagamento" prima ancora di arrivare alla pagina di Stripe.
"""
from types import SimpleNamespace

import pytest

from app.utils import stripe_config


class _SessioneFinta:
    """Prende il posto di stripe.checkout.Session e si segna i parametri."""

    def __init__(self):
        self.parametri = None

    def create(self, **kwargs):
        self.parametri = kwargs
        return SimpleNamespace(id="cs_prova", url="https://stripe.example/cs_prova")


@pytest.fixture
def stripe_finto(monkeypatch):
    sessione = _SessioneFinta()
    monkeypatch.setattr(stripe_config, "stripe",
                        SimpleNamespace(checkout=SimpleNamespace(Session=sessione)))
    monkeypatch.setattr(stripe_config, "STRIPE_SECRET_KEY", "sk_test_prova")
    return sessione


def _apri(**extra):
    return stripe_config.create_checkout_session(
        amount=6000, currency="eur",
        success_url="https://esempio.test/ok",
        cancel_url="https://esempio.test/no",
        metadata={"booking_id": "1"},
        **extra,
    )


def test_i_metodi_di_pagamento_non_si_passano_piu(stripe_finto):
    """Li decide il Dashboard: passarli qui fa fallire la chiamata."""
    _apri()
    assert "payment_method_types" not in stripe_finto.parametri


def test_la_sessione_ha_quello_che_serve(stripe_finto):
    _apri()
    parametri = stripe_finto.parametri
    assert parametri["mode"] == "payment"
    assert parametri["line_items"][0]["price_data"]["unit_amount"] == 6000
    assert parametri["line_items"][0]["price_data"]["currency"] == "eur"
    # lo slot resta occupato finche' il checkout e' aperto: non deve valere un giorno
    assert parametri["expires_at"] > 0


def test_il_consulente_che_conferma_a_mano_blocca_e_non_incassa(stripe_finto):
    """Con la conferma a mano l'importo si blocca soltanto: si incassa quando
    il consulente accetta."""
    _apri(capture_manual=True)
    assert stripe_finto.parametri["payment_intent_data"]["capture_method"] == "manual"


def test_la_commissione_e_il_destinatario_restano_al_loro_posto(stripe_finto):
    _apri(stripe_account_id="acct_prova", application_fee_amount=1200, capture_manual=True)
    dati = stripe_finto.parametri["payment_intent_data"]
    assert dati["application_fee_amount"] == 1200
    assert dati["transfer_data"] == {"destination": "acct_prova"}
    # e il blocco non si perde aggiungendo la commissione
    assert dati["capture_method"] == "manual"
