"""
Routes per il pannello di amministrazione.
Solo utenti con user_type_id >= 2 possono accedere.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select, func, text
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field
from typing import Optional
import asyncio
import os

from app.database import engine
from app.models import (
    User, Booking, Dispute, DisputeMessage, Review, CommunityQuestion, Category,
    TransactionProblem, CommunityReport,
)
from app.utils.verifica_categorie import id_categorie_verificate, scrivi_categorie_verificate
from app.utils.archivio_s3 import servi as foto_pubblica
from app.routes.auth import verify_token
from app.logger_config import logger
from app.utils.orari import now_italy_naive
from app.utils.prezzi import centesimi, centesimi_rimborso_contestazione, percentuale_commissione, totale_pagato

router = APIRouter(prefix="/admin")

ITALY_TZ = ZoneInfo("Europe/Rome")


def require_admin(request: Request) -> User:
    """Verifica che l'utente sia admin (user_type_id >= 2). Ritorna l'utente o lancia eccezione."""
    user = verify_token(request)
    if not user:
        return None
    if (user.user_type_id or 0) < 2:
        return None
    return user


# ==================== DASHBOARD ====================

@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Dashboard principale admin con statistiche"""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    now = datetime.now(ITALY_TZ)
    thirty_days_ago = now - timedelta(days=30)
    seven_days_ago = now - timedelta(days=7)

    with Session(engine) as session:
        # === STATISTICHE UTENTI ===
        total_users = session.exec(select(func.count(User.id))).one()
        verified_users = session.exec(
            select(func.count(User.id)).where(User.is_verified == True)
        ).one()
        new_users_30d = session.exec(
            select(func.count(User.id)).where(User.created_at >= thirty_days_ago)
        ).one()
        new_users_7d = session.exec(
            select(func.count(User.id)).where(User.created_at >= seven_days_ago)
        ).one()

        # === STATISTICHE CONSULENZE ===
        total_bookings = session.exec(select(func.count(Booking.id))).one()
        completed_bookings = session.exec(
            select(func.count(Booking.id)).where(Booking.status == "completed")
        ).one()
        pending_bookings = session.exec(
            select(func.count(Booking.id)).where(Booking.status == "pending")
        ).one()
        confirmed_bookings = session.exec(
            select(func.count(Booking.id)).where(Booking.status == "confirmed")
        ).one()
        cancelled_bookings = session.exec(
            select(func.count(Booking.id)).where(Booking.status == "cancelled")
        ).one()
        bookings_30d = session.exec(
            select(func.count(Booking.id)).where(Booking.created_at >= thirty_days_ago)
        ).one()
        bookings_7d = session.exec(
            select(func.count(Booking.id)).where(Booking.created_at >= seven_days_ago)
        ).one()

        # === STATISTICHE PAGAMENTI ===
        total_revenue_result = session.exec(
            select(func.sum(Booking.price)).where(Booking.payment_status.in_(["paid", "held", "released", "partially_refunded"]))
        ).one()
        total_revenue = float(total_revenue_result) if total_revenue_result else 0.0
        
        revenue_30d_result = session.exec(
            select(func.sum(Booking.price)).where(
                Booking.payment_status.in_(["paid", "held", "released", "partially_refunded"]),
                Booking.created_at >= thirty_days_ago
            )
        ).one()
        revenue_30d = float(revenue_30d_result) if revenue_30d_result else 0.0

        paid_bookings = session.exec(
            select(func.count(Booking.id)).where(Booking.payment_status.in_(["paid", "held", "released", "partially_refunded"]))
        ).one()

        # === STATISTICHE RECENSIONI ===
        total_reviews = session.exec(select(func.count(Review.id))).one()
        avg_rating_result = session.exec(
            select(
                func.avg((Review.rating_helpful + Review.rating_prepared + Review.rating_communication) / 3.0)
            )
        ).one()
        avg_rating = round(float(avg_rating_result), 1) if avg_rating_result else 0.0

        # === STATISTICHE CONTESTAZIONI ===
        total_disputes = session.exec(select(func.count(Dispute.id))).one()
        open_disputes = session.exec(
            select(func.count(Dispute.id)).where(Dispute.status == "open")
        ).one()
        in_review_disputes = session.exec(
            select(func.count(Dispute.id)).where(Dispute.status == "in_review")
        ).one()
        resolved_disputes = session.exec(
            select(func.count(Dispute.id)).where(Dispute.status == "resolved")
        ).one()

        # === STATISTICHE COMMUNITY ===
        total_questions = session.exec(select(func.count(CommunityQuestion.id))).one()
        questions_30d = session.exec(
            select(func.count(CommunityQuestion.id)).where(CommunityQuestion.created_at >= thirty_days_ago)
        ).one()

        # === ULTIMI UTENTI REGISTRATI ===
        recent_users = session.exec(
            select(User).order_by(User.created_at.desc()).limit(5)
        ).all()

        # === ULTIME CONTESTAZIONI ===
        recent_disputes_raw = session.exec(
            select(Dispute).order_by(Dispute.created_at.desc()).limit(5)
        ).all()
        
        # Arricchisci disputes con nomi utente
        recent_disputes = []
        for d in recent_disputes_raw:
            client = session.get(User, d.client_user_id)
            consultant = session.get(User, d.consultant_user_id)
            recent_disputes.append({
                "id": d.id,
                "booking_id": d.booking_id,
                "client_name": f"{client.nome or ''} {client.cognome or ''}".strip() if client else "?",
                "consultant_name": f"{consultant.nome or ''} {consultant.cognome or ''}".strip() if consultant else "?",
                "status": d.status,
                "created_at": d.created_at,
                "description": d.description[:100] + "..." if len(d.description) > 100 else d.description,
            })

    stats = {
        "users": {
            "total": total_users,
            "verified": verified_users,
            "new_30d": new_users_30d,
            "new_7d": new_users_7d,
        },
        "bookings": {
            "total": total_bookings,
            "completed": completed_bookings,
            "pending": pending_bookings,
            "confirmed": confirmed_bookings,
            "cancelled": cancelled_bookings,
            "last_30d": bookings_30d,
            "last_7d": bookings_7d,
        },
        "revenue": {
            "total": total_revenue,
            "last_30d": revenue_30d,
            "paid_count": paid_bookings,
        },
        "reviews": {
            "total": total_reviews,
            "avg_rating": avg_rating,
        },
        "disputes": {
            "total": total_disputes,
            "open": open_disputes,
            "in_review": in_review_disputes,
            "resolved": resolved_disputes,
        },
        "community": {
            "total_questions": total_questions,
            "last_30d": questions_30d,
        },
    }

    return request.app.state.templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "current_user": admin_user,
            "user": admin_user,
            "stats": stats,
            "recent_users": recent_users,
            "recent_disputes": recent_disputes,
        }
    )


