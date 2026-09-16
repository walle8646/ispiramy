"""
PayPal Payment Routes
Handles PayPal checkout flow: create order → user approves → capture → create booking
"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlmodel import Session, select
from sqlalchemy import func
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import json

from app.database import engine
from app.models import Booking, User, ConsultationOffer
from app.utils.paypal_config import authorize_order, create_order, capture_order, is_configured
from app.utils.prezzi import spese_servizio, totale_cliente
from app.utils.booking_requests import (
    conferma_consulenza, metti_in_attesa, richiede_accettazione, scadenza_risposta,
)
from app.routes.auth import verify_token
from app.logger_config import logger
from app.utils.notification_service import send_notification
from app.utils_user import has_payment_method
from app.utils.orari import ORE_PREAVVISO_PRENOTAZIONE
from app.scheduler import schedule_booking_reminders, schedule_payment_release, schedule_noshow_check

router = APIRouter()
ITALY_TZ = ZoneInfo("Europe/Rome")
DEBUG_MODE = os.getenv("DEBUG", "false").lower() == "true"


from app.routes.booking import BLOCKING_BOOKING_STATUSES, slot_gia_prenotato  # noqa: E402


def get_current_user(request: Request):
    return verify_token(request)


# ==================== DIRECT BOOKING ====================

@router.post("/api/booking/create-paypal")
async def create_booking_paypal(request: Request):
    """
    Crea un ordine PayPal per una prenotazione diretta.
    Crea un booking in status 'pending_payment', poi redirige l'utente a PayPal.
    """
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    if current_user.is_anonymous:
        raise HTTPException(status_code=403, detail="Non puoi prenotare in modalità anonima.")
    
    if not is_configured():
        raise HTTPException(status_code=503, detail="PayPal non configurato")
    
    booking_data = await request.json()
    consultant_id = booking_data.get('consultant_user_id')
    booking_date_str = booking_data.get('booking_date')
    start_time = booking_data.get('start_time')
    end_time = booking_data.get('end_time')
    duration_minutes = booking_data.get('duration_minutes')
    availability_block_id = booking_data.get('availability_block_id')
    client_notes = booking_data.get('client_notes', '')
    description = booking_data.get('description', '')
    community_question_id = booking_data.get('community_question_id')
    price = booking_data.get('price')
    recording_requested = booking_data.get('recording_requested', True)
    
    if not all([consultant_id, booking_date_str, start_time, end_time, duration_minutes, price]):
        raise HTTPException(status_code=400, detail="Campi obbligatori mancanti")
    
    if not community_question_id and (not description or not description.strip()):
        raise HTTPException(status_code=400, detail="Descrizione della consulenza obbligatoria")
    
    if duration_minutes not in [60, 90, 120]:
        raise HTTPException(status_code=400, detail="Durata non valida (minimo 60 minuti)")
    
    if current_user.id == consultant_id:
        raise HTTPException(status_code=400, detail="Non puoi prenotare con te stesso")
    
    with Session(engine) as session:
        consultant = session.get(User, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consulente non trovato")
        
        if not has_payment_method(consultant):
            raise HTTPException(status_code=400, detail="Il consulente non ha configurato un metodo di pagamento. Non è possibile prenotare.")
        
        try:
            booking_date = datetime.strptime(booking_date_str, '%Y-%m-%d')
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato data non valido")
        
        if not DEBUG_MODE:
            booking_datetime = datetime.strptime(f"{booking_date_str} {start_time}", '%Y-%m-%d %H:%M')
            # Gli orari di prenotazione sono ora italiana: confrontarli con utcnow()
            # rendeva il vincolo di 4 ore un vincolo di 2 ore in ora legale.
            now_italy = datetime.now(ITALY_TZ).replace(tzinfo=None)
            time_until = (booking_datetime - now_italy).total_seconds() / 3600
            if time_until < ORE_PREAVVISO_PRENOTAZIONE:
                raise HTTPException(
                    status_code=400,
                    detail=f"La consulenza deve essere prenotata almeno {ORE_PREAVVISO_PRENOTAZIONE} ore nel futuro",
                )
        
        existing = session.exec(
            select(Booking)
            .where(func.date(Booking.booking_date) == booking_date_str)
            .where(Booking.consultant_user_id == consultant_id)
            .where(Booking.start_time == start_time)
            .where(Booking.status.in_(BLOCKING_BOOKING_STATUSES))
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Questo slot è già stato prenotato")
        
        if not price or price <= 0:
            raise HTTPException(status_code=400, detail="Prezzo non valido")
        
        hourly_rate = consultant.prezzo_consulenza if consultant.prezzo_consulenza else 0
        if hourly_rate <= 0:
            raise HTTPException(status_code=400, detail="Il consulente non ha impostato un prezzo")
        expected_price = (hourly_rate / 60) * duration_minutes
        if abs(price - expected_price) > 1:
            raise HTTPException(status_code=400, detail="Prezzo non valido per la durata selezionata")
        
        # Calcola end_datetime e held_until
        booking_dt = datetime.strptime(f"{booking_date_str} {start_time}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{booking_date_str} {end_time}", "%Y-%m-%d %H:%M")
        held_until = end_dt + timedelta(hours=48)

        # Consulente che conferma a mano: l'ordine PayPal blocca l'importo
        # (intent AUTHORIZE) e si incassa solo quando accetta.
        con_accettazione = richiede_accettazione(consultant)

        # Crea booking in stato pending_payment
        new_booking = Booking(
            client_user_id=current_user.id,
            consultant_user_id=consultant_id,
            availability_block_id=int(availability_block_id) if availability_block_id else None,
            booking_date=booking_dt,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            price=price,
            status="pending_payment",
            payment_status="pending",
            payment_method="paypal",
            payment_held_until=held_until,
            acceptance_deadline=scadenza_risposta(booking_dt) if con_accettazione else None,
            service_fee=spese_servizio(),
            client_notes=client_notes or "Prenotazione diretta",
            description=description,
            community_question_id=int(community_question_id) if community_question_id else None,
            recording_requested=recording_requested if isinstance(recording_requested, bool) else str(recording_requested).lower() == 'true'
        )
        session.add(new_booking)
        session.commit()
        session.refresh(new_booking)
        
        # Crea ordine PayPal
        app_url = os.getenv("BASE_URL", "http://localhost:8080")
        order = create_order(
            # Il cliente paga consulenza + spese di servizio
            amount=float(totale_cliente(price)),
            currency="EUR",
            return_url=f"{app_url}/booking/paypal/capture?booking_id={new_booking.id}",
            cancel_url=f"{app_url}/booking/paypal/cancel?booking_id={new_booking.id}",
            metadata={"booking_id": new_booking.id, "booking_type": "direct"},
            intent="AUTHORIZE" if con_accettazione else "CAPTURE",
        )
        
        if not order:
            # Rimuovi il booking fallito
            session.delete(new_booking)
            session.commit()
            raise HTTPException(status_code=500, detail="Errore nella creazione dell'ordine PayPal")
        
        # Salva l'order_id sul booking
        new_booking.paypal_order_id = order["id"]
        session.add(new_booking)
        session.commit()
        
        # Trova l'approval URL
        approval_url = None
        for link in order.get("links", []):
            if link.get("rel") == "payer-action":
                approval_url = link["href"]
                break
        
        if not approval_url:
            session.delete(new_booking)
            session.commit()
            raise HTTPException(status_code=500, detail="URL di approvazione PayPal non trovato")
        
        logger.info(f"✅ PayPal order {order['id']} creato per booking {new_booking.id}")
        
        return JSONResponse({
            "success": True,
            "checkout_url": approval_url,
            "order_id": order["id"]
        })


# ==================== CONSULTATION OFFER ====================

@router.post("/api/consultation/{offer_id}/pay-paypal")
async def create_consultation_paypal(offer_id: int, request: Request):
    """
    Crea un ordine PayPal per un'offerta di consulenza.
    """
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    if not is_configured():
        raise HTTPException(status_code=503, detail="PayPal non configurato")
    
    body = await request.json()
    selected_date = body.get('date')
    start_time = body.get('start_time')
    end_time = body.get('end_time')
    community_question_id = body.get('community_question_id')
    description = body.get('description', '')
    recording_requested = body.get('recording_requested', True)
    
    if not all([selected_date, start_time, end_time]):
        raise HTTPException(status_code=400, detail="Dati slot mancanti")
    
    with Session(engine) as session:
        offer = session.get(ConsultationOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offerta non trovata")
        
        if current_user.id != offer.client_user_id:
            raise HTTPException(status_code=403, detail="Non sei autorizzato")
        
        if offer.status != "pending":
            raise HTTPException(status_code=400, detail="Questa offerta non è più disponibile")
        
        if offer.expires_at < datetime.utcnow():
            offer.status = "expired"
            session.add(offer)
            session.commit()
            raise HTTPException(status_code=400, detail="Questa offerta è scaduta")
        
        consultant = session.get(User, offer.consultant_user_id)
        if not has_payment_method(consultant):
            raise HTTPException(status_code=400, detail="Il consulente non ha configurato un metodo di pagamento. Non è possibile prenotare.")

        if slot_gia_prenotato(session, offer.consultant_user_id, selected_date, start_time):
            raise HTTPException(status_code=409, detail="Questo slot è già stato prenotato")

        booking_dt = datetime.strptime(f"{selected_date} {start_time}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{selected_date} {end_time}", "%Y-%m-%d %H:%M")
        held_until = end_dt + timedelta(hours=48)
        
        new_booking = Booking(
            client_user_id=offer.client_user_id,
            consultant_user_id=offer.consultant_user_id,
            booking_date=booking_dt,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=offer.duration_minutes,
            price=offer.price,
            status="pending_payment",
            payment_status="pending",
            payment_method="paypal",
            payment_held_until=held_until,
            service_fee=spese_servizio(),
            community_question_id=int(community_question_id) if community_question_id else None,
            client_notes=description if description.strip() else f"Prenotazione da offerta consulenza #{offer.id}",
            recording_requested=recording_requested
        )
        session.add(new_booking)
        session.commit()
        session.refresh(new_booking)
        
        app_url = os.getenv("BASE_URL", "http://localhost:8080")
        order = create_order(
            amount=float(totale_cliente(offer.price)),
            currency="EUR",
            return_url=f"{app_url}/booking/paypal/capture?booking_id={new_booking.id}&offer_id={offer.id}",
            cancel_url=f"{app_url}/booking/paypal/cancel?booking_id={new_booking.id}",
            metadata={"booking_id": new_booking.id, "booking_type": "consultation_offer", "offer_id": offer.id}
        )
        
        if not order:
            session.delete(new_booking)
            session.commit()
            raise HTTPException(status_code=500, detail="Errore nella creazione dell'ordine PayPal")
        
        new_booking.paypal_order_id = order["id"]
        session.add(new_booking)
        session.commit()
        
        approval_url = None
        for link in order.get("links", []):
            if link.get("rel") == "payer-action":
                approval_url = link["href"]
                break
        
        if not approval_url:
            session.delete(new_booking)
            session.commit()
            raise HTTPException(status_code=500, detail="URL di approvazione PayPal non trovato")
        
        logger.info(f"✅ PayPal order {order['id']} creato per offerta {offer.id}, booking {new_booking.id}")
        
        return JSONResponse({
            "success": True,
            "checkout_url": approval_url,
            "order_id": order["id"]
        })


# ==================== CAPTURE (return da PayPal) ====================

@router.get("/booking/paypal/capture")
async def paypal_capture(request: Request):
    """
    Callback dopo approvazione PayPal. Cattura il pagamento e conferma il booking.
    """
    booking_id = request.query_params.get("booking_id")
    offer_id = request.query_params.get("offer_id")  # solo per consultation offers
    
    if not booking_id:
        return RedirectResponse("/profile?error=paypal_missing_booking", status_code=302)
    
    with Session(engine) as session:
        booking = session.get(Booking, int(booking_id))
        if not booking:
            return RedirectResponse("/profile?error=paypal_booking_not_found", status_code=302)
        
        if booking.status != "pending_payment":
            # Già processato (doppio click o webhook)
            return RedirectResponse("/profile", status_code=302)
        
        if not booking.paypal_order_id:
            return RedirectResponse("/profile?error=paypal_no_order", status_code=302)

        if booking.acceptance_deadline:
            return _autorizza_richiesta(session, booking)

        # Cattura il pagamento
        capture_data = capture_order(booking.paypal_order_id)
        if not capture_data or capture_data.get("status") != "COMPLETED":
            logger.error(f"❌ PayPal capture fallita per booking {booking_id}: {capture_data}")
            return RedirectResponse("/profile?error=paypal_capture_failed", status_code=302)
        
        # Estrai capture_id e importo effettivamente incassato
        capture_id = None
        importo_incassato = None
        try:
            purchase_units = capture_data.get("purchase_units", [])
            if purchase_units:
                captures = purchase_units[0].get("payments", {}).get("captures", [])
                if captures:
                    capture_id = captures[0].get("id")
                    importo = captures[0].get("amount", {}).get("value")
                    if importo is not None:
                        importo_incassato = float(importo)
        except (IndexError, KeyError, TypeError, ValueError):
            pass

        # L'importo catturato deve corrispondere al prezzo della consulenza:
        # senza questo controllo si accettava per buono qualunque valore
        # tornasse da PayPal, compreso un ordine manipolato lato client.
        atteso = float(booking.price) if booking.price is not None else None
        if importo_incassato is None:
            logger.error(f"❌ PayPal booking {booking_id}: importo non presente nella capture, non confermo")
            return RedirectResponse("/profile?error=paypal_importo_mancante", status_code=302)
        if atteso is not None and abs(importo_incassato - atteso) > 0.01:
            logger.error(
                f"❌ PayPal booking {booking_id}: incassati {importo_incassato} € "
                f"ma il prezzo e' {atteso} €. Prenotazione NON confermata."
            )
            return RedirectResponse("/profile?error=paypal_importo_non_corrispondente", status_code=302)

        # Aggiorna booking
        booking.status = "confirmed"
        booking.payment_status = "held"
        booking.paypal_capture_id = capture_id
        session.add(booking)
        
        # Se è un'offerta consulenza, aggiorna lo stato dell'offerta
        if offer_id:
            offer = session.get(ConsultationOffer, int(offer_id))
            if offer:
                offer.status = "accepted"
                offer.booking_id = booking.id
                offer.updated_at = datetime.utcnow()
                session.add(offer)
        
        session.commit()
        
        # Notifiche a consulente e cliente + promemoria, no-show e rilascio a 48h
        conferma_consulenza(session, booking)
        logger.info(f"✅ PayPal booking {booking.id} confermato, capture {capture_id}")
        
    return RedirectResponse("/profile", status_code=302)


def _autorizza_richiesta(session, booking):
    """Ritorno da PayPal per un consulente che conferma a mano: si blocca l'importo senza incassarlo."""
    dati = authorize_order(booking.paypal_order_id)
    autorizzazione = None
    try:
        autorizzazione = dati["purchase_units"][0]["payments"]["authorizations"][0]
    except (KeyError, IndexError, TypeError):
        pass
    if not dati or dati.get("status") != "COMPLETED" or not autorizzazione:
        logger.error(f"❌ PayPal autorizzazione fallita per booking {booking.id}: {dati}")
        return RedirectResponse("/profile?error=paypal_capture_failed", status_code=302)

    try:
        importo = float(autorizzazione.get("amount", {}).get("value"))
    except (TypeError, ValueError):
        importo = None
    atteso = float(booking.price) if booking.price is not None else None
    if importo is None or (atteso is not None and abs(importo - atteso) > 0.01):
        logger.error(f"❌ PayPal booking {booking.id}: autorizzati {importo} € ma il prezzo e' {atteso} €")
        return RedirectResponse("/profile?error=paypal_importo_non_corrispondente", status_code=302)

    booking.paypal_authorization_id = autorizzazione.get("id")
    metti_in_attesa(session, booking)
    return RedirectResponse("/profile", status_code=302)


# ==================== CANCEL (ritorno da PayPal senza pagare) ====================

@router.get("/booking/paypal/cancel")
async def paypal_cancel(request: Request):
    """
    L'utente ha annullato il pagamento su PayPal.
    Rimuove il booking pending_payment.
    """
    booking_id = request.query_params.get("booking_id")
    
    if booking_id:
        with Session(engine) as session:
            booking = session.get(Booking, int(booking_id))
            if booking and booking.status == "pending_payment":
                session.delete(booking)
                session.commit()
                logger.info(f"🗑️ Booking {booking_id} rimosso (PayPal cancellato)")
    
    return RedirectResponse("/profile", status_code=302)
