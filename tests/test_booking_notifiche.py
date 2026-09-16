"""Avvisi e storico delle consulenze, e slot occupato per le offerte.

Quattro buchi trovati mappando il flusso dei pagamenti:
- il cliente non riceveva nessuna conferma della prenotazione;
- chi subiva un annullamento non veniva avvisato;
- le consulenze annullate sparivano dallo storico del profilo;
- pagando un'offerta non si controllava che l'orario fosse ancora libero.
"""
import asyncio
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, ConsultationOffer, Notification, User
from app.routes import consultation as consultation_routes
from app.routes.stripe_webhook import handle_consultation_offer_booking, handle_direct_booking
from app.utils.orari import now_italy_naive
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit


@pytest.fixture
def persone():
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        cliente = User(email=f"avv-c-{secrets.token_hex(4)}@test.local", password_md5=hash_password(password),
                       confirmed=1, nome="Giulia", cognome="Bianchi")
        consulente = User(email=f"avv-p-{secrets.token_hex(4)}@test.local", password_md5=hash_password(password),
                          confirmed=1, nome="Paolo", cognome="Neri", prezzo_consulenza=60,
                          stripe_account_id="acct_x", stripe_onboarding_complete=True)
        s.add(cliente)
        s.add(consulente)
        s.commit()
        s.refresh(cliente)
        s.refresh(consulente)
        dati = SimpleNamespace(cliente=cliente.id, consulente=consulente.id, password=password,
                               email_cliente=cliente.email, email_consulente=consulente.email)
    yield dati
    reset_rate_limit()
    with Session(engine) as s:
        for o in s.exec(select(ConsultationOffer).where(ConsultationOffer.client_user_id == dati.cliente)).all():
            o.booking_id = None
            s.add(o)
        s.commit()
        for b in s.exec(select(Booking).where(Booking.consultant_user_id == dati.consulente)).all():
            s.delete(b)
        for o in s.exec(select(ConsultationOffer).where(ConsultationOffer.client_user_id == dati.cliente)).all():
            s.delete(o)
        for n in s.exec(select(Notification).where(Notification.user_id.in_([dati.cliente, dati.consulente]))).all():
            s.delete(n)
        s.commit()
        for uid in (dati.cliente, dati.consulente):
            u = s.get(User, uid)
            if u:
                s.delete(u)
        s.commit()


def _prenotazione(persone, *, giorni=2, ora="10:00", fine="11:00", stato="confirmed",
                  pagamento="held", **extra):
    giorno = (now_italy_naive() + timedelta(days=giorni)).replace(hour=0, minute=0, second=0, microsecond=0)
    with Session(engine) as s:
        b = Booking(client_user_id=persone.cliente, consultant_user_id=persone.consulente,
                    booking_date=giorno, start_time=ora, end_time=fine, duration_minutes=60,
                    price=60, status=stato, payment_status=pagamento, payment_method="stripe",
                    stripe_payment_intent_id="pi_x", **extra)
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id


def _notifiche(user_id, tipo):
    with Session(engine) as s:
        return s.exec(select(Notification).where(Notification.user_id == user_id,
                                                 Notification.type == tipo)).all()


def _login(client, email, password):
    client.get("/logout")
    reset_rate_limit()
    marker = 'name="csrf-token" content="'
    pagina = client.get("/login").text
    if marker in pagina:
        client.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    assert client.post("/api/login", data={"email": email, "password": password}).status_code == 200


class TestConfermaAlCliente:
    def test_anche_il_cliente_riceve_la_conferma(self, persone):
        booking_id = _prenotazione(persone, stato="pending_payment", pagamento="pending")
        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            b.stripe_checkout_session_id = "cs_conf"
            s.add(b)
            s.commit()

        asyncio.run(handle_direct_booking("cs_conf", "pi_conf", {
            "client_user_id": str(persone.cliente), "consultant_user_id": str(persone.consulente),
            "booking_date": "2026-01-01", "start_time": "10:00", "end_time": "11:00",
            "duration_minutes": "60", "booking_id": str(booking_id), "recording_requested": "true",
        }, 6000))

        with Session(engine) as s:
            b = s.get(Booking, booking_id)
        assert (b.status, b.payment_status) == ("confirmed", "held")
        assert len(_notifiche(persone.consulente, "booking_confirmed")) == 1
        avvisi = _notifiche(persone.cliente, "booking_confirmed_client")
        assert len(avvisi) == 1 and "confermata" in avvisi[0].message