# ==================== CONTESTAZIONI ====================

@router.get("/disputes", response_class=HTMLResponse)
async def admin_disputes_list(request: Request):
    """Lista di tutte le contestazioni"""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    status_filter = request.query_params.get("status", "all")

    with Session(engine) as session:
        query = select(Dispute).order_by(Dispute.created_at.desc())
        if status_filter != "all":
            query = query.where(Dispute.status == status_filter)
        
        disputes_raw = session.exec(query).all()

        disputes = []
        for d in disputes_raw:
            client = session.get(User, d.client_user_id)
            consultant = session.get(User, d.consultant_user_id)
            booking = session.get(Booking, d.booking_id)
            msg_count = session.exec(
                select(func.count(DisputeMessage.id)).where(DisputeMessage.dispute_id == d.id)
            ).one()
            disputes.append({
                "id": d.id,
                "booking_id": d.booking_id,
                "client_name": f"{client.nome or ''} {client.cognome or ''}".strip() if client else "?",
                "client_email": client.email if client else "?",
                "consultant_name": f"{consultant.nome or ''} {consultant.cognome or ''}".strip() if consultant else "?",
                "consultant_email": consultant.email if consultant else "?",
                "status": d.status,
                "created_at": d.created_at,
                "description": d.description,
                "booking_date": booking.booking_date if booking else None,
                "booking_price": float(booking.price) if booking and booking.price else 0,
                "message_count": msg_count,
            })

        # Contatori per tab
        counts = {
            "all": session.exec(select(func.count(Dispute.id))).one(),
            "open": session.exec(select(func.count(Dispute.id)).where(Dispute.status == "open")).one(),
            "in_review": session.exec(select(func.count(Dispute.id)).where(Dispute.status == "in_review")).one(),
            "resolved": session.exec(select(func.count(Dispute.id)).where(Dispute.status == "resolved")).one(),
            "rejected": session.exec(select(func.count(Dispute.id)).where(Dispute.status == "rejected")).one(),
        }

    return request.app.state.templates.TemplateResponse(
        "admin/disputes.html",
        {
            "request": request,
            "current_user": admin_user,
            "user": admin_user,
            "disputes": disputes,
            "status_filter": status_filter,
            "counts": counts,
        }
    )


@router.get("/disputes/{dispute_id}", response_class=HTMLResponse)
async def admin_dispute_detail(dispute_id: int, request: Request):
    """Dettaglio singola contestazione con messaggi"""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    with Session(engine) as session:
        dispute = session.get(Dispute, dispute_id)
        if not dispute:
            raise HTTPException(status_code=404, detail="Contestazione non trovata")

        client = session.get(User, dispute.client_user_id)
        consultant = session.get(User, dispute.consultant_user_id)
        booking = session.get(Booking, dispute.booking_id)

        # Messaggi della contestazione
        messages_raw = session.exec(
            select(DisputeMessage).where(DisputeMessage.dispute_id == dispute_id).order_by(DisputeMessage.created_at)
        ).all()

        messages = []
        for m in messages_raw:
            sender = session.get(User, m.sender_user_id) if m.sender_user_id else None
            messages.append({
                "id": m.id,
                "message": m.message,
                "is_admin": m.is_admin,
                "sender_name": f"{sender.nome or ''} {sender.cognome or ''}".strip() if sender else "Team Ispiramy",
                "created_at": m.created_at,
            })

        dispute_data = {
            "id": dispute.id,
            "booking_id": dispute.booking_id,
            "status": dispute.status,
            "description": dispute.description,
            "created_at": dispute.created_at,
            "updated_at": dispute.updated_at,
            "client": {
                "id": client.id if client else None,
                "name": f"{client.nome or ''} {client.cognome or ''}".strip() if client else "?",
                "email": client.email if client else "?",
                "profile_picture": foto_pubblica(client.profile_picture) or None if client else None,
            },
            "consultant": {
                "id": consultant.id if consultant else None,
                "name": f"{consultant.nome or ''} {consultant.cognome or ''}".strip() if consultant else "?",
                "email": consultant.email if consultant else "?",
                "profile_picture": foto_pubblica(consultant.profile_picture) or None if consultant else None,
                "professione": consultant.professione if consultant else None,
                "platform_fee_percent": consultant.platform_fee_percent if consultant else 20,
            },
            "booking": {
                "date": booking.booking_date if booking else None,
                "start_time": booking.start_time if booking else None,
                "end_time": booking.end_time if booking else None,
                "price": float(booking.price) if booking and booking.price else 0,
                "service_fee": float(booking.service_fee or 0) if booking else 0,
                "total_paid": float(totale_pagato(booking)) if booking else 0,
                "status": booking.status if booking else None,
                "payment_status": booking.payment_status if booking else None,
                "recording_requested": bool(booking.recording_requested) if booking else False,
                "recording_filename": booking.recording_filename if booking else None,
            },
            "ai_verdict": dispute.ai_verdict,
            "ai_confidence": dispute.ai_confidence,
            "ai_comment": dispute.ai_comment,
            "ai_analyzed_at": dispute.ai_analyzed_at,
        }

    return request.app.state.templates.TemplateResponse(
        "admin/dispute_detail.html",
        {
            "request": request,
            "current_user": admin_user,
            "user": admin_user,
            "dispute": dispute_data,
            "messages": messages,
        }
    )


class DisputeMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)


class DisputeStatusRequest(BaseModel):
    status: str
    refund_percentage: Optional[int] = None  # 0-100, solo per status 'resolved'


def applica_rimborso_contestazione(session, booking, refund_pct: int) -> None:
    """Rimborsa al cliente una percentuale del prezzo della consulenza.

    Le spese di servizio restano a Ispiramy, anche al 100%. Se il rimborso
    non è del 100%, il resto della sola consulenza (meno la commissione) va
    al consulente. Se il rimborso al cliente non riesce, solleva: la
    contestazione deve restare aperta. Un trasferimento successivo che fallisce
    non annulla il rimborso già partito: la quota del consulente resta ferma
    e si riprova da sola fra 6 ore, al riavvio, o dal pannello.
    """
    from decimal import Decimal

    from app.utils.problemi_transazione import accantona_quota, chiudi, quota_consulente_centesimi

    amount_cents = centesimi(booking.price)
    refund_pct = max(0, min(100, refund_pct))
    refund_cents = centesimi_rimborso_contestazione(booking, refund_pct)
    avviso = None
    errore_quota = None
    quota = 0
    if refund_pct < 100:
        consulente = session.get(User, booking.consultant_user_id)
        quota = quota_consulente_centesimi(booking, consulente, refund_pct)

    if booking.payment_method == "paypal" and booking.paypal_capture_id:
        from app.utils.paypal_config import refund_capture

        if refund_pct > 0:
            refund_amount = round(refund_cents / 100, 2)
            result = refund_capture(booking.paypal_capture_id, amount=refund_amount, currency="EUR")
            if not result:
                raise RuntimeError(f"rimborso PayPal non riuscito per booking {booking.id}")
            booking.refund_amount = Decimal(str(refund_amount))
            logger.info(f"💸 Rimborso PayPal {refund_pct}% (€{refund_amount:.2f}) per booking {booking.id}")
        if refund_pct < 100 and quota > 0:
            try:
                avviso = _paga_resto_consulente_paypal(session, booking, amount_cents, refund_pct)
                if not avviso:
                    errore_quota = "Il consulente non ha un conto PayPal collegato"
            except Exception as e:  # noqa: BLE001
                logger.error(f"❌ Pagamento parziale PayPal non riuscito per booking {booking.id}: {e}")
                errore_quota = str(e)
    elif booking.stripe_payment_intent_id:
        import stripe
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

        if refund_pct > 0:
            refund_amount_cents = refund_cents
            refund = stripe.Refund.create(
                payment_intent=booking.stripe_payment_intent_id,
                amount=refund_amount_cents,
                idempotency_key=f"refund-{booking.id}-{refund_amount_cents}",
            )
            booking.refund_amount = Decimal(str(refund_amount_cents / 100))
            logger.info(
                f"💸 Rimborso {refund_pct}% (€{refund_amount_cents / 100:.2f}) "
                f"per booking {booking.id}, refund {refund.id}"
            )
        if refund_pct < 100 and quota > 0:
            try:
                avviso = _paga_resto_consulente_stripe(session, booking, amount_cents, refund_pct)
                if not avviso:
                    errore_quota = "Il consulente non ha un conto Stripe collegato"
            except Exception as e:  # noqa: BLE001
                logger.error(f"❌ Pagamento parziale Stripe non riuscito per booking {booking.id}: {e}")
                errore_quota = str(e)
    elif refund_pct > 0:
        raise RuntimeError(f"nessun pagamento da rimborsare per booking {booking.id}")

    booking.payment_status = "refunded" if refund_pct == 100 else "partially_refunded"
    session.add(booking)
    session.commit()
    # Dopo il commit: con il database ancora occupato dalla contestazione,
    # l'avviso al consulente su SQLite non partiva. La quota non partita si
    # annota qui, così il rimborso al cliente resta valido.
    if errore_quota and quota > 0:
        accantona_quota(booking.id, quota, errore_quota)
    elif avviso:
        chiudi(booking.id, "transfer", "Quota inviata al consulente.")
        from app.utils.notification_service import send_notification
        user_id, transfer_amount = avviso
        send_notification(
            user_id=user_id,
            type_key="payment_released",
            title="Pagamento parziale ricevuto",
            message=(
                f"Hai ricevuto €{transfer_amount / 100:.2f} per la consulenza #{booking.id} "
                f"(rimborso parziale {refund_pct}% al cliente)."
            ),
            related_booking_id=booking.id,
        )


