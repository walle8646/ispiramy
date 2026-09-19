"""Richieste di consulenza che il consulente deve accettare o rifiutare.

Per i consulenti con la conferma automatica spenta (User.auto_accept_bookings)
una prenotazione diretta non è confermata subito:

    pagamento autorizzato  →  awaiting_acceptance / authorized
    il consulente accetta  →  si incassa, confirmed / held (come oggi)
    rifiuta o non risponde →  si annulla il blocco, cancelled / voided

L'importo viene solo bloccato (Stripe capture manuale, PayPal intent
AUTHORIZE): se la richiesta non va avanti non c'è nessun addebito da
rimborsare, e nessuna commissione persa. La scadenza è al massimo 24 ore, ben
dentro la validità di un'autorizzazione (7 giorni su carta, 3 su PayPal).
"""
import os
from datetime import datetime, timedelta
from typing import Optional

from zoneinfo import ZoneInfo

from app.logger_config import logger
from app.utils.orari import con_fuso, data_consulenza, now_italy_naive

ITALY_TZ = ZoneInfo("Europe/Rome")

ORE_PER_RISPONDERE = 24
# Il consulente deve rispondere al piu' tardi un'ora prima dell'inizio: dopo,
# la richiesta viene rifiutata da sola e il cliente e' libero di cercare altro.
MARGINE_PRIMA_DELL_INIZIO = timedelta(hours=1)
# Mai meno di così per rispondere (conta solo con prenotazioni molto vicine)
TEMPO_MINIMO_PER_RISPONDERE = timedelta(minutes=30)
# Il job di scadenza lascia passare qualche minuto dopo la scadenza: un
# "Accetta" cliccato all'ultimo secondo non deve incrociarsi con l'annullamento.
TOLLERANZA_SCADENZA = timedelta(minutes=2)


class RichiestaNonValida(Exception):
    """Operazione non possibile sulla richiesta (messaggio per l'utente)."""

    def __init__(self, messaggio: str, status_code: int = 400):
        super().__init__(messaggio)
        self.messaggio = messaggio
        self.status_code = status_code


def richiede_accettazione(consulente) -> bool:
    """True se le prenotazioni dirette verso questo consulente vanno accettate a mano."""
    return consulente is not None and consulente.auto_accept_bookings is False


def scadenza_risposta(inizio: datetime, adesso: Optional[datetime] = None) -> datetime:
    """Entro quando il consulente deve rispondere: 24 ore, e comunque un'ora prima dell'inizio."""
    adesso = adesso or now_italy_naive()
    scadenza = min(adesso + timedelta(hours=ORE_PER_RISPONDERE), inizio - MARGINE_PRIMA_DELL_INIZIO)
    if scadenza < adesso + TEMPO_MINIMO_PER_RISPONDERE:
        scadenza = min(adesso + TEMPO_MINIMO_PER_RISPONDERE, inizio)
    return scadenza.replace(microsecond=0)


def formatta_scadenza(scadenza: Optional[datetime]) -> str:
    return scadenza.strftime("%d/%m alle %H:%M") if scadenza else ""


def _inizio(booking) -> datetime:
    data = data_consulenza(booking.booking_date)
    return datetime.combine(data, datetime.strptime(booking.start_time, "%H:%M").time())


def _fine(booking) -> datetime:
    data = data_consulenza(booking.booking_date)
    return datetime.combine(data, datetime.strptime(booking.end_time, "%H:%M").time())


def _nome(user, ripiego: str) -> str:
    if user and user.nome:
        return f"{user.nome} {user.cognome or ''}".strip()
    return ripiego


def _url(percorso: str) -> str:
    return f"{os.getenv('BASE_URL', 'http://localhost:8080')}{percorso}"


def _stripe():
    from app.utils.stripe_config import stripe_module
    if not stripe_module:
        raise RuntimeError("Stripe non disponibile")
    return stripe_module


# ---------------------------------------------------------------- job e notifiche

