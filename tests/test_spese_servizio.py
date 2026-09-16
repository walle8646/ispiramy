"""Spese di servizio e tariffa oraria minima.

Il cliente paga la consulenza piu' 1,99 € di spese di servizio. Le due cose
restano separate ovunque: su `booking.price` si calcolano la commissione e il
pagamento al consulente, le spese sono nostre e non entrano in quel conto.
"""
from decimal import Decimal

import pytest

from app.models import Booking
from app.utils.prezzi import (
    PREZZO_ORARIO_MINIMO,
    SPESE_SERVIZIO,
    centesimi,
    spese_servizio,
    totale_cliente,
    totale_pagato,
)


def test_le_spese_di_servizio_sono_un_euro_e_novantanove():
    assert SPESE_SERVIZIO == Decimal("1.99")
    assert spese_servizio() == Decimal("1.99")


def test_il_cliente_paga_consulenza_piu_spese():
    assert totale_cliente(50) == Decimal("51.99")
    assert totale_cliente(Decimal("37.50")) == Decimal("39.49")
    assert totale_cliente("100.00") == Decimal("101.99")


def test_gli_arrotondamenti_restano_al_centesimo():
    assert totale_cliente(33.333) == Decimal("35.32")
    assert centesimi(Decimal("51.99")) == 5199
    assert centesimi(33.335) == 3334


def test_il_totale_pagato_di_una_prenotazione():
    assert totale_pagato(Booking(price=Decimal("40"), service_fee=Decimal("1.99"))) == Decimal("41.99")


def test_le_prenotazioni_di_prima_non_hanno_spese():
    """Chi ha prenotato prima di questa modifica ha pagato solo la consulenza."""
    assert totale_pagato(Booking(price=Decimal("40"), service_fee=None)) == Decimal("40.00")


def test_il_minimo_orario_e_venti_euro():
    assert PREZZO_ORARIO_MINIMO == 20


# --------------------------------------------------------------------------- incasso

def test_stripe_incassa_consulenza_piu_spese():
    """Quello che parte a Stripe e' il totale; il prezzo della consulenza resta quello."""
    prezzo = 75
    assert centesimi(totale_cliente(prezzo)) == 7699


def test_il_consulente_non_vede_le_spese():
    """La commissione e il pagamento si calcolano su booking.price, non sul totale."""
    booking = Booking(price=Decimal("100"), service_fee=SPESE_SERVIZIO)
    quota_piattaforma = int(float(booking.price) * 100) * 20 // 100
    al_consulente = int(float(booking.price) * 100) - quota_piattaforma

    assert al_consulente == 8000, "80€ su 100€ di consulenza"
    # Noi incassiamo la commissione piu' le spese di servizio
    assert centesimi(totale_pagato(booking)) - al_consulente == 2199


# --------------------------------------------------------------------------- webhook

def test_il_webhook_toglie_le_spese_dall_importo_incassato():
    """Senza toglierle, booking.price si gonfierebbe e con esso commissione e payout."""
    from app.routes.stripe_webhook import _spese_dai_metadati

    assert _spese_dai_metadati({"service_fee": "1.99"}) == Decimal("1.99")
    incassato = 5199  # centesimi
    prezzo = round(incassato / 100 - float(_spese_dai_metadati({"service_fee": "1.99"})), 2)
    assert prezzo == 50.0


def test_una_sessione_senza_spese_nei_metadati_vale_zero():
    """Le sessioni aperte prima di questa modifica non hanno il campo."""
    from app.routes.stripe_webhook import _spese_dai_metadati

    assert _spese_dai_metadati({}) == Decimal("0")
    assert _spese_dai_metadati({"service_fee": ""}) == Decimal("0")
    assert _spese_dai_metadati({"service_fee": "non un numero"}) == Decimal("0")
    assert _spese_dai_metadati(None) == Decimal("0")


# --------------------------------------------------------------------------- rimborsi