def _paga_resto_consulente_stripe(session, booking, amount_cents: int, refund_pct: int) -> None:
    import stripe

    remaining_cents = amount_cents - int(amount_cents * refund_pct / 100)
    consultant = session.get(User, booking.consultant_user_id)
    if not consultant or not consultant.stripe_account_id or remaining_cents <= 0:
        return None
    fee_pct = percentuale_commissione(consultant)
    transfer_amount = remaining_cents - int(remaining_cents * fee_pct / 100)
    if transfer_amount <= 0:
        return None
    transfer = stripe.Transfer.create(
        amount=transfer_amount,
        currency="eur",
        destination=consultant.stripe_account_id,
        transfer_group=f"booking_{booking.id}",
        idempotency_key=f"consultant-share-{booking.id}",
        metadata={
            "booking_id": str(booking.id),
            "partial_payment": "true",
            "refund_percentage": str(refund_pct),
        },
    )
    booking.stripe_transfer_id = transfer.id
    booking.payment_released_at = now_italy_naive()
    logger.info(f"💰 Pagamento parziale €{transfer_amount / 100:.2f} al consulente per booking {booking.id}")
    return consultant.id, transfer_amount


def _paga_resto_consulente_paypal(session, booking, amount_cents: int, refund_pct: int) -> None:
    from app.utils.paypal_config import create_payout

    remaining_cents = amount_cents - int(amount_cents * refund_pct / 100)
    consultant = session.get(User, booking.consultant_user_id)
    if not consultant or not consultant.paypal_email or remaining_cents <= 0:
        return None
    fee_pct = percentuale_commissione(consultant)
    transfer_amount = remaining_cents - int(remaining_cents * fee_pct / 100)
    if transfer_amount <= 0:
        return None
    payout = create_payout(
        recipient_email=consultant.paypal_email,
        amount=round(transfer_amount / 100, 2),
        currency="EUR",
        note=f"Pagamento parziale consulenza #{booking.id}",
        sender_item_id=f"booking_{booking.id}_partial",
    )
    if not payout:
        raise RuntimeError("payout PayPal vuoto")
    booking.paypal_payout_id = payout.get("batch_header", {}).get("payout_batch_id", "")
    booking.payment_released_at = now_italy_naive()
    logger.info(f"💰 PayPal payout parziale €{transfer_amount / 100:.2f} al consulente per booking {booking.id}")
    return consultant.id, transfer_amount


def avvisa_decisione_contestazione(session, dispute, booking, status: str, refund_pct: Optional[int]) -> None:
    """Cliente e consulente ricevono la stessa decisione, in app e per email."""
    from app.utils.notification_service import send_notification

    data = ""
    ora = ""
    if booking and booking.booking_date:
        data = f"{booking.booking_date:%d/%m/%Y}"
    if booking and booking.start_time:
        ora = booking.start_time
    base = os.getenv("BASE_URL", "http://localhost:8080")

    if status == "rejected":
        titolo = "Contestazione non accolta"
        cliente_it = f"La contestazione sulla consulenza del {data} non è stata accolta. Il compenso resta al consulente."
        cliente_en = f"The dispute about the consultation on {data} was not upheld. The consultant keeps the payout."
        consulente_it = f"La contestazione sulla consulenza del {data} non è stata accolta. Il compenso ti viene inviato."
        consulente_en = f"The dispute about the consultation on {data} was not upheld. Your payout is being sent."
    else:
        pct = 100 if refund_pct is None else refund_pct
        if pct >= 100:
            titolo = "Contestazione accolta"
            cliente_it = (
                f"La contestazione sulla consulenza del {data} è stata accolta. "
                "Ti rimborsiamo il prezzo della consulenza. Le spese di servizio restano a Ispiramy."
            )
            cliente_en = (
                f"The dispute about the consultation on {data} was upheld. "
                "You get back the consultation amount. The service fee stays with Ispiramy."
            )
            consulente_it = (
                f"La contestazione sulla consulenza del {data} è stata accolta. "
                "Il cliente viene rimborsato del prezzo della consulenza e il compenso non viene inviato."
            )
            consulente_en = (
                f"The dispute about the consultation on {data} was upheld. "
                "The client is refunded the consultation amount and the payout is not sent."
            )
        elif pct <= 0:
            titolo = "Contestazione chiusa"
            cliente_it = f"La contestazione sulla consulenza del {data} è stata chiusa senza rimborso."
            cliente_en = f"The dispute about the consultation on {data} was closed with no refund."
            consulente_it = f"La contestazione sulla consulenza del {data} è stata chiusa senza rimborso. Il compenso ti viene inviato."
            consulente_en = f"The dispute about the consultation on {data} was closed with no refund. Your payout is being sent."
        else:
            titolo = "Contestazione chiusa"
            cliente_it = (
                f"La contestazione sulla consulenza del {data} è stata chiusa con un rimborso "
                f"del {pct}% del prezzo della consulenza. Le spese di servizio restano a Ispiramy."
            )
            cliente_en = (
                f"The dispute about the consultation on {data} was closed with a {pct}% refund "
                "of the consultation amount. The service fee stays with Ispiramy."
            )
            consulente_it = (
                f"La contestazione sulla consulenza del {data} è stata chiusa con un rimborso "
                f"del {pct}% del prezzo della consulenza. Il resto del compenso ti viene inviato."
            )
            consulente_en = (
                f"The dispute about the consultation on {data} was closed with a {pct}% refund "
                "of the consultation amount. The rest of your payout is being sent."
            )

    def manda(user_id, messaggio, inglese):
        utente = session.get(User, user_id) if user_id else None
        if not utente:
            return
        nome = f"{utente.nome or ''} {utente.cognome or ''}".strip() or (utente.email or "")
        send_notification(
            user_id=utente.id,
            type_key="dispute_decided",
            title=titolo,
            message=messaggio,
            template_data={
                "user_name": nome,
                "date": data,
                "time": ora,
                "outcome_it": messaggio,
                "outcome_en": inglese,
                "action_url": f"{base}/profile#bookings",
            },
            related_booking_id=booking.id if booking else dispute.booking_id,
            action_url="/profile#bookings",
        )

    manda(dispute.client_user_id, cliente_it, cliente_en)
    manda(dispute.consultant_user_id, consulente_it, consulente_en)


