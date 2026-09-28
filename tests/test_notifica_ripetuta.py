"""La stessa notifica non deve tornare a ogni riavvio.

Un consulente si e' ritrovato la campanella piena della stessa identica
notifica. Il motivo: il rilascio del pagamento viene ritentato a ogni avvio
del server da recover_stuck_bookings(), e se c'e' una contestazione aperta si
ferma sempre nello stesso punto, dopo aver avvisato il consulente. Nessuno
stato cambiava, quindi ogni deploy ne aggiungeva una.
"""
import secrets
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, Dispute, Notification, User
from app.utils.notification_service import send_notification
from app.utils.orari import now_italy_naive


@pytest.fixture
def consulenza_contestata():
    """Una consulenza pagata, coi soldi ancora trattenuti, e una
    contestazione aperta sopra: la situazione che bloccava il rilascio."""
    with Session(engine) as s:
        cliente = User(email=f"rip-c-{secrets.token_hex(4)}@test.local",
                       password_md5="x", confirmed=1, nome="Giulia")
        consulente = User(email=f"rip-p-{secrets.token_hex(4)}@test.local",
                          password_md5="x", confirmed=1, nome="Paolo")
        s.add(cliente)
        s.add(consulente)
        s.commit()
        s.refresh(cliente)
        s.refresh(consulente)

        giorno = (now_italy_naive() - timedelta(days=3)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        prenotazione = Booking(client_user_id=cliente.id,
                               consultant_user_id=consulente.id,
                               booking_date=giorno, start_time="10:00",
                               end_time="11:00", duration_minutes=60, price=60,
                               status="completed", payment_status="held",
                               payment_method="stripe")
        s.add(prenotazione)
        s.commit()
        s.refresh(prenotazione)

        contestazione = Dispute(booking_id=prenotazione.id,
                                client_user_id=cliente.id,
                                consultant_user_id=consulente.id,
                                description="La consulenza non e' andata bene",
                                status="open")
        s.add(contestazione)
        s.commit()
        dati = (consulente.id, prenotazione.id, cliente.id, contestazione.id)

    yield dati

    consulente_id, prenotazione_id, cliente_id, contestazione_id = dati
    with Session(engine) as s:
        for notifica in s.exec(select(Notification).where(
                Notification.user_id.in_([consulente_id, cliente_id]))).all():
            s.delete(notifica)
        contestazione = s.get(Dispute, contestazione_id)
        if contestazione:
            s.delete(contestazione)
        prenotazione = s.get(Booking, prenotazione_id)
        if prenotazione:
            s.delete(prenotazione)
        s.commit()
        for utente_id in (consulente_id, cliente_id):
            utente = s.get(User, utente_id)
            if utente:
                s.delete(utente)
        s.commit()


def _quante(user_id, tipo):
    with Session(engine) as s:
        return len(s.exec(select(Notification).where(
            Notification.user_id == user_id, Notification.type == tipo)).all())


def test_il_rilascio_bloccato_avvisa_una_volta_sola(consulenza_contestata):
    """Tre riavvii del server, una notifica."""
    from app.scheduler import release_booking_payment

    consulente_id, prenotazione_id, _, _ = consulenza_contestata
    for _ in range(3):
        release_booking_payment(prenotazione_id)

    assert _quante(consulente_id, "payment_hold") == 1


def test_solo_una_volta_guarda_la_prenotazione(consulenza_contestata):
    """Due consulenze diverse restano due notifiche: il doppione e' la
    stessa notifica sulla stessa prenotazione, non due prenotazioni."""
    consulente_id, prenotazione_id, _, _ = consulenza_contestata

    for _ in range(2):
        send_notification(user_id=consulente_id, type_key="payment_hold",
                          title="Pagamento in attesa", message="prima",
                          related_booking_id=prenotazione_id,
                          solo_una_volta=True)
    send_notification(user_id=consulente_id, type_key="payment_hold",
                      title="Pagamento in attesa", message="seconda",
                      related_booking_id=prenotazione_id + 90000,
                      solo_una_volta=True)

    assert _quante(consulente_id, "payment_hold") == 2


def test_senza_il_flag_niente_cambia(consulenza_contestata):
    """Le altre notifiche devono continuare a ripetersi: due prenotazioni
    confermate sono due avvisi, non uno."""
    consulente_id, _, _, _ = consulenza_contestata

    for _ in range(2):
        send_notification(user_id=consulente_id, type_key="booking_confirmed",
                          title="Prenotazione confermata", message="uguale")

    assert _quante(consulente_id, "booking_confirmed") == 2
