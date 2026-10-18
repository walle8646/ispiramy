"""Richieste di consulenza che il consulente deve accettare o rifiutare.

Con la conferma automatica spenta il pagamento del cliente è solo autorizzato:
si incassa quando il consulente accetta, e se rifiuta o non risponde il blocco
si annulla (nessun rimborso, nessuna commissione Stripe persa). Stripe e
PayPal sono sostituiti da finti: qui si verifica la logica, non le API.
"""
import asyncio
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.database import engine, ensure_check_constraints
from app.models import Booking, Notification, User
from app.routes import booking as booking_routes
from app.routes import paypal_payment
from app.routes.stripe_webhook import handle_direct_booking
from app.utils import booking_requests
from app.utils.booking_requests import scadenza_risposta, scadi_richieste_senza_risposta
from app.utils.notification_types import ensure_notification_types
from app.utils.orari import now_italy_naive
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit


class StripeFinto:
    """Registra le chiamate a PaymentIntent.capture / cancel."""

    def __init__(self, capture_ok=True, cancel_ok=True):
        self.catturati, self.annullati = [], []
        finto = self

        class PaymentIntent:
            @staticmethod
            def capture(pi):
                finto.catturati.append(pi)
                if not capture_ok:
                    raise RuntimeError("authorization expired")
                return SimpleNamespace(status="succeeded")

            @staticmethod
            def cancel(pi):
                finto.annullati.append(pi)
                if not cancel_ok:
                    raise RuntimeError("network down")
                return SimpleNamespace(status="canceled")

        self.PaymentIntent = PaymentIntent


# ---------------------------------------------------------------- fixture

@pytest.fixture
def persone():
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        cliente = User(email=f"req-c-{secrets.token_hex(4)}@test.local", password_md5=hash_password(password),
                       confirmed=1, nome="Mario", cognome="Rossi")
        consulente = User(email=f"req-p-{secrets.token_hex(4)}@test.local", password_md5=hash_password(password),
                          confirmed=1, nome="Anna", cognome="Verdi", prezzo_consulenza=60,
                          stripe_account_id="acct_test", stripe_onboarding_complete=True,
                          auto_accept_bookings=False)
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
        for b in s.exec(select(Booking).where(Booking.consultant_user_id == dati.consulente)).all():
            s.delete(b)
        for n in s.exec(select(Notification).where(Notification.user_id.in_([dati.cliente, dati.consulente]))).all():
            s.delete(n)
        s.commit()
        for uid in (dati.cliente, dati.consulente):
            u = s.get(User, uid)
            if u:
                s.delete(u)
        s.commit()


