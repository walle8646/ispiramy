"""Movimenti di denaro da riprovare, senza pagarli due volte.

Un compenso, un rimborso o il resto dovuto al consulente dopo un rimborso
parziale può fallire (conto mancante, rete, Stripe). Resta una riga aperta:
si riprova fra 6 ore, al riavvio, o subito da chi amministra. Se il denaro è
già partito, il tentativo nuovo non ne manda altro.
"""
from datetime import timedelta

from sqlmodel import Session, select

from app.database import engine
from app.logger_config import logger
from app.models import Booking, TransactionProblem, User
from app.utils.orari import ITALY_TZ, now_italy_naive
from app.utils.prezzi import centesimi, percentuale_commissione, totale_pagato

ORE_FRA_UN_TENTATIVO = 6


def _job_id(kind: str, booking_id: int) -> str:
    return {
        "payout": f"payment_release_{booking_id}",
        "refund": f"refund_retry_{booking_id}",
        "transfer": f"consultant_share_{booking_id}",
    }[kind]


def conta_aperti() -> int:
    with Session(engine) as session:
        return len(session.exec(
            select(TransactionProblem.id).where(TransactionProblem.status == "open")
        ).all())


def aperto(session, booking_id: int, kind: str) -> TransactionProblem | None:
    return session.exec(
        select(TransactionProblem).where(
            TransactionProblem.booking_id == booking_id,
            TransactionProblem.kind == kind,
            TransactionProblem.status == "open",
        )
    ).first()


def registra(booking_id: int, kind: str, amount_cents: int, errore: str) -> None:
    """Aggiorna il problema aperto, oppure lo crea. Il prossimo tentativo è fra 6 ore."""
    adesso = now_italy_naive()
    prossimo = adesso + timedelta(hours=ORE_FRA_UN_TENTATIVO)
    motivo = (errore or "tentativo non riuscito").strip()
    if len(motivo) > 2000:
        motivo = motivo[:2000]
    with Session(engine) as session:
        riga = aperto(session, booking_id, kind)
        if riga is None:
            riga = TransactionProblem(
                booking_id=booking_id,
                kind=kind,
                status="open",
                amount_cents=max(0, int(amount_cents or 0)),
                attempts=0,
                created_at=adesso,
            )
        riga.amount_cents = max(0, int(amount_cents or 0)) or riga.amount_cents
        riga.error_reason = motivo
        riga.attempts = (riga.attempts or 0) + 1
        riga.last_attempt_at = adesso
        riga.next_attempt_at = prossimo
        riga.updated_at = adesso
        riga.status = "open"
        session.add(riga)
        session.commit()
    # Il compenso intero ha già il suo appuntamento (payment_release_):
    # riprogrammarlo qui lo sostituirebbe con un'altra funzione.
    if kind != "payout":
        _programma(kind, booking_id, prossimo)
    logger.info(
        f"🔁 Problema {kind} booking {booking_id}: "
        f"€{(amount_cents or 0) / 100:.2f}, nuovo tentativo {prossimo}"
    )


def chiudi(booking_id: int, kind: str, nota: str, user_id: int | None = None) -> None:
    """Chiude il problema aperto e toglie il tentativo programmato."""
    adesso = now_italy_naive()
    with Session(engine) as session:
        riga = aperto(session, booking_id, kind)
        if riga is None:
            return
        riga.status = "resolved"
        riga.resolution_note = (nota or "")[:2000]
        riga.resolved_at = adesso
        riga.resolved_by_user_id = user_id
        riga.next_attempt_at = None
        riga.updated_at = adesso
        session.add(riga)
        session.commit()
    _annulla_job(kind, booking_id)