class TestAnnullamento:
    def test_il_consulente_viene_avvisato(self, persone, csrf_client, monkeypatch):
        # Niente rimborso vero: qui interessa l'avviso
        booking_id = _prenotazione(persone, giorni=3, pagamento="paid")
        monkeypatch.setattr("app.routes.booking.avvisa_annullamento",
                            __import__("app.utils.booking_requests", fromlist=["x"]).avvisa_annullamento)
        _login(csrf_client, persone.email_cliente, persone.password)

        r = csrf_client.delete(f"/api/booking/cancel/{booking_id}?reason=Imprevisto")
        assert r.status_code == 200, r.text
        avvisi = _notifiche(persone.consulente, "booking_cancelled")
        assert len(avvisi) == 1
        assert "Giulia" in avvisi[0].message
        assert not _notifiche(persone.cliente, "booking_cancelled")  # non a chi annulla


class TestStorico:
    def test_le_annullate_restano_nello_storico(self, persone, csrf_client):
        annullata = _prenotazione(persone, giorni=-1, pagamento="refunded", stato="cancelled",
                                  cancellation_reason="Imprevisto del consulente")
        with Session(engine) as s:
            b = s.get(Booking, annullata)
            b.cancelled_by = persone.consulente
            s.add(b)
            s.commit()
        svolta = _prenotazione(persone, giorni=-2, ora="15:00", fine="16:00", stato="completed")
        mai_pagata = _prenotazione(persone, giorni=-3, ora="18:00", fine="19:00",
                                   stato="cancelled", pagamento="pending")
        assenza = _prenotazione(persone, giorni=-4, ora="20:00", fine="21:00",
                                stato="no_show", pagamento="refunded")

        _login(csrf_client, persone.email_cliente, persone.password)
        storico = csrf_client.get("/api/booking/history").json()["bookings"]
        per_id = {b["id"]: b for b in storico}

        assert svolta in per_id
        assert assenza in per_id, "anche un'assenza rimborsata è successa"
        assert mai_pagata not in per_id, "un checkout mai completato non è una consulenza"
        voce = per_id[annullata]
        assert voce["status"] == "cancelled"
        assert voce["cancelled_by"] == "other"
        assert voce["payment_status"] == "refunded"
        assert voce["cancellation_reason"] == "Imprevisto del consulente"
        assert voce["can_review"] is False and voce["can_dispute"] is False

    def test_il_cliente_ritrova_quanto_ha_pagato(self, persone, csrf_client):
        """Nello storico serve il totale davvero pagato, spese di servizio comprese:
        è la cifra che il cliente ritrova sull'estratto conto."""
        from decimal import Decimal

        svolta = _prenotazione(persone, giorni=-2, stato="completed", pagamento="paid",
                               service_fee=Decimal("1.99"))
        _login(csrf_client, persone.email_cliente, persone.password)
        voce = next(b for b in csrf_client.get("/api/booking/history").json()["bookings"]
                    if b["id"] == svolta)

        assert voce["price"] == 60.0
        assert voce["service_fee"] == 1.99
        assert voce["total_paid"] == 61.99

    def test_le_consulenze_di_prima_non_mostrano_spese(self, persone, csrf_client):
        """Senza spese salvate il totale è il solo prezzo, non 60 + 0 arrotondato male."""
        vecchia = _prenotazione(persone, giorni=-5, ora="09:00", fine="10:00",
                                stato="completed", pagamento="paid")
        _login(csrf_client, persone.email_cliente, persone.password)
        voce = next(b for b in csrf_client.get("/api/booking/history").json()["bookings"]
                    if b["id"] == vecchia)

        assert voce["service_fee"] == 0
        assert voce["total_paid"] == 60.0

    def test_un_rimborso_si_vede_nello_storico(self, persone, csrf_client):
        from decimal import Decimal

        annullata = _prenotazione(persone, giorni=-6, ora="11:00", fine="12:00",
                                  stato="cancelled", pagamento="refunded",
                                  service_fee=Decimal("1.99"), refund_amount=Decimal("61.99"))
        _login(csrf_client, persone.email_cliente, persone.password)
        voce = next(b for b in csrf_client.get("/api/booking/history").json()["bookings"]
                    if b["id"] == annullata)

        assert voce["refund_amount"] == 61.99, "il rimborso comprende le spese di servizio"

    def test_una_richiesta_scaduta_si_vede_come_annullata(self, persone, csrf_client):
        scaduta = _prenotazione(persone, giorni=1, stato="cancelled", pagamento="voided",
                                cancellation_reason="Il consulente non ha risposto in tempo")
        _login(csrf_client, persone.email_cliente, persone.password)
        storico = csrf_client.get("/api/booking/history").json()["bookings"]
        voce = next(b for b in storico if b["id"] == scaduta)
        assert voce["cancelled_by"] == "system"
        assert voce["payment_status"] == "voided"