def programma_job_consulenza(booking) -> None:
    """Promemoria, controllo assenze e rilascio del pagamento di una consulenza confermata."""
    from app.scheduler import schedule_booking_reminders, schedule_noshow_check, schedule_payment_release

    inizio = _inizio(booking).replace(tzinfo=ITALY_TZ)
    fine = _fine(booking).replace(tzinfo=ITALY_TZ)
    schedule_booking_reminders(
        booking_id=booking.id,
        booking_datetime=inizio,
        client_id=booking.client_user_id,
        consultant_id=booking.consultant_user_id,
    )
    schedule_payment_release(booking.id, fine)
    schedule_noshow_check(booking.id, fine)


def conferma_consulenza(session, booking, *, messaggio_consulente: Optional[str] = None) -> None:
    """Notifiche e job di una consulenza confermata e pagata.

    Una sola versione per tutti i modi di arrivarci (Stripe, PayPal, offerta
    accettata): prima ogni flusso aveva la sua copia, e il cliente non riceveva
    nessuna conferma.
    """
    from app.models import User
    from app.utils.notification_service import send_notification

    cliente = session.get(User, booking.client_user_id)
    consulente = session.get(User, booking.consultant_user_id)
    nome_cliente = _nome(cliente, "Un utente")
    nome_consulente = _nome(consulente, "Il consulente")
    data = f"{booking.booking_date:%d/%m/%Y}"
    dettagli = {
        "client_name": nome_cliente,
        "consultant_name": nome_consulente,
        "date": data,
        "time": booking.start_time,
        "duration": str(booking.duration_minutes),
        "action_url": _url("/profile#bookings"),
    }

    send_notification(
        user_id=booking.consultant_user_id,
        type_key="booking_confirmed",
        title="Nuova Prenotazione!",
        message=messaggio_consulente or (
            f"{nome_cliente} ha prenotato una consulenza per il {data} alle {con_fuso(booking.start_time)}"
        ),
        template_data=dettagli,
        related_booking_id=booking.id,
        related_user_id=booking.client_user_id,
        action_url="/profile#bookings",
    )
    send_notification(
        user_id=booking.client_user_id,
        type_key="booking_confirmed_client",
        title="Consulenza confermata",
        message=f"La tua consulenza con {nome_consulente} del {data} alle {booking.start_time} è confermata",
        template_data=dettagli,
        related_booking_id=booking.id,
        related_user_id=booking.consultant_user_id,
        action_url="/profile#bookings",
    )
    programma_job_consulenza(booking)
    logger.info(f"📅 Consulenza {booking.id} confermata: notifiche inviate e job schedulati")


def avvisa_annullamento(session, booking, annullata_da_user_id: int, motivo: Optional[str] = None) -> None:
    """Avvisa l'altro partecipante che la consulenza è stata annullata."""
    from html import escape

    from app.models import User
    from app.utils.notification_service import send_notification
    from app.utils.notification_email import NOTA_BLOCCO_ANNULLATO, NOTA_RIMBORSO

    if annullata_da_user_id == booking.client_user_id:
        destinatario_id, altro_id = booking.consultant_user_id, booking.client_user_id
    else:
        destinatario_id, altro_id = booking.client_user_id, booking.consultant_user_id

    destinatario = session.get(User, destinatario_id)
    altro = session.get(User, altro_id)
    nome_altro = _nome(altro, "L'altro partecipante")
    data = f"{booking.booking_date:%d/%m/%Y}"

    sezione_motivo = ""
    if motivo:
        sezione_motivo = (
            '<div style="background:#fff3cd;padding:15px;border-radius:5px;'
            'border-left:4px solid #ffc107;margin:20px 0;">'
            f"<p><strong>📝 Motivo:</strong></p><p>{escape(motivo)}</p></div>"
        )

    send_notification(
        user_id=destinatario_id,
        type_key="booking_cancelled",
        title="Consulenza annullata",
        message=f"{nome_altro} ha annullato la consulenza del {data} alle {con_fuso(booking.start_time)}",
        template_data={
            "user_name": _nome(destinatario, "Ciao"),
            "other_name": nome_altro,
            "date": data,
            "time": booking.start_time,
            "reason_section": sezione_motivo,
            "refund_note": NOTA_BLOCCO_ANNULLATO if booking.payment_status == "voided" else NOTA_RIMBORSO,
            "action_url": _url("/profile#bookings"),
        },
        related_booking_id=booking.id,
        related_user_id=altro_id,
        action_url="/profile#bookings",
    )