@router.post("/api/disputes/{dispute_id}/message")
async def admin_send_dispute_message(dispute_id: int, data: DisputeMessageRequest, request: Request):
    """Admin invia un messaggio nella contestazione"""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")

    with Session(engine) as session:
        dispute = session.get(Dispute, dispute_id)
        if not dispute:
            raise HTTPException(status_code=404, detail="Contestazione non trovata")

        msg = DisputeMessage(
            dispute_id=dispute_id,
            sender_user_id=admin_user.id,
            is_admin=True,
            message=data.message,
            created_at=now_italy_naive(),
        )
        session.add(msg)

        # Se era "open", passa a "in_review" 
        if dispute.status == "open":
            dispute.status = "in_review"
            dispute.updated_at = now_italy_naive()

        session.commit()

        logger.info(f"📩 [admin] Messaggio inviato per contestazione #{dispute_id} da admin {admin_user.id}")

    return JSONResponse({"success": True, "message": "Messaggio inviato"})


@router.post("/api/disputes/{dispute_id}/status")
async def admin_update_dispute_status(dispute_id: int, data: DisputeStatusRequest, request: Request):
    """Admin aggiorna lo stato di una contestazione"""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")

    if data.status not in ("open", "in_review", "resolved", "rejected"):
        raise HTTPException(status_code=400, detail="Stato non valido")

    with Session(engine) as session:
        dispute = session.get(Dispute, dispute_id)
        if not dispute:
            raise HTTPException(status_code=404, detail="Contestazione non trovata")

        booking = session.get(Booking, dispute.booking_id) if data.status in ("resolved", "rejected") else None
        refund_pct = None

        # Il rimborso parte prima di segnare la contestazione come chiusa: se
        # Stripe o PayPal non rispondono, resta aperta e si può riprovare.
        # Segnarla chiusa prima lasciava il denaro trattenuto per sempre,
        # perché il rilascio era già stato annullato all'apertura.
        if data.status == "resolved":
            refund_pct = 100 if data.refund_percentage is None else data.refund_percentage
            refund_pct = max(0, min(100, refund_pct))
            if booking and booking.payment_status == "held" and refund_pct > 0:
                try:
                    applica_rimborso_contestazione(session, booking, refund_pct)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"❌ Rimborso contestazione #{dispute_id} non riuscito: {e}")
                    raise HTTPException(
                        status_code=502,
                        detail="Il rimborso non è riuscito. La contestazione resta aperta.",
                    )

        dispute.status = data.status
        dispute.updated_at = now_italy_naive()
        session.add(dispute)
        session.commit()
        logger.info(f"🔄 [admin] Contestazione #{dispute_id} aggiornata a '{data.status}' da admin {admin_user.id}")

        if data.status == "rejected" and booking and booking.payment_status == "held":
            # Respinta → paga il consulente anche senza recensione e prima
            # delle 48 ore. La contestazione è già chiusa, quindi non blocca.
            from app.scheduler import release_booking_payment
            release_booking_payment(booking.id, forza=True)
            logger.info(f"💰 Contestazione #{dispute_id} respinta: pagamento rilasciato per booking {booking.id}")
        elif data.status == "resolved" and refund_pct == 0 and booking and booking.payment_status == "held":
            from app.scheduler import release_booking_payment
            release_booking_payment(booking.id, forza=True)

        if data.status in ("resolved", "rejected"):
            avvisa_decisione_contestazione(session, dispute, booking, data.status, refund_pct)

    return JSONResponse({"success": True, "message": f"Stato aggiornato a {data.status}"})


# ==================== GESTIONE UTENTI ====================

@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    """Ricerca e gestione utenti"""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    search = request.query_params.get("q", "").strip()

    with Session(engine) as session:
        if search:
            query = select(User).where(
                (User.email.ilike(f"%{search}%")) |
                (User.nome.ilike(f"%{search}%")) |
                (User.cognome.ilike(f"%{search}%"))
            ).order_by(User.created_at.desc()).limit(50)
        else:
            query = select(User).order_by(User.created_at.desc()).limit(50)

        users_raw = session.exec(query).all()

        tutte_categorie = [
            {"id": c.id, "name": c.name, "icon": c.icon or ""}
            for c in session.exec(select(Category).order_by(Category.name)).all()
        ]

        users = []
        for u in users_raw:
            # Conta consulenze e recensioni
            booking_count = session.exec(
                select(func.count(Booking.id)).where(
                    (Booking.client_user_id == u.id) | (Booking.consultant_user_id == u.id)
                )
            ).one()
            review_count = session.exec(
                select(func.count(Review.id)).where(Review.consultant_user_id == u.id)
            ).one()
            users.append({
                "id": u.id,
                "email": u.email,
                "nome": u.nome or "",
                "cognome": u.cognome or "",
                "professione": u.professione or "",
                "profile_picture": foto_pubblica(u.profile_picture) or None,
                "is_verified": u.is_verified,
                "category_id": u.category_id,
                "categorie_verificate": sorted(id_categorie_verificate(u)),
                "user_type_id": u.user_type_id,
                "consulenze_vendute": u.consulenze_vendute,
                "consulenze_acquistate": u.consulenze_acquistate,
                "prezzo_consulenza": u.prezzo_consulenza,
                "booking_count": booking_count,
                "review_count": review_count,
                "created_at": u.created_at,
                "last_seen": u.last_seen,
            })

        total_users = session.exec(select(func.count(User.id))).one()

    return request.app.state.templates.TemplateResponse(
        "admin/users.html",
        {
            "request": request,
            "current_user": admin_user,
            "user": admin_user,
            "users": users,
            "tutte_categorie": tutte_categorie,
            "search": search,
            "total_users": total_users,
        }
    )