def _richiesta(persone, *, giorni=2, stato="awaiting_acceptance", pagamento="authorized",
               scadenza=None, metodo="stripe"):
    inizio = (now_italy_naive() + timedelta(days=giorni)).replace(hour=10, minute=0, second=0, microsecond=0)
    with Session(engine) as s:
        b = Booking(
            client_user_id=persone.cliente, consultant_user_id=persone.consulente,
            booking_date=inizio, start_time="10:00", end_time="11:00", duration_minutes=60,
            price=60, status=stato, payment_status=pagamento, payment_method=metodo,
            stripe_payment_intent_id="pi_test" if metodo == "stripe" else None,
            paypal_order_id="ORDER-1" if metodo == "paypal" else None,
            paypal_authorization_id="AUTH-1" if metodo == "paypal" and pagamento == "authorized" else None,
            acceptance_deadline=scadenza or scadenza_risposta(inizio),
            description="Devo rivedere un contratto d'affitto",
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id


def _booking(booking_id):
    with Session(engine) as s:
        return s.get(Booking, booking_id)


def _notifiche(user_id, tipo):
    with Session(engine) as s:
        return s.exec(select(Notification).where(Notification.user_id == user_id, Notification.type == tipo)).all()


def _login(client, email, password):
    client.get("/logout")
    reset_rate_limit()
    # Il logout azzera la sessione, e con lei il token CSRF: va rinegoziato
    marker = 'name="csrf-token" content="'
    pagina = client.get("/login").text
    if marker in pagina:
        client.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    r = client.post("/api/login", data={"email": email, "password": password})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- regole

class TestScadenza:
    def test_prenotazione_lontana_24_ore(self):
        adesso = datetime(2026, 9, 11, 9, 0)
        assert scadenza_risposta(datetime(2026, 9, 20, 10, 0), adesso) == datetime(2026, 9, 12, 9, 0)

    def test_prenotazione_vicina_un_ora_prima_dell_inizio(self):
        """Il consulente ha tempo al massimo fino a un'ora prima dell'inizio:
        dopo, la richiesta viene rifiutata da sola."""
        adesso = datetime(2026, 9, 11, 9, 0)
        assert scadenza_risposta(datetime(2026, 9, 11, 15, 0), adesso) == datetime(2026, 9, 11, 14, 0)

    def test_prenotazione_col_preavviso_minimo(self):
        """Prenotata con due ore di preavviso: al consulente resta un'ora."""
        adesso = datetime(2026, 9, 11, 9, 0)
        assert scadenza_risposta(datetime(2026, 9, 11, 11, 0), adesso) == datetime(2026, 9, 11, 10, 0)

    def test_mai_meno_di_mezz_ora_e_mai_dopo_l_inizio(self):
        adesso = datetime(2026, 9, 11, 9, 0)
        assert scadenza_risposta(datetime(2026, 9, 11, 9, 45), adesso) == datetime(2026, 9, 11, 9, 30)
        assert scadenza_risposta(datetime(2026, 9, 11, 9, 20), adesso) == datetime(2026, 9, 11, 9, 20)

    def test_la_richiesta_occupa_lo_slot(self):
        assert "awaiting_acceptance" in booking_routes.BLOCKING_BOOKING_STATUSES

    def test_chi_richiede_accettazione(self):
        assert booking_requests.richiede_accettazione(User(email="x", password_md5="x", auto_accept_bookings=False))
        assert not booking_requests.richiede_accettazione(User(email="x", password_md5="x"))
        assert not booking_requests.richiede_accettazione(None)


class TestSchema:
    def test_vincoli_solo_su_postgres(self):
        assert ensure_check_constraints() == []  # i test girano su SQLite

    def test_tipi_notifica_idempotenti(self):
        assert ensure_notification_types() == []  # già inseriti all'avvio


# ---------------------------------------------------------------- creazione e pagamento

class TestCreazione:
    def test_stripe_blocca_senza_incassare(self, persone, csrf_client, monkeypatch):
        chiamate = {}

        def checkout_finto(**kwargs):
            chiamate.update(kwargs)
            return SimpleNamespace(id=f"cs_{secrets.token_hex(4)}", url="https://checkout.test")

        monkeypatch.setattr(booking_routes, "create_checkout_session", checkout_finto)
        _login(csrf_client, persone.email_cliente, persone.password)
        giorno = (now_italy_naive() + timedelta(days=3)).strftime("%Y-%m-%d")
        r = csrf_client.post("/api/booking/create", json={
            "consultant_user_id": persone.consulente, "booking_date": giorno,
            "start_time": "10:00", "end_time": "11:00", "duration_minutes": 60,
            "price": 60, "description": "Contratto d'affitto",
        })
        assert r.status_code == 200, r.text
        assert chiamate["capture_manual"] is True
        assert chiamate["metadata"]["requires_acceptance"] == "true"

        with Session(engine) as s:
            b = s.exec(select(Booking).where(Booking.consultant_user_id == persone.consulente)).one()
        assert b.status == "pending_payment"
        assert b.acceptance_deadline is not None

    def test_consulente_con_conferma_automatica_incassa_subito(self, persone, csrf_client, monkeypatch):
        with Session(engine) as s:
            u = s.get(User, persone.consulente)
            u.auto_accept_bookings = True
            s.add(u)
            s.commit()
        chiamate = {}
        monkeypatch.setattr(booking_routes, "create_checkout_session",
                            lambda **kw: chiamate.update(kw) or SimpleNamespace(id="cs_auto", url="https://x"))
        _login(csrf_client, persone.email_cliente, persone.password)
        giorno = (now_italy_naive() + timedelta(days=3)).strftime("%Y-%m-%d")
        r = csrf_client.post("/api/booking/create", json={
            "consultant_user_id": persone.consulente, "booking_date": giorno,
            "start_time": "11:00", "end_time": "12:00", "duration_minutes": 60,
            "price": 60, "description": "Contratto",
        })
        assert r.status_code == 200, r.text
        assert chiamate["capture_manual"] is False

    def test_webhook_mette_in_attesa_e_avvisa_il_consulente(self, persone):
        booking_id = _richiesta(persone, stato="pending_payment", pagamento="pending")
        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            b.stripe_checkout_session_id = "cs_attesa"
            b.stripe_payment_intent_id = None
            s.add(b)
            s.commit()

        asyncio.run(handle_direct_booking("cs_attesa", "pi_attesa", {
            "client_user_id": str(persone.cliente), "consultant_user_id": str(persone.consulente),
            "booking_date": "2026-01-01", "start_time": "10:00", "end_time": "11:00",
            "duration_minutes": "60", "booking_id": str(booking_id), "recording_requested": "true",
        }, 6000))

        b = _booking(booking_id)
        assert (b.status, b.payment_status) == ("awaiting_acceptance", "authorized")
        assert b.stripe_payment_intent_id == "pi_attesa"
        assert b.acceptance_deadline > now_italy_naive()
        notifiche = _notifiche(persone.consulente, "booking_request")
        assert len(notifiche) == 1 and "Accetta o rifiuta" in notifiche[0].message

    def test_paypal_autorizza_invece_di_incassare(self, persone, client, monkeypatch):
        booking_id = _richiesta(persone, stato="pending_payment", pagamento="pending", metodo="paypal")
        monkeypatch.setattr(paypal_payment, "authorize_order", lambda order_id: {
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"authorizations": [
                {"id": "AUTH-OK", "status": "CREATED", "amount": {"value": "60.00"}}]}}],
        })
        monkeypatch.setattr(paypal_payment, "capture_order",
                            lambda order_id: pytest.fail("con la conferma a mano non si incassa subito"))

        r = client.get(f"/booking/paypal/capture?booking_id={booking_id}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/profile"
        b = _booking(booking_id)
        assert (b.status, b.payment_status, b.paypal_authorization_id) == ("awaiting_acceptance", "authorized", "AUTH-OK")

    def test_paypal_importo_diverso_non_conferma(self, persone, client, monkeypatch):
        booking_id = _richiesta(persone, stato="pending_payment", pagamento="pending", metodo="paypal")
        monkeypatch.setattr(paypal_payment, "authorize_order", lambda order_id: {
            "status": "COMPLETED",
            "purchase_units": [{"payments": {"authorizations": [{"id": "A", "amount": {"value": "1.00"}}]}}],
        })
        r = client.get(f"/booking/paypal/capture?booking_id={booking_id}", follow_redirects=False)
        assert "importo_non_corrispondente" in r.headers["location"]
        assert _booking(booking_id).status == "pending_payment"


# ---------------------------------------------------------------- risposta del consulente

class TestAccetta:
    def test_accetta_incassa_e_conferma(self, persone, csrf_client, monkeypatch):
        stripe = StripeFinto()
        monkeypatch.setattr(booking_requests, "_stripe", lambda: stripe)
        booking_id = _richiesta(persone)
        _login(csrf_client, persone.email_consulente, persone.password)

        r = csrf_client.post(f"/api/booking/{booking_id}/accept")
        assert r.status_code == 200, r.text
        b = _booking(booking_id)
        assert (b.status, b.payment_status) == ("confirmed", "held")
        assert stripe.catturati == ["pi_test"]
        assert len(_notifiche(persone.cliente, "booking_accepted")) == 1

        # doppio click: nessun secondo incasso
        assert csrf_client.post(f"/api/booking/{booking_id}/accept").status_code == 200
        assert stripe.catturati == ["pi_test"]

    def test_paypal_incassa_l_autorizzazione(self, persone, csrf_client, monkeypatch):
        from app.utils import paypal_config
        monkeypatch.setattr(paypal_config, "capture_authorization",
                            lambda auth_id: {"id": f"CAP-{auth_id}", "status": "COMPLETED"})
        booking_id = _richiesta(persone, metodo="paypal")
        _login(csrf_client, persone.email_consulente, persone.password)

        assert csrf_client.post(f"/api/booking/{booking_id}/accept").status_code == 200
        b = _booking(booking_id)
        assert (b.status, b.payment_status, b.paypal_capture_id) == ("confirmed", "held", "CAP-AUTH-1")

    def test_solo_il_consulente(self, persone, csrf_client, monkeypatch):
        monkeypatch.setattr(booking_requests, "_stripe", lambda: StripeFinto())
        booking_id = _richiesta(persone)
        _login(csrf_client, persone.email_cliente, persone.password)
        assert csrf_client.post(f"/api/booking/{booking_id}/accept").status_code == 403
        assert _booking(booking_id).status == "awaiting_acceptance"

    def test_scaduta_non_si_accetta(self, persone, csrf_client, monkeypatch):
        stripe = StripeFinto()
        monkeypatch.setattr(booking_requests, "_stripe", lambda: stripe)
        booking_id = _richiesta(persone, scadenza=now_italy_naive() - timedelta(minutes=1))
        _login(csrf_client, persone.email_consulente, persone.password)
        r = csrf_client.post(f"/api/booking/{booking_id}/accept")
        assert r.status_code == 400 and "scaduta" in r.json()["detail"]
        assert stripe.catturati == []

    def test_incasso_fallito_lascia_la_richiesta_in_attesa(self, persone, csrf_client, monkeypatch):
        monkeypatch.setattr(booking_requests, "_stripe", lambda: StripeFinto(capture_ok=False))
        booking_id = _richiesta(persone)
        _login(csrf_client, persone.email_consulente, persone.password)
        r = csrf_client.post(f"/api/booking/{booking_id}/accept")
        assert r.status_code == 502
        b = _booking(booking_id)
        assert (b.status, b.payment_status) == ("awaiting_acceptance", "authorized")


class TestRifiuta:
    def test_rifiuto_annulla_il_blocco(self, persone, csrf_client, monkeypatch):
        stripe = StripeFinto()
        monkeypatch.setattr(booking_requests, "_stripe", lambda: stripe)
        # Anche a meno di 4 ore dall'inizio: la richiesta si rifiuta fino alla scadenza
        booking_id = _richiesta(persone, giorni=0)
        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            inizio = now_italy_naive() + timedelta(hours=3)
            b.booking_date = inizio.replace(hour=0, minute=0, second=0, microsecond=0)
            b.start_time = inizio.strftime("%H:%M")
            s.add(b)
            s.commit()
        _login(csrf_client, persone.email_consulente, persone.password)

        r = csrf_client.post(f"/api/booking/{booking_id}/refuse", json={"reason": "Non è il mio campo"})
        assert r.status_code == 200, r.text
        b = _booking(booking_id)
        assert (b.status, b.payment_status) == ("cancelled", "voided")
        assert stripe.annullati == ["pi_test"]
        assert len(_notifiche(persone.cliente, "booking_refused")) == 1


class TestScadenzaAutomatica:
    def test_senza_risposta_si_annulla_e_si_avvisa(self, persone, monkeypatch):
        stripe = StripeFinto()
        monkeypatch.setattr(booking_requests, "_stripe", lambda: stripe)
        scaduta = _richiesta(persone, scadenza=now_italy_naive() - timedelta(minutes=10))
        ancora_valida = _richiesta(persone, giorni=3)

        assert scadi_richieste_senza_risposta() >= 1
        b = _booking(scaduta)
        assert (b.status, b.payment_status) == ("cancelled", "voided")
        assert _booking(ancora_valida).status == "awaiting_acceptance"
        assert len(_notifiche(persone.cliente, "booking_request_expired")) == 1

    def test_appena_scaduta_aspetta_la_tolleranza(self, persone, monkeypatch):
        monkeypatch.setattr(booking_requests, "_stripe", lambda: StripeFinto())
        booking_id = _richiesta(persone, scadenza=now_italy_naive() - timedelta(seconds=30))
        scadi_richieste_senza_risposta()
        assert _booking(booking_id).status == "awaiting_acceptance"

    def test_se_il_blocco_non_si_annulla_riprova_dopo(self, persone, monkeypatch):
        monkeypatch.setattr(booking_requests, "_stripe", lambda: StripeFinto(cancel_ok=False))
        booking_id = _richiesta(persone, scadenza=now_italy_naive() - timedelta(minutes=10))
        scadi_richieste_senza_risposta()
        assert _booking(booking_id).status == "awaiting_acceptance"


# ---------------------------------------------------------------- profilo e call

class TestProfilo:
    def test_la_richiesta_sta_nel_suo_gruppo(self, persone, csrf_client):
        """Le richieste da confermare non occupano i posti degli appuntamenti:
        nel profilo stanno in un gruppo a parte, che si apre col "+"."""
        booking_id = _richiesta(persone)
        for email in (persone.email_consulente, persone.email_cliente):
            _login(csrf_client, email, persone.password)
            dati = csrf_client.get("/api/booking/upcoming").json()
            assert all(b["id"] != booking_id for b in dati["bookings"])
            card = next(r for r in dati["requests"] if r["id"] == booking_id)
            assert card["awaiting_acceptance"] is True
            assert card["acceptance_expired"] is False
            assert card["acceptance_deadline"].endswith(("+01:00", "+02:00"))

    def test_molte_richieste_non_scacciano_gli_appuntamenti(self, persone, csrf_client):
        confermato = _richiesta(persone, giorni=1, stato="confirmed", pagamento="held")
        for ora in ("09:00", "11:00", "12:00", "13:00"):
            b = _richiesta(persone, giorni=2)
            with Session(engine) as s:
                riga = s.get(Booking, b)
                riga.start_time, riga.end_time = ora, "23:59"
                s.add(riga)
                s.commit()

        _login(csrf_client, persone.email_consulente, persone.password)
        dati = csrf_client.get("/api/booking/upcoming").json()
        assert [b["id"] for b in dati["bookings"]] == [confermato]
        assert len(dati["requests"]) == 4

    def test_niente_call_prima_dell_accettazione(self, persone, csrf_client):
        booking_id = _richiesta(persone)
        _login(csrf_client, persone.email_cliente, persone.password)
        assert csrf_client.post(f"/api/booking/{booking_id}/join").status_code == 403
        r = csrf_client.get(f"/booking/call/{booking_id}", follow_redirects=False)
        assert r.status_code == 303

    def test_impostazione_conferma_automatica(self, persone, csrf_client):
        _login(csrf_client, persone.email_consulente, persone.password)
        assert csrf_client.post("/api/user/set-auto-accept", json={"auto_accept_bookings": True}).status_code == 200
        with Session(engine) as s:
            assert s.get(User, persone.consulente).auto_accept_bookings is True
        assert csrf_client.post("/api/user/set-auto-accept", json={"auto_accept_bookings": "no"}).status_code == 400

    def test_pagina_prenotazione_avvisa_il_cliente(self, persone, csrf_client):
        _login(csrf_client, persone.email_cliente, persone.password)
        r = csrf_client.get(f"/book/{persone.consulente}")
        assert r.status_code == 200
        assert "conferma le prenotazioni manualmente" in r.text
        assert "Invia richiesta" in r.text


class TestPreavviso:
    """Due ore per prenotare, e al consulente resta tempo fino a un'ora prima."""

    def _prenota(self, csrf_client, persone, ore_da_ora):
        inizio = now_italy_naive() + timedelta(hours=ore_da_ora)
        return csrf_client.post("/api/booking/create", json={
            "consultant_user_id": persone.consulente,
            "booking_date": inizio.strftime("%Y-%m-%d"),
            "start_time": inizio.strftime("%H:%M"),
            "end_time": (inizio + timedelta(hours=1)).strftime("%H:%M"),
            "duration_minutes": 60, "price": 60, "description": "Serve aiuto",
        })

    def test_due_ore_e_mezza_prima_si_puo_prenotare(self, persone, csrf_client, monkeypatch):
        monkeypatch.setattr(booking_routes, "create_checkout_session",
                            lambda **kw: SimpleNamespace(id="cs_preavviso", url="https://checkout.test"))
        _login(csrf_client, persone.email_cliente, persone.password)
        assert self._prenota(csrf_client, persone, 2.5).status_code == 200

    def test_un_ora_prima_e_troppo_tardi(self, persone, csrf_client, monkeypatch):
        monkeypatch.setattr(booking_routes, "create_checkout_session",
                            lambda **kw: pytest.fail("non si deve arrivare al pagamento"))
        _login(csrf_client, persone.email_cliente, persone.password)
        r = self._prenota(csrf_client, persone, 1)
        assert r.status_code == 400
        assert "2 ore" in r.json()["detail"]

    def test_la_scadenza_cade_sempre_almeno_un_ora_prima(self, persone):
        inizio = now_italy_naive() + timedelta(hours=2, minutes=30)
        scadenza = scadenza_risposta(inizio)
        assert scadenza <= inizio - timedelta(hours=1)
        assert scadenza > now_italy_naive()
