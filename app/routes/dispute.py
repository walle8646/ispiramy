"""
Routes per il sistema di contestazioni post-consulenza.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import BaseModel, Field

import os

from app.database import engine
from app.models import Dispute, DisputeMessage, Booking, User
from app.routes.auth import get_current_user
from app.logger_config import logger
from app.utils.orari import now_italy_naive

router = APIRouter()

ITALY_TZ = ZoneInfo("Europe/Rome")


class DisputeRequest(BaseModel):
    description: str = Field(min_length=20, max_length=5000)


@router.post("/api/booking/{booking_id}/dispute")
async def open_dispute(booking_id: int, data: DisputeRequest, request: Request):
    """Apri una contestazione per una consulenza"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")

    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")

        if current_user.id != booking.client_user_id:
            raise HTTPException(status_code=403, detail="Solo il cliente può aprire una contestazione")

        # Controlla se esiste già una contestazione per questo booking
        existing = session.exec(
            select(Dispute).where(Dispute.booking_id == booking_id)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Hai già aperto una contestazione per questa consulenza")

        dispute = Dispute(
            booking_id=booking_id,
            client_user_id=current_user.id,
            consultant_user_id=booking.consultant_user_id,
            description=data.description,
            status="open",
            created_at=now_italy_naive(),
        )
        session.add(dispute)
        session.commit()
        session.refresh(dispute)

        logger.info(f"🚨 [dispute] Contestazione #{dispute.id} aperta per booking {booking_id} da user {current_user.id}")

        # Né la recensione né la scadenza di riserva devono pagare mentre si
        # discute: se scattassero nello stesso momento in cui si apre la
        # contestazione, il compenso partirebbe lo stesso. Resta trattenuto
        # finché un amministratore non decide.
        try:
            from app.scheduler import cancel_payment_release
            cancel_payment_release(booking_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"⚠️ Rilascio pagamento non annullato per booking {booking_id}: {e}")

        # Il consulente deve saperlo: il suo compenso resta trattenuto e
        # potrebbe dover rispondere. Prima lo vedeva solo l'amministratore.
        avvisa_consulente(session, booking, current_user, data.description)

        return JSONResponse({"success": True, "dispute_id": dispute.id, "message": "Contestazione aperta con successo"})


def avvisa_consulente(session, booking, cliente, descrizione: str) -> None:
    """Notifica ed email al consulente per una contestazione appena aperta."""
    from html import escape

    from app.utils.notification_service import send_notification

    consulente = session.get(User, booking.consultant_user_id)
    nome_cliente = f"{cliente.nome} {cliente.cognome or ''}".strip() if cliente.nome else "Il cliente"
    nome_consulente = f"{consulente.nome} {consulente.cognome or ''}".strip() if consulente and consulente.nome else "Consulente"
    data = f"{booking.booking_date:%d/%m/%Y}"

    # Il testo lo scrive il cliente e finisce in un'email HTML: va escapato
    motivo = (descrizione or "").strip()
    sezione_motivo = ""
    if motivo:
        if len(motivo) > 600:
            motivo = motivo[:600].rstrip() + "…"
        sezione_motivo = (
            '<div style="background:#f9fafb;border-left:4px solid #9ca3af;border-radius:6px;'
            'padding:14px 18px;margin:20px 0;">'
            f"<p style='margin:0 0 6px;'><strong>Cosa segnala il cliente:</strong></p>"
            f"<p style='margin:0;'>{escape(motivo)}</p></div>"
        )

    try:
        send_notification(
            user_id=booking.consultant_user_id,
            type_key="dispute_opened",
            title="Contestazione aperta",
            message=f"{nome_cliente} ha aperto una contestazione sulla consulenza del {data}",
            template_data={
                "consultant_name": nome_consulente,
                "client_name": nome_cliente,
                "date": data,
                "time": booking.start_time,
                "reason_section": sezione_motivo,
                "action_url": f"{os.getenv('BASE_URL', 'http://localhost:8080')}/profile#bookings",
            },
            related_booking_id=booking.id,
            related_user_id=cliente.id,
            action_url="/profile#bookings",
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Avviso di contestazione non inviato al consulente: {e}")