def _programma(kind: str, booking_id: int, quando_naive) -> None:
    from apscheduler.triggers.date import DateTrigger

    from app.scheduler import scheduler

    quando = quando_naive.replace(tzinfo=ITALY_TZ) if quando_naive.tzinfo is None else quando_naive
    funzione = {
        "payout": _riprova_compenso,
        "refund": _riprova_rimborso,
        "transfer": _riprova_quota,
    }[kind]
    try:
        scheduler.add_job(
            funzione,
            trigger=DateTrigger(run_date=quando),
            args=[booking_id],
            id=_job_id(kind, booking_id),
            replace_existing=True,
            misfire_grace_time=3600,
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Tentativo {kind} non programmabile per booking {booking_id}: {e}")


def _annulla_job(kind: str, booking_id: int) -> None:
    from apscheduler.jobstores.base import JobLookupError

    from app.scheduler import scheduler

    try:
        scheduler.remove_job(_job_id(kind, booking_id))
    except JobLookupError:
        return
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Job {kind} non cancellato per booking {booking_id}: {e}")


def quota_consulente_centesimi(booking, consulente, refund_pct: int) -> int:
    """Quanto resta al consulente dopo un rimborso parziale, commissione tolta."""
    prezzo = centesimi(booking.price)
    rimanenza = prezzo - int(prezzo * refund_pct / 100)
    if rimanenza <= 0:
        return 0
    fee = percentuale_commissione(consulente)
    return max(0, rimanenza - int(rimanenza * fee / 100))


def accantona_quota(booking_id: int, amount_cents: int, errore: str) -> None:
    """Il cliente è già stato rimborsato: la quota del consulente resta ferma e si riprova."""
    registra(booking_id, "transfer", amount_cents, errore)


def accantona_rimborso(booking_id: int, amount_cents: int, errore: str) -> None:
    registra(booking_id, "refund", amount_cents, errore)


def accantona_compenso(booking_id: int, errore: str) -> None:
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        consulente = session.get(User, booking.consultant_user_id) if booking else None
        if booking is None:
            return
        importo = 0
        from app.utils.prezzi import compenso_consulente_centesimi
        importo = compenso_consulente_centesimi(
            booking.price, percentuale_commissione(consulente)
        )
    registra(booking_id, "payout", importo, errore)


def _gia_pagato_al_consulente(booking) -> bool:
    return bool(booking.stripe_transfer_id or booking.paypal_payout_id)


def _riprova_quota(booking_id: int) -> None:
    """Manda solo la quota già calcolata. Se è già partita, non la rimanda."""
    with Session(engine) as session:
        riga = aperto(session, booking_id, "transfer")
        booking = session.get(Booking, booking_id)
        if riga is None or booking is None:
            return
        if _gia_pagato_al_consulente(booking):
            gia = True
            importo = riga.amount_cents
        else:
            gia = False
            importo = riga.amount_cents
            consulente = session.get(User, booking.consultant_user_id)
    if gia:
        chiudi(booking_id, "transfer", "Quota già inviata al consulente, nessun altro trasferimento.")
        return
    if importo <= 0:
        chiudi(booking_id, "transfer", "Nessuna quota da inviare.")
        return
    try:
        _invia_quota(booking_id, importo)
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Quota consulente booking {booking_id} non inviata: {e}")
        registra(booking_id, "transfer", importo, str(e))
        return
    chiudi(booking_id, "transfer", "Quota inviata al consulente.")


def _invia_quota(booking_id: int, importo: int) -> None:
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if booking is None:
            raise RuntimeError("prenotazione sparita")
        if _gia_pagato_al_consulente(booking):
            return
        consulente = session.get(User, booking.consultant_user_id)
        if booking.payment_method == "paypal":
            if not consulente or not consulente.paypal_email:
                raise RuntimeError("Il consulente non ha un conto PayPal collegato")
            from app.utils.paypal_config import create_payout
            payout = create_payout(
                recipient_email=consulente.paypal_email,
                amount=round(importo / 100, 2),
                currency="EUR",
                note=f"Pagamento parziale consulenza #{booking.id}",
                sender_item_id=f"booking_{booking.id}_partial",
            )
            if not payout:
                raise RuntimeError("PayPal non ha accettato il pagamento")
            booking.paypal_payout_id = payout.get("batch_header", {}).get("payout_batch_id", "") or "paypal"
        else:
            if not consulente or not consulente.stripe_account_id:
                raise RuntimeError("Il consulente non ha un conto Stripe collegato")
            import stripe
            import os
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
            transfer = stripe.Transfer.create(
                amount=importo,
                currency="eur",
                destination=consulente.stripe_account_id,
                transfer_group=f"booking_{booking.id}",
                idempotency_key=f"consultant-share-{booking.id}",
                metadata={
                    "booking_id": str(booking.id),
                    "partial_payment": "true",
                },
            )
            booking.stripe_transfer_id = transfer.id
        booking.payment_released_at = now_italy_naive()
        session.add(booking)
        session.commit()
        try:
            from app.utils.notification_service import send_notification
            send_notification(
                user_id=consulente.id,
                type_key="payment_released",
                title="Pagamento parziale ricevuto",
                message=f"Hai ricevuto €{importo / 100:.2f} per la consulenza #{booking.id}.",
                related_booking_id=booking.id,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"❌ Avviso quota booking {booking.id} non inviato: {e}")


def _riprova_compenso(booking_id: int) -> None:
    from app.scheduler import release_booking_payment

    release_booking_payment(booking_id)
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if booking and booking.payment_status == "released":
            chiudi(booking_id, "payout", "Compenso inviato al consulente.")


def _riprova_rimborso(booking_id: int) -> None:
    with Session(engine) as session:
        riga = aperto(session, booking_id, "refund")
        booking = session.get(Booking, booking_id)
        if riga is None or booking is None:
            return
        if booking.payment_status in ("refunded", "partially_refunded") and booking.refund_amount:
            gia = True
            importo = riga.amount_cents
        else:
            gia = False
            importo = riga.amount_cents
            metodo = booking.payment_method
            intent = booking.stripe_payment_intent_id
            capture = booking.paypal_capture_id
    if gia:
        chiudi(booking_id, "refund", "Rimborso già registrato, non ne parte un altro.")
        return
    try:
        from decimal import Decimal
        if metodo == "paypal" and capture:
            from app.utils.paypal_config import refund_capture
            result = refund_capture(capture, amount=round(importo / 100, 2), currency="EUR")
            if not result:
                raise RuntimeError("PayPal non ha accettato il rimborso")
        elif intent:
            import os
            import stripe
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
            stripe.Refund.create(
                payment_intent=intent,
                amount=importo,
                idempotency_key=f"refund-{booking_id}-{importo}",
            )
        else:
            raise RuntimeError("Nessun pagamento da rimborsare")
        with Session(engine) as session:
            booking = session.get(Booking, booking_id)
            booking.refund_amount = Decimal(str(importo / 100))
            totale = centesimi(totale_pagato(booking))
            booking.payment_status = "refunded" if importo >= totale else "partially_refunded"
            session.add(booking)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Rimborso booking {booking_id} non riuscito: {e}")
        registra(booking_id, "refund", importo, str(e))
        return
    chiudi(booking_id, "refund", "Rimborso inviato al cliente.")


def riprova_adesso(problem_id: int) -> str:
    """Un tentativo subito, lo stesso che scatterebbe da solo fra 6 ore."""
    with Session(engine) as session:
        riga = session.get(TransactionProblem, problem_id)
        if riga is None:
            return "manca"
        if riga.status != "open":
            return "chiuso"
        kind, booking_id = riga.kind, riga.booking_id
    if kind == "transfer":
        _riprova_quota(booking_id)
    elif kind == "refund":
        _riprova_rimborso(booking_id)
    elif kind == "payout":
        _riprova_compenso(booking_id)
    else:
        return "tipo"
    return "ok"


def segna_risolto(problem_id: int, user_id: int, nota: str) -> str:
    testo = (nota or "").strip()
    if not testo:
        return "nota"
    with Session(engine) as session:
        riga = session.get(TransactionProblem, problem_id)
        if riga is None:
            return "manca"
        if riga.status != "open":
            return "chiuso"
        kind, booking_id = riga.kind, riga.booking_id
    chiudi(booking_id, kind, testo, user_id=user_id)
    return "ok"


def riprendi_dopo_riavvio() -> None:
    """Al riavvio: ciò che era già in scadenza si riprova, il resto resta in calendario."""
    adesso = now_italy_naive()
    with Session(engine) as session:
        aperti = session.exec(
            select(TransactionProblem).where(TransactionProblem.status == "open")
        ).all()
        lavoro = [
            (riga.id, riga.kind, riga.booking_id, riga.next_attempt_at)
            for riga in aperti
        ]
    for _id, kind, booking_id, prossimo in lavoro:
        if prossimo is None or prossimo <= adesso:
            if kind == "transfer":
                _riprova_quota(booking_id)
            elif kind == "refund":
                _riprova_rimborso(booking_id)
            elif kind == "payout":
                _riprova_compenso(booking_id)
        else:
            _programma(kind, booking_id, prossimo)
