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
from app.models import User, Booking, Dispute, DisputeMessage, Review, CommunityQuestion
from app.routes.auth import verify_token
from app.logger_config import logger
from app.utils.orari import now_italy_naive
from app.utils.prezzi import centesimi, totale_pagato

router = APIRouter(prefix="/admin")

ITALY_TZ = ZoneInfo("Europe/Rome")


def require_admin(request: Request) -> User:
    """Verifica che l'utente sia admin (user_type_id >= 2). Ritorna l'utente o lancia eccezione."""
    user = verify_token(request)
    if not user:
        return None
    if user.user_type_id < 2:
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
                "profile_picture": client.profile_picture if client else None,
            },
            "consultant": {
                "id": consultant.id if consultant else None,
                "name": f"{consultant.nome or ''} {consultant.cognome or ''}".strip() if consultant else "?",
                "email": consultant.email if consultant else "?",
                "profile_picture": consultant.profile_picture if consultant else None,
                "professione": consultant.professione if consultant else None,
                "platform_fee_percent": consultant.platform_fee_percent if consultant else 20,
            },
            "booking": {
                "date": booking.booking_date if booking else None,
                "start_time": booking.start_time if booking else None,
                "end_time": booking.end_time if booking else None,
                "price": float(booking.price) if booking and booking.price else 0,
                "status": booking.status if booking else None,
                "payment_status": booking.payment_status if booking else None,
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

        dispute.status = data.status
        dispute.updated_at = now_italy_naive()
        session.commit()

        logger.info(f"🔄 [admin] Contestazione #{dispute_id} aggiornata a '{data.status}' da admin {admin_user.id}")
        
        # Se la contestazione è stata risolta o respinta, gestisci il pagamento
        if data.status in ("resolved", "rejected"):
            booking = session.get(Booking, dispute.booking_id)
            if booking and booking.payment_status == "held":
                if data.status == "rejected":
                    # Contestazione respinta → paga il consulente (100%)
                    from app.scheduler import release_booking_payment
                    release_booking_payment(booking.id)
                    logger.info(f"💰 Contestazione #{dispute_id} respinta: pagamento rilasciato per booking {booking.id}")
                elif data.status == "resolved":
                    # Contestazione risolta → rimborso (parziale o totale)
                    refund_pct = data.refund_percentage if data.refund_percentage is not None else 100
                    refund_pct = max(0, min(100, refund_pct))
                    
                    # Due basi diverse: al cliente si rimborsa anche la parte di
                    # spese di servizio che ha pagato, al consulente spetta solo
                    # la quota sul valore della consulenza.
                    amount_cents = int(float(booking.price) * 100)
                    totale_cents = centesimi(totale_pagato(booking))

                    if booking.payment_method == "paypal" and booking.paypal_capture_id:
                        # === PayPal refund/payout ===
                        try:
                            from app.utils.paypal_config import refund_capture, create_payout
                            
                            if refund_pct > 0:
                                refund_amount = round(totale_cents * refund_pct / 100 / 100, 2)
                                result = refund_capture(booking.paypal_capture_id, amount=refund_amount, currency="EUR")
                                if result:
                                    from decimal import Decimal
                                    booking.refund_amount = Decimal(str(refund_amount))
                                    logger.info(f"💸 Rimborso PayPal {refund_pct}% (€{refund_amount:.2f}) per booking {booking.id}")
                                else:
                                    logger.error(f"❌ Errore rimborso PayPal per booking {booking.id}")
                            
                            if refund_pct < 100:
                                remaining_cents = amount_cents - int(amount_cents * refund_pct / 100)
                                consultant = session.get(User, booking.consultant_user_id)
                                
                                if consultant and consultant.paypal_email:
                                    fee_pct = consultant.platform_fee_percent if consultant.platform_fee_percent else 20
                                    transfer_amount = remaining_cents - int(remaining_cents * fee_pct / 100)
                                    
                                    if transfer_amount > 0:
                                        payout = create_payout(
                                            recipient_email=consultant.paypal_email,
                                            amount=round(transfer_amount / 100, 2),
                                            currency="EUR",
                                            note=f"Pagamento parziale consulenza #{booking.id}",
                                            sender_item_id=f"booking_{booking.id}_partial"
                                        )
                                        if payout:
                                            payout_id = payout.get("batch_header", {}).get("payout_batch_id", "")
                                            booking.paypal_payout_id = payout_id
                                            booking.payment_released_at = now_italy_naive()
                                            logger.info(f"💰 PayPal payout parziale €{transfer_amount/100:.2f} al consulente per booking {booking.id}")
                                            
                                            from app.utils.notification_service import send_notification
                                            send_notification(
                                                user_id=consultant.id,
                                                type_key="payment_released",
                                                title="Pagamento parziale ricevuto",
                                                message=f"Hai ricevuto €{transfer_amount/100:.2f} per la consulenza #{booking.id} (rimborso parziale {refund_pct}% al cliente)."
                                            )
                            
                            booking.payment_status = "refunded" if refund_pct == 100 else "partially_refunded"
                            session.add(booking)
                            session.commit()
                            
                        except Exception as e:
                            logger.error(f"❌ Errore rimborso/payout PayPal booking {booking.id}: {e}")
                    
                    elif booking.stripe_payment_intent_id:
                        # === Stripe refund/transfer ===
                        try:
                            import stripe
                            stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
                            
                            if refund_pct > 0:
                                # Rimborso (parziale o totale) al cliente, spese di servizio comprese
                                refund_amount_cents = int(totale_cents * refund_pct / 100)
                                refund = stripe.Refund.create(
                                    payment_intent=booking.stripe_payment_intent_id,
                                    amount=refund_amount_cents
                                )
                                from decimal import Decimal
                                booking.refund_amount = Decimal(str(refund_amount_cents / 100))
                                logger.info(f"💸 Rimborso {refund_pct}% (€{refund_amount_cents/100:.2f}) per booking {booking.id}, refund {refund.id}")
                            
                            if refund_pct < 100:
                                # Pagamento parziale al consulente (parte non rimborsata - fee piattaforma)
                                remaining_cents = amount_cents - int(amount_cents * refund_pct / 100)
                                consultant = session.get(User, booking.consultant_user_id)
                                
                                if consultant and consultant.stripe_account_id:
                                    fee_pct = consultant.platform_fee_percent if consultant.platform_fee_percent else 20
                                    transfer_amount = remaining_cents - int(remaining_cents * fee_pct / 100)
                                    
                                    if transfer_amount > 0:
                                        transfer = stripe.Transfer.create(
                                            amount=transfer_amount,
                                            currency="eur",
                                            destination=consultant.stripe_account_id,
                                            transfer_group=f"booking_{booking.id}",
                                            metadata={
                                                "booking_id": str(booking.id),
                                                "partial_payment": "true",
                                                "refund_percentage": str(refund_pct),
                                            }
                                        )
                                        booking.stripe_transfer_id = transfer.id
                                        booking.payment_released_at = now_italy_naive()
                                        logger.info(f"💰 Pagamento parziale €{transfer_amount/100:.2f} al consulente per booking {booking.id}")
                                        
                                        from app.utils.notification_service import send_notification
                                        send_notification(
                                            user_id=consultant.id,
                                            type_key="payment_released",
                                            title="Pagamento parziale ricevuto",
                                            message=f"Hai ricevuto €{transfer_amount/100:.2f} per la consulenza #{booking.id} (rimborso parziale {refund_pct}% al cliente)."
                                        )
                            
                            booking.payment_status = "refunded" if refund_pct == 100 else "partially_refunded"
                            session.add(booking)
                            session.commit()
                            
                        except Exception as e:
                            logger.error(f"❌ Errore rimborso/trasferimento booking {booking.id}: {e}")

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
                "profile_picture": u.profile_picture,
                "is_verified": u.is_verified,
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


@router.get("/api/bookings/{booking_id}/download")
async def admin_download_recording(booking_id: int, request: Request):
    """Genera presigned URL S3 per scaricare la registrazione MP4"""
    admin_user = require_admin(request)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    # Il video di una consulenza e' il contenuto piu' sensibile della
    # piattaforma: require_admin ammette anche i verificatori (livello 2),
    # qui serve un amministratore vero.
    if admin_user.user_type_id < 3:
        raise HTTPException(status_code=403, detail="Solo gli amministratori possono scaricare le registrazioni")

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
    # Manda il video della consulenza a un servizio esterno: solo amministratori.
    if admin_user.user_type_id < 3:
        raise HTTPException(status_code=403, detail="Solo gli amministratori possono avviare l'analisi AI")

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