class UserTypeUpdateRequest(BaseModel):
    # Solo i tre ruoli che esistono davvero: 1=Utente, 2=Verificatore, 3=Amministratore.
    # ge=1/le=10 lasciava assegnare ruoli inesistenti (es. 7), che require_admin
    # avrebbe comunque trattato come >= 2.
    user_type_id: int = Field(ge=1, le=3)


@router.post("/api/users/{user_id}/type")
async def admin_update_user_type(user_id: int, data: UserTypeUpdateRequest, request: Request):
    """Admin aggiorna il tipo utente (solo Amministratore, user_type_id == 3)"""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    if admin_user.user_type_id < 3:
        raise HTTPException(status_code=403, detail="Solo gli amministratori possono modificare il tipo utente")

    with Session(engine) as session:
        target_user = session.get(User, user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="Utente non trovato")

        old_type = target_user.user_type_id
        target_user.user_type_id = data.user_type_id
        session.commit()

        logger.info(f"👤 [admin] User #{user_id} tipo cambiato da {old_type} a {data.user_type_id} da admin {admin_user.id}")

    return JSONResponse({"success": True, "message": f"Tipo utente aggiornato a {data.user_type_id}"})


class CategorieVerificateRequest(BaseModel):
    category_ids: list[int] = Field(default_factory=list)


@router.post("/api/users/{user_id}/categorie-verificate")
async def admin_categorie_verificate(user_id: int, data: CategorieVerificateRequest, request: Request):
    """Un verificatore segna per quali categorie il badge e' visibile.

    Non e' un pulsante pubblico: lo stesso accesso del pannello (user_type_id >= 2).
    is_verified resta il lasciapassare dell'account.
    """
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")

    with Session(engine) as session:
        target_user = session.get(User, user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="Utente non trovato")
        note = {c.id for c in session.exec(select(Category)).all()}
        scelte = [cid for cid in data.category_ids if cid in note]
        scrivi_categorie_verificate(target_user, scelte)
        session.add(target_user)
        session.commit()
        logger.info(
            f"👤 [admin] Categorie verificate di #{user_id} aggiornate da admin {admin_user.id}: {scelte}"
        )

    return JSONResponse({"success": True, "category_ids": scelte})


# ========== CONSULENZE ==========

@router.get("/bookings", response_class=HTMLResponse)
async def admin_bookings(request: Request, q: str = ""):
    """Lista consulenze con ricerca per utente"""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    with Session(engine) as session:
        # Alias per client e consultant
        ClientUser = User
        ConsultantUser = User

        search = q.strip()

        if search:
            # Cerca booking dove client o consultant matchano la query
            matching_user_ids_stmt = select(User.id).where(
                (User.email.ilike(f"%{search}%")) |
                (User.nome.ilike(f"%{search}%")) |
                (User.cognome.ilike(f"%{search}%"))
            )
            matching_ids = session.exec(matching_user_ids_stmt).all()

            if matching_ids:
                bookings = session.exec(
                    select(Booking).where(
                        (Booking.client_user_id.in_(matching_ids)) |
                        (Booking.consultant_user_id.in_(matching_ids))
                    ).order_by(Booking.booking_date.desc()).limit(100)
                ).all()
            else:
                bookings = []
        else:
            bookings = session.exec(
                select(Booking).order_by(Booking.booking_date.desc()).limit(100)
            ).all()

        total_bookings = session.exec(select(func.count(Booking.id))).one()

        # Raccogli info utenti
        user_ids = set()
        for b in bookings:
            user_ids.add(b.client_user_id)
            user_ids.add(b.consultant_user_id)

        users_map = {}
        if user_ids:
            users = session.exec(select(User).where(User.id.in_(user_ids))).all()
            users_map = {u.id: u for u in users}

        bookings_data = []
        for b in bookings:
            client = users_map.get(b.client_user_id)
            consultant = users_map.get(b.consultant_user_id)
            bookings_data.append({
                "id": b.id,
                "booking_date": b.booking_date,
                "start_time": b.start_time,
                "end_time": b.end_time,
                "duration_minutes": b.duration_minutes,
                "status": b.status,
                "payment_status": b.payment_status,
                "price": b.price,
                "recording_status": b.recording_status,
                "recording_filename": b.recording_filename,
                "client_name": f"{client.nome} {client.cognome}" if client else "N/D",
                "client_email": client.email if client else "",
                "consultant_name": f"{consultant.nome} {consultant.cognome}" if consultant else "N/D",
                "consultant_email": consultant.email if consultant else "",
            })

    return request.app.state.templates.TemplateResponse("admin/bookings.html", {
        "request": request,
        "user": admin_user,
        "current_user": admin_user,
        "bookings": bookings_data,
        "search": search,
        "total_bookings": total_bookings,
    })


def _nome_persona(utente) -> str:
    if not utente:
        return "—"
    nome = f"{utente.nome or ''} {utente.cognome or ''}".strip()
    return nome or utente.email or "—"