class TestOffertaSlotOccupato:
    @pytest.fixture
    def offerta(self, persone):
        with Session(engine) as s:
            o = ConsultationOffer(consultant_user_id=persone.consulente, client_user_id=persone.cliente,
                                  price=60, duration_minutes=60, status="pending",
                                  expires_at=datetime.utcnow() + timedelta(days=3))
            s.add(o)
            s.commit()
            s.refresh(o)
            return o.id

    def test_slot_gia_prenotato_blocca_il_pagamento(self, persone, csrf_client, offerta, monkeypatch):
        monkeypatch.setattr(consultation_routes, "create_checkout_session",
                            lambda **kw: pytest.fail("non si deve arrivare al pagamento"))
        giorno = (now_italy_naive() + timedelta(days=2)).strftime("%Y-%m-%d")
        _prenotazione(persone, giorni=2, ora="09:00", fine="10:00")
        _login(csrf_client, persone.email_cliente, persone.password)

        r = csrf_client.post(f"/consulenza/prenota/{offerta}/confirm", json={
            "date": giorno, "start_time": "09:00", "end_time": "10:00", "description": "Serve aiuto",
        })
        assert r.status_code == 409

    def test_lo_slot_viene_occupato_e_poi_confermato(self, persone, csrf_client, offerta, monkeypatch):
        monkeypatch.setattr(consultation_routes, "create_checkout_session",
                            lambda **kw: SimpleNamespace(id="cs_offerta", url="https://checkout.test"))
        giorno = (now_italy_naive() + timedelta(days=4)).strftime("%Y-%m-%d")
        _login(csrf_client, persone.email_cliente, persone.password)

        r = csrf_client.post(f"/consulenza/prenota/{offerta}/confirm", json={
            "date": giorno, "start_time": "09:00", "end_time": "10:00", "description": "Serve aiuto",
        })
        assert r.status_code == 200, r.text

        with Session(engine) as s:
            prenotate = s.exec(select(Booking).where(Booking.consultant_user_id == persone.consulente)).all()
        assert len(prenotate) == 1 and prenotate[0].status == "pending_payment"

        # un secondo cliente non può più prendere quell'orario
        r2 = csrf_client.post(f"/consulenza/prenota/{offerta}/confirm", json={
            "date": giorno, "start_time": "09:00", "end_time": "10:00", "description": "Serve aiuto",
        })
        assert r2.status_code == 409

        asyncio.run(handle_consultation_offer_booking("cs_offerta", "pi_offerta", {
            "offer_id": str(offerta), "client_user_id": str(persone.cliente),
            "consultant_user_id": str(persone.consulente), "selected_date": giorno,
            "start_time": "09:00", "end_time": "10:00", "duration_minutes": "60",
            "booking_id": str(prenotate[0].id), "recording_requested": "true",
        }))

        with Session(engine) as s:
            prenotate = s.exec(select(Booking).where(Booking.consultant_user_id == persone.consulente)).all()
            offerta_db = s.get(ConsultationOffer, offerta)
        assert len(prenotate) == 1, "il webhook non deve creare un doppione"
        assert (prenotate[0].status, prenotate[0].payment_status) == ("confirmed", "held")
        assert offerta_db.status == "accepted" and offerta_db.booking_id == prenotate[0].id
        assert len(_notifiche(persone.cliente, "booking_confirmed_client")) == 1
        assert len(_notifiche(persone.consulente, "booking_confirmed")) == 1


class TestStoricoOfferteNelProfilo:
    """Nella sezione "Richieste di consulenza" restano in vista solo quelle
    aperte: accettate, rifiutate e scadute stanno nello storico, chiuso."""

    def test_il_profilo_divide_aperte_e_storico(self):
        from pathlib import Path

        profilo = (Path(__file__).resolve().parent.parent / "app" / "templates" / "profile.html").read_text(encoding="utf-8-sig")
        assert "toggleStoricoOfferte" in profilo
        assert "storicoOfferteAperto = false" in profilo, "lo storico parte chiuso"
        assert "o.status === 'pending'" in profilo, "aperte = in attesa di risposta"
