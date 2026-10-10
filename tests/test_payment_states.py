"""Regressioni sugli stati di prenotazione e pagamento.

Due bug distinti, entrambi silenziosi:
- lo slot non veniva riservato durante il checkout, quindi due clienti potevano
  pagare lo stesso orario;
- gli elenchi filtravano su payment_status 'paid', che da quando il denaro
  resta trattenuto non viene praticamente mai usato: le consulenze
  sparivano dallo storico appena il pagamento veniva rilasciato.
"""
import secrets
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, User
from app.routes.booking import (
    BLOCKING_BOOKING_STATUSES,
    PAID_PAYMENT_STATUSES,
    now_italy_naive,
)
from app.scheduler import release_expired_pending_payments


class TestCostantiStati:
    def test_pending_payment_occupa_lo_slot(self):
        assert "pending_payment" in BLOCKING_BOOKING_STATUSES
        assert "confirmed" in BLOCKING_BOOKING_STATUSES
        assert "cancelled" not in BLOCKING_BOOKING_STATUSES

    def test_released_conta_come_pagato(self):
        # e' lo stato dopo il transfer al consulente: la consulenza deve
        # restare visibile nello storico
        assert "released" in PAID_PAYMENT_STATUSES
        assert "held" in PAID_PAYMENT_STATUSES
        assert "pending" not in PAID_PAYMENT_STATUSES


@pytest.fixture
def utenti():
    with Session(engine) as s:
        a = User(email=f"a-{secrets.token_hex(4)}@test.local", password_md5="x")
        b = User(email=f"b-{secrets.token_hex(4)}@test.local", password_md5="x")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        ids = (a.id, b.id)
    yield ids
    with Session(engine) as s:
        for bk in s.exec(select(Booking).where(Booking.client_user_id == ids[0])).all():
            s.delete(bk)
        for uid in ids:
            u = s.get(User, uid)
            if u:
                s.delete(u)
        s.commit()


def _crea_pending(client_id, consultant_id, creato_da_minuti):
    quando = now_italy_naive() + timedelta(days=3)
    with Session(engine) as s:
        b = Booking(
            client_user_id=client_id,
            consultant_user_id=consultant_id,
            booking_date=datetime(quando.year, quando.month, quando.day),
            start_time="10:00",
            end_time="11:00",
            duration_minutes=60,
            status="pending_payment",
            payment_status="pending",
            created_at=datetime.utcnow() - timedelta(minutes=creato_da_minuti),
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id


class TestPuliziaCheckoutAbbandonati:
    def test_slot_vecchio_viene_liberato(self, utenti):
        cliente, consulente = utenti
        booking_id = _crea_pending(cliente, consulente, creato_da_minuti=60)

        release_expired_pending_payments()

        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            assert b.status == "cancelled"
            assert b.cancellation_reason == "Pagamento non completato"

    def test_checkout_appena_avviato_resta_valido(self, utenti):
        cliente, consulente = utenti
        booking_id = _crea_pending(cliente, consulente, creato_da_minuti=2)

        release_expired_pending_payments()

        with Session(engine) as s:
            # 2 minuti fa: l'utente potrebbe essere ancora sulla pagina di Stripe
            assert s.get(Booking, booking_id).status == "pending_payment"