@router.get("/transaction-problems", response_class=HTMLResponse)
async def admin_transaction_problems(request: Request):
    """Compensi, rimborsi e trasferimenti che non sono partiti e vanno riprovati."""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    with Session(engine) as session:
        righe = session.exec(
            select(TransactionProblem)
            .where(TransactionProblem.status == "open")
            .order_by(TransactionProblem.updated_at.desc(), TransactionProblem.id.desc())
        ).all()
        problemi = []
        for riga in righe:
            booking = session.get(Booking, riga.booking_id)
            cliente = session.get(User, booking.client_user_id) if booking else None
            consulente = session.get(User, booking.consultant_user_id) if booking else None
            problemi.append({
                "id": riga.id,
                "booking_id": riga.booking_id,
                "kind": riga.kind,
                "amount_eur": f"{(riga.amount_cents or 0) / 100:.2f}",
                "error_reason": riga.error_reason or "",
                "attempts": riga.attempts or 0,
                "last_attempt_at": riga.last_attempt_at,
                "next_attempt_at": riga.next_attempt_at,
                "client_name": _nome_persona(cliente),
                "consultant_name": _nome_persona(consulente),
            })

    return request.app.state.templates.TemplateResponse(
        "admin/transaction_problems.html",
        {
            "request": request,
            "user": admin_user,
            "current_user": admin_user,
            "problemi": problemi,
        },
    )


class ResolveProblemRequest(BaseModel):
    note: str = ""


@router.post("/api/transaction-problems/{problem_id}/retry")
async def admin_retry_transaction_problem(problem_id: int, request: Request):
    """Lo stesso tentativo che scatterebbe da solo fra 6 ore, fatto subito."""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")

    from app.utils.problemi_transazione import riprova_adesso
    esito = riprova_adesso(problem_id)
    if esito == "manca":
        raise HTTPException(status_code=404, detail="Problema non trovato")
    return JSONResponse({"success": True, "esito": esito})


@router.post("/api/transaction-problems/{problem_id}/resolve")
async def admin_resolve_transaction_problem(problem_id: int, data: ResolveProblemRequest, request: Request):
    """Chiude il problema senza mandare altro denaro. La nota è obbligatoria."""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")

    from app.utils.problemi_transazione import segna_risolto
    esito = segna_risolto(problem_id, admin_user.id, data.note)
    if esito == "nota":
        raise HTTPException(status_code=400, detail="Scrivi una nota: serve a ricordare come è stato chiuso.")
    if esito == "manca":
        raise HTTPException(status_code=404, detail="Problema non trovato")
    return JSONResponse({"success": True, "esito": esito})