def metti_in_attesa(session, booking) -> None:
    """Pagamento autorizzato: la prenotazione aspetta la risposta del consulente."""
    from app.models import User
    from app.utils.notification_service import send_notification

    booking.status = "awaiting_acceptance"
    booking.payment_status = "authorized"
    # Ricalcolata ora: dal momento in cui è partito il checkout può essere
    # passata mezz'ora, e la scadenza vale dal pagamento.
    booking.acceptance_deadline = scadenza_risposta(_inizio(booking))
    booking.updated_at = datetime.utcnow()
    session.add(booking)
    session.commit()
    session.refresh(booking)

    cliente = session.get(User, booking.client_user_id)
    consulente = session.get(User, booking.consultant_user_id)
    nome_cliente = _nome(cliente, "Un utente")
    scadenza = formatta_scadenza(booking.acceptance_deadline)
    argomento = (booking.description or booking.client_notes or "").strip()
    if len(argomento) > 400:
        argomento = argomento[:400].rstrip() + "…"

    send_notification(
        user_id=booking.consultant_user_id,
        type_key="booking_request",
        title="Nuova richiesta di consulenza",
        message=(
            f"{nome_cliente} chiede una consulenza il {booking.booking_date:%d/%m/%Y} "
            f"alle {booking.start_time}. Accetta o rifiuta entro il {scadenza}."
        ),
        template_data={
            "consultant_name": _nome(consulente, "Consulente"),
            "client_name": nome_cliente,
            "date": f"{booking.booking_date:%d/%m/%Y}",
            "time": booking.start_time,
            "duration": str(booking.duration_minutes),
            "deadline": scadenza,
            "topic": argomento or "—",
            "action_url": _url("/profile#bookings"),
        },
        related_booking_id=booking.id,
        related_user_id=booking.client_user_id,
        action_url="/profile#bookings",
    )
    logger.info(f"📩 Booking {booking.id} in attesa di accettazione fino a {booking.acceptance_deadline}")


# ---------------------------------------------------------------- pagamento

def incassa(booking) -> Optional[str]:
    """Incassa l'importo autorizzato. Ritorna None se riuscito, altrimenti il motivo."""
    if booking.payment_method == "paypal":
        if not booking.paypal_authorization_id:
            return "autorizzazione PayPal mancante"
        from app.utils.paypal_config import capture_authorization
        dati = capture_authorization(booking.paypal_authorization_id)
        if not dati or dati.get("status") not in ("COMPLETED", "PENDING"):
            return f"incasso PayPal non riuscito ({(dati or {}).get('status')})"
        booking.paypal_capture_id = dati.get("id")
        return None

    if not booking.stripe_payment_intent_id:
        return "pagamento Stripe mancante"
    try:
        intent = _stripe().PaymentIntent.capture(booking.stripe_payment_intent_id)
    except Exception as e:  # noqa: BLE001
        return f"incasso Stripe non riuscito ({e})"
    if getattr(intent, "status", None) != "succeeded":
        return f"incasso Stripe non riuscito (stato {getattr(intent, 'status', None)})"
    return None


def annulla_blocco(booking) -> bool:
    """Annulla l'autorizzazione: l'importo bloccato torna disponibile al cliente."""
    if booking.payment_method == "paypal":
        if not booking.paypal_authorization_id:
            return False
        from app.utils.paypal_config import void_authorization
        return void_authorization(booking.paypal_authorization_id)

    if not booking.stripe_payment_intent_id:
        return False
    try:
        _stripe().PaymentIntent.cancel(booking.stripe_payment_intent_id)
        return True
    except Exception as e:  # noqa: BLE001
        # Già annullata (es. autorizzazione scaduta da sola): il risultato è lo stesso
        if "canceled" in str(e).lower():
            return True
        logger.error(f"❌ Annullamento autorizzazione Stripe booking {booking.id}: {e}")
        return False


# ---------------------------------------------------------------- azioni