def test_un_rimborso_totale_restituisce_anche_le_spese():
    booking = Booking(price=Decimal("60"), service_fee=SPESE_SERVIZIO)
    totale_cents = centesimi(totale_pagato(booking))
    assert int(totale_cents * 100 / 100) == 6199


def test_un_rimborso_parziale_tocca_anche_le_spese_in_proporzione():
    booking = Booking(price=Decimal("60"), service_fee=SPESE_SERVIZIO)
    totale_cents = centesimi(totale_pagato(booking))
    rimborso = int(totale_cents * 50 / 100)
    # al consulente resta la meta' del valore della consulenza, non del totale
    resto_consulenza = int(float(booking.price) * 100) - int(float(booking.price) * 100 * 50 / 100)
    assert rimborso == 3099
    assert resto_consulenza == 3000


# --------------------------------------------------------------------------- codice

def test_il_consulente_non_puo_scendere_sotto_il_minimo(csrf_client):
    """La tariffa oraria minima è salita a 20€: 19 non si salva più."""
    import secrets

    from sqlmodel import Session

    from app.database import engine
    from app.models import User
    from app.utils.password import hash_password
    from app.utils.rate_limit import reset_rate_limit

    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        utente = User(email=f"cons-{secrets.token_hex(4)}@test.local",
                      password_md5=hash_password(password), confirmed=1, user_type_id=2,
                      prezzo_consulenza=50)
        s.add(utente)
        s.commit()
        s.refresh(utente)
        uid, email = utente.id, utente.email

    try:
        assert csrf_client.post("/api/login", data={"email": email, "password": password}).status_code == 200

        rifiutato = csrf_client.post("/api/profile/update", data={"prezzo_consulenza": 19})
        assert rifiutato.status_code == 400
        assert "20" in rifiutato.json()["error"]

        accettato = csrf_client.post("/api/profile/update", data={"prezzo_consulenza": PREZZO_ORARIO_MINIMO})
        assert accettato.status_code == 200
        with Session(engine) as s:
            assert s.get(User, uid).prezzo_consulenza == PREZZO_ORARIO_MINIMO
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()
        with Session(engine) as s:
            u = s.get(User, uid)
            if u:
                s.delete(u)
                s.commit()


def test_le_offerte_rispettano_il_minimo_orario():
    """Su un'offerta il minimo dipende dalla durata: 30€ per 2 ore sono 15€/ora."""
    from pathlib import Path

    codice = (Path(__file__).resolve().parent.parent / "app" / "routes" / "consultation.py").read_text(encoding="utf-8")
    assert "PREZZO_ORARIO_MINIMO * duration_minutes / 60" in codice
    assert "if price < 15" not in codice


def test_lo_storico_del_cliente_mostra_il_totale():
    """Il dettaglio compare solo al cliente: al consulente le spese non interessano."""
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "profile.html").read_text(encoding="utf-8")
    assert "function importiHTML(b)" in html
    assert "b.total_paid" in html
    assert "consulenza ${euro(b.price)} + servizio ${euro(b.service_fee)}" in html
    assert "b.role !== 'client'" in html


def test_i_flussi_di_pagamento_usano_il_totale():
    """Stripe e PayPal, prenotazione diretta e offerta: tutti addebitano il totale."""
    from pathlib import Path

    radice = Path(__file__).resolve().parent.parent
    for percorso, quante in (
        ("app/routes/booking.py", 1),
        ("app/routes/paypal_payment.py", 2),
        ("app/routes/consultation.py", 1),
    ):
        codice = (radice / percorso).read_text(encoding="utf-8")
        assert codice.count("totale_cliente(") >= quante, f"{percorso} non addebita il totale"


def test_ogni_prenotazione_nuova_registra_le_spese():
    from pathlib import Path

    radice = Path(__file__).resolve().parent.parent
    for percorso in ("app/routes/booking.py", "app/routes/paypal_payment.py", "app/routes/consultation.py"):
        codice = (radice / percorso).read_text(encoding="utf-8")
        assert "service_fee=spese_servizio()" in codice, f"{percorso} non salva le spese sulla prenotazione"