@router.get("/api/bookings/{booking_id}/download")
async def admin_download_recording(booking_id: int, request: Request):
    """Genera presigned URL S3 per scaricare la registrazione MP4"""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")

    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Consulenza non trovata")

        if not booking.recording_filename:
            raise HTTPException(status_code=404, detail="Nessuna registrazione disponibile")

        import boto3, os
        s3_client = boto3.client(
            's3',
            region_name=os.getenv("AWS_S3_REGION", "eu-south-1"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        bucket = os.getenv("AWS_S3_BUCKET_NAME")

        # Cerca il file MP4 nella cartella booking_{id}/
        mp4_key = None
        prefixes_to_try = [
            f"booking_{booking.id}/",
            f"{booking.recording_filename}/" if booking.recording_filename else None,
        ]
        for prefix in prefixes_to_try:
            if not prefix:
                continue
            try:
                logger.info(f"🔍 Cercando MP4 in S3: bucket={bucket}, prefix={prefix}")
                resp = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
                for obj in resp.get("Contents", []):
                    logger.info(f"   Trovato: {obj['Key']}")
                    if obj["Key"].endswith(".mp4"):
                        mp4_key = obj["Key"]
                        break
                if mp4_key:
                    break
            except Exception as e:
                logger.error(f"Errore listing S3 per booking {booking.id} con prefix {prefix}: {e}")

        if not mp4_key:
            raise HTTPException(status_code=404, detail="File MP4 non trovato su S3")

        try:
            # 1 ora, non 7 giorni: è la registrazione di una consulenza privata
            # e il link firmato è utilizzabile da chiunque lo riceva.
            url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': mp4_key},
                ExpiresIn=3600,
            )
        except Exception as e:
            logger.error(f"Errore presigned URL per booking {booking.id}: {e}")
            raise HTTPException(status_code=500, detail="Errore nella generazione del link di download")

        return JSONResponse({"url": url})


# ==================== AI ANALYSIS ====================

@router.post("/api/disputes/{dispute_id}/ai-analysis")
async def admin_ai_analysis(dispute_id: int, request: Request):
    """Analizza il video della consulenza con Gemini AI per valutare la contestazione"""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")

    with Session(engine) as session:
        dispute = session.get(Dispute, dispute_id)
        if not dispute:
            raise HTTPException(status_code=404, detail="Contestazione non trovata")

        booking = session.get(Booking, dispute.booking_id)
        if not booking or not booking.recording_filename:
            raise HTTPException(status_code=404, detail="Nessuna registrazione video disponibile per questa consulenza")

        client = session.get(User, dispute.client_user_id)
        consultant = session.get(User, dispute.consultant_user_id)

        client_name = f"{client.nome or ''} {client.cognome or ''}".strip() if client else "?"
        consultant_name = f"{consultant.nome or ''} {consultant.cognome or ''}".strip() if consultant else "?"
        booking_date = booking.booking_date.strftime('%d/%m/%Y') if booking.booking_date else "?"
        booking_time = f"{booking.start_time} - {booking.end_time}" if booking.start_time else "?"

        import os
        video_path = None
        try:
            from app.utils.gemini_analysis import download_video_from_s3, analyze_dispute_video

            # 1. Scarica video da S3.
            # Download e analisi sono sincroni e possono durare minuti: eseguiti
            # sull'event loop bloccherebbero ogni altra richiesta all'app.
            video_path = await asyncio.to_thread(download_video_from_s3, booking.id)

            # 1b. Costruisci la trascrizione della chat testuale della call (con allegati)
            import json as _json
            from app.models import CallMessage
            chat_msgs = session.exec(
                select(CallMessage)
                .where(CallMessage.booking_id == booking.id)
                .order_by(CallMessage.created_at)
            ).all()
            _lines = []
            for m in chat_msgs:
                autore = consultant_name if m.user_id == dispute.consultant_user_id else client_name
                ts = m.created_at.strftime('%H:%M') if m.created_at else ''
                testo = (m.message or '').strip()
                allegati = ''
                if m.attachments:
                    try:
                        _atts = _json.loads(m.attachments)
                        if _atts:
                            _nomi = ', '.join(a.get('filename', 'allegato') for a in _atts)
                            allegati = f" [allegati inviati: {_nomi}]"
                    except Exception:
                        allegati = " [allegato inviato]"
                _lines.append(f"[{ts}] {autore}: {testo}{allegati}".rstrip())
            chat_transcript = "\n".join(_lines)

            # 2. Analizza con Gemini (video + chat)
            result = await asyncio.to_thread(
                analyze_dispute_video,
                video_path=video_path,
                booking_description=booking.description,
                dispute_description=dispute.description,
                consultant_name=consultant_name,
                client_name=client_name,
                booking_date=booking_date,
                booking_time=booking_time,
                chat_transcript=chat_transcript,
            )

            # 3. Salva risultati nel DB
            dispute.ai_verdict = result["verdict"]
            dispute.ai_confidence = result["confidence"]
            dispute.ai_comment = result["comment"]
            dispute.ai_analyzed_at = now_italy_naive()
            session.add(dispute)
            session.commit()

            return JSONResponse({
                "success": True,
                "verdict": result["verdict"],
                "confidence": result["confidence"],
                "comment": result["comment"],
            })

        except Exception as e:
            logger.error(f"Errore analisi AI contestazione {dispute_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Errore nell'analisi AI: {str(e)}")
        finally:
            # Cleanup file temporaneo
            if video_path:
                try:
                    os.unlink(video_path)
                except Exception:
                    pass


def conta_segnalazioni_aperte() -> int:
    """Quante domande segnalate aspettano ancora una decisione."""
    with Session(engine) as session:
        return session.exec(
            select(func.count()).select_from(CommunityReport).where(CommunityReport.status == "open")
        ).one() or 0


@router.get("/reports", response_class=HTMLResponse)
async def admin_reports(request: Request):
    """Segnalazioni della community, per chi verifica e per chi amministra."""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    with Session(engine) as session:
        righe = session.exec(
            select(CommunityReport)
            .where(CommunityReport.status == "open")
            .order_by(CommunityReport.created_at.desc(), CommunityReport.id.desc())
        ).all()
        segnalazioni = []
        for riga in righe:
            domanda = session.get(CommunityQuestion, riga.question_id)
            autore = session.get(User, domanda.user_id) if domanda else None
            segnalatore = session.get(User, riga.user_id)
            segnalazioni.append({
                "id": riga.id,
                "question_id": riga.question_id,
                "title": (domanda.title if domanda else "—") or "—",
                "excerpt": ((domanda.description or "")[:180] if domanda else ""),
                "hidden_already": bool(domanda and not domanda.validation),
                "author_name": _nome_persona(autore),
                "reporter_name": _nome_persona(segnalatore),
                "reason": riga.reason or "",
                "created_at": riga.created_at,
            })

    return request.app.state.templates.TemplateResponse(
        "admin/reports.html",
        {
            "request": request,
            "user": admin_user,
            "current_user": admin_user,
            "segnalazioni": segnalazioni,
        },
    )


def _chiudi_segnalazione(report_id: int, admin_user: User, nascondi: bool) -> str:
    """dismiss lascia la domanda visibile. hide la toglie, come un rifiuto dell'AI."""
    with Session(engine) as session:
        riga = session.get(CommunityReport, report_id)
        if not riga or riga.status != "open":
            return "manca"
        adesso = now_italy_naive()
        if nascondi:
            domanda = session.get(CommunityQuestion, riga.question_id)
            if domanda:
                domanda.validation = False
                session.add(domanda)
            aperte = session.exec(
                select(CommunityReport).where(
                    CommunityReport.question_id == riga.question_id,
                    CommunityReport.status == "open",
                )
            ).all()
            for altra in aperte:
                altra.status = "hidden"
                altra.resolved_at = adesso
                altra.resolved_by = admin_user.id
                session.add(altra)
        else:
            riga.status = "dismissed"
            riga.resolved_at = adesso
            riga.resolved_by = admin_user.id
            session.add(riga)
        session.commit()
    return "ok"


@router.post("/api/reports/{report_id}/dismiss")
async def admin_dismiss_report(report_id: int, request: Request):
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    esito = _chiudi_segnalazione(report_id, admin_user, nascondi=False)
    if esito == "manca":
        raise HTTPException(status_code=404, detail="Segnalazione non trovata")
    return JSONResponse({"success": True})


@router.post("/api/reports/{report_id}/hide")
async def admin_hide_report(report_id: int, request: Request):
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    esito = _chiudi_segnalazione(report_id, admin_user, nascondi=True)
    if esito == "manca":
        raise HTTPException(status_code=404, detail="Segnalazione non trovata")
    return JSONResponse({"success": True})