def accetta_richiesta(session, booking, consulente_id: int) -> None:
    """Il consulente accetta: si incassa e la consulenza diventa confermata."""
    from app.models import User
    from app.utils.notification_service import send_notification

    if booking.consultant_user_id != consulente_id:
        raise RichiestaNonValida("Solo il consulente può accettare la richiesta", 403)
    if booking.status == "confirmed":
        return  # doppio click: già fatto
    if booking.status != "awaiting_acceptance" or booking.payment_status != "authorized":
        raise RichiestaNonValida("Questa richiesta non è più in attesa di risposta")
    if booking.acceptance_deadline and now_italy_naive() >= booking.acceptance_deadline:
        raise RichiestaNonValida("La richiesta è scaduta: il cliente è stato avvisato e non gli è stato addebitato nulla")

    errore = incassa(booking)
    if errore:
        logger.error(f"❌ Booking {booking.id}: accettazione fallita, {errore}")
        raise RichiestaNonValida(
            "Non siamo riusciti a incassare il pagamento del cliente (l'autorizzazione "
            "potrebbe essere scaduta o revocata). La richiesta non è stata accettata: riprova "
            "tra qualche minuto o rifiutala.",
            502,
        )

    booking.status = "confirmed"
    booking.payment_status = "held"
    booking.updated_at = datetime.utcnow()
    session.add(booking)
    session.commit()
    session.refresh(booking)
    programma_job_consulenza(booking)

    consulente = session.get(User, booking.consultant_user_id)
    cliente = session.get(User, booking.client_user_id)
    nome_consulente = _nome(consulente, "Il consulente")
    send_notification(
        user_id=booking.client_user_id,
        type_key="booking_accepted",
        title="Richiesta accettata",
        message=(
            f"{nome_consulente} ha accettato la tua consulenza del "
            f"{booking.booking_date:%d/%m/%Y} alle {con_fuso(booking.start_time)}"
        ),
        template_data={
            "client_name": _nome(cliente, "Cliente"),
            "consultant_name": nome_consulente,
            "date": f"{booking.booking_date:%d/%m/%Y}",
            "time": booking.start_time,
            "duration": str(booking.duration_minutes),
            "action_url": _url("/profile#bookings"),
        },
        related_booking_id=booking.id,
        related_user_id=booking.consultant_user_id,
        action_url="/profile#bookings",
    )
    logger.info(f"✅ Booking {booking.id} accettato dal consulente e incassato")


def scadi_richieste_senza_risposta() -> int:
    """Annulla le richieste a cui il consulente non ha risposto in tempo. Job periodico."""
    from sqlmodel import Session, select

    from app.database import engine
    from app.models import Booking, User
    from app.utils.notification_service import send_notification

    limite = now_italy_naive() - TOLLERANZA_SCADENZA
    scadute = 0
    try:
        with Session(engine) as session:
            richieste = session.exec(
                select(Booking)
                .where(Booking.status == "awaiting_acceptance")
                .where(Booking.acceptance_deadline < limite)
            ).all()
            for booking in richieste:
                if not annulla_blocco(booking):
                    # Riprova al prossimo giro: meglio uno slot occupato qualche
                    # minuto in più che un blocco sulla carta del cliente dimenticato.
                    logger.error(f"❌ Booking {booking.id}: blocco non annullato, riprovo più tardi")
                    continue
                booking.status = "cancelled"
                booking.payment_status = "voided"
                booking.cancellation_reason = "Il consulente non ha risposto in tempo"
                booking.cancelled_at = datetime.utcnow()
                booking.updated_at = datetime.utcnow()
                session.add(booking)
                session.commit()
                scadute += 1

                consulente = session.get(User, booking.consultant_user_id)
                cliente = session.get(User, booking.client_user_id)
                nome_consulente = _nome(consulente, "Il consulente")
                send_notification(
                    user_id=booking.client_user_id,
                    type_key="booking_request_expired",
                    title="Richiesta scaduta",
                    message=(
                        f"{nome_consulente} non ha risposto in tempo alla tua richiesta del "
                        f"{booking.booking_date:%d/%m/%Y}. Non ti è stato addebitato nulla."
                    ),
                    template_data={
                        "client_name": _nome(cliente, "Cliente"),
                        "consultant_name": nome_consulente,
                        "date": f"{booking.booking_date:%d/%m/%Y}",
                        "time": booking.start_time,
                        "action_url": _url("/"),
                    },
                    related_booking_id=booking.id,
                    related_user_id=booking.consultant_user_id,
                    action_url="/",
                )
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Errore scadenza richieste di consulenza: {e}")
    if scadute:
        logger.info(f"⌛ {scadute} richieste di consulenza scadute senza risposta")
    return scadute
