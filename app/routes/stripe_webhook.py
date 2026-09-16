"""
Stripe Webhook Handler
Receives and processes Stripe events (payment confirmations, etc.)
"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import os
import json
from app.database import engine
from app.models import Booking, ConsultationOffer, User, Notification
from app.utils.stripe_config import construct_webhook_event
from app.logger_config import logger
from app.scheduler import schedule_booking_reminders, schedule_payment_release, schedule_noshow_check
from app.utils.booking_requests import conferma_consulenza, metti_in_attesa
from app.utils.notification_service import send_notification

router = APIRouter()

# Timezone italiano
ITALY_TZ = ZoneInfo("Europe/Rome")

@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """
    Handle Stripe webhook events
    Stripe will call this endpoint when payment events occur
    """
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    logger.info(f"🔔 Stripe webhook received. Signature header: {sig_header is not None}")
    
    if not sig_header:
        logger.error("❌ Missing Stripe signature header")
        raise HTTPException(status_code=400, detail="Missing Stripe signature")
    
    try:
        # Verify webhook signature only
        construct_webhook_event(payload, sig_header)
    except ValueError as e:
        logger.error(f"❌ Invalid Stripe webhook: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    
    # Parse raw JSON payload as plain dict (avoids Stripe object issues)
    event_dict = json.loads(payload)
    event_type = event_dict.get('type', '')
    logger.info(f"✅ Webhook signature verified. Event type: {event_type}")
    
    event_object = event_dict.get('data', {}).get('object', {})
    
    # Handle the event
    if event_type == 'checkout.session.completed':
        logger.info("🎉 Processing checkout.session.completed event")
        await handle_checkout_session_completed(event_object)
    
    elif event_type == 'payment_intent.succeeded':
        logger.info(f"✅ Payment intent succeeded: {event_object.get('id')}")
    
    elif event_type == 'payment_intent.payment_failed':
        logger.warning(f"❌ Payment intent failed: {event_object.get('id')}")
    
    else:
        logger.warning(f"⚠️  Unhandled event type: {event_type}")
    
    # Return 200 to acknowledge receipt of the event
    return JSONResponse({"status": "success"})


async def handle_checkout_session_completed(checkout_session):
    """
    Handle successful payment - create booking in database
    """
    session_id = checkout_session.get('id')
    payment_intent_id = checkout_session.get('payment_intent')
    metadata = checkout_session.get('metadata', {})
    # Importo realmente incassato, in centesimi: è l'unica fonte attendibile
    # del prezzo della consulenza (la tariffa oraria del consulente non lo è,
    # perché la durata può essere 90 o 120 minuti).
    amount_total = checkout_session.get('amount_total')

    logger.info(f"📦 Processing checkout session: {session_id}")
    logger.info(f"💳 Payment intent: {payment_intent_id}")

    # Check booking type
    booking_type = metadata.get('booking_type', 'consultation_offer')
    logger.info(f"📝 Booking type: {booking_type}")

    if booking_type == 'direct':
        logger.info(f"🔄 Handling direct booking from metadata: {metadata}")
        # Direct booking (from booking.html)
        await handle_direct_booking(session_id, payment_intent_id, metadata, amount_total)
    else:
        logger.info(f"🔄 Handling consultation offer booking from metadata: {metadata}")
        # Consultation offer booking (from consultation offer)
        await handle_consultation_offer_booking(session_id, payment_intent_id, metadata)


def _after_booking_confirmed(db_session, booking, messaggio_consulente=None):
    """Notifiche (a consulente e cliente) e job di una prenotazione confermata."""
    conferma_consulenza(db_session, booking, messaggio_consulente=messaggio_consulente)


def _spese_dai_metadati(metadata) -> Decimal:
    """Le spese di servizio dichiarate al checkout, 0 se la sessione è più vecchia di questa voce."""
    try:
        return Decimal(str((metadata or {}).get("service_fee") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


async def handle_direct_booking(session_id, payment_intent_id, metadata, amount_total=None):
    """Handle direct booking payment"""
    client_user_id = int(metadata.get('client_user_id'))
    consultant_user_id = int(metadata.get('consultant_user_id'))
    booking_date_str = metadata.get('booking_date')
    start_time = metadata.get('start_time')
    end_time = metadata.get('end_time')
    duration_minutes = int(metadata.get('duration_minutes'))
    availability_block_id = metadata.get('availability_block_id')
    client_notes = metadata.get('client_notes', '')
    description = metadata.get('description', '')
    community_question_id = metadata.get('community_question_id', '')
    recording_requested_raw = metadata.get('recording_requested', 'true')
    recording_requested = recording_requested_raw == 'true'
    logger.info(f"📹 recording_requested da Stripe metadata: raw='{recording_requested_raw}', parsed={recording_requested}")
    
    logger.info(f"👤 Direct booking details:")
    logger.info(f"   Client: {client_user_id}, Consultant: {consultant_user_id}")
    logger.info(f"   Date: {booking_date_str}, Time: {start_time}-{end_time}")
    logger.info(f"   Duration: {duration_minutes} min, Block ID: {availability_block_id}")
    logger.info(f"   Description: {description[:50]}..." if len(description) > 50 else f"   Description: {description}")  # Log primo 50 chars
    
    with Session(engine) as db_session:
        # La prenotazione e' gia' stata creata in stato 'pending_payment' quando
        # e' partito il checkout (serve a tenere occupato lo slot). Qui la si
        # conferma. Il ramo "crea da zero" resta per le sessioni aperte prima di
        # questa modifica e per eventuali booking creati altrove.
        existing_booking = db_session.query(Booking).filter(
            Booking.stripe_checkout_session_id == session_id
        ).first()

        if not existing_booking and metadata.get('booking_id'):
            try:
                existing_booking = db_session.get(Booking, int(metadata['booking_id']))
            except (TypeError, ValueError):
                existing_booking = None

        if existing_booking:
            if existing_booking.status == "pending_payment" and existing_booking.acceptance_deadline:
                # Consulente che conferma a mano: l'importo e' solo autorizzato
                # (capture manuale) e la prenotazione aspetta la sua risposta.
                existing_booking.payment_method = "stripe"
                existing_booking.stripe_checkout_session_id = session_id
                existing_booking.stripe_payment_intent_id = payment_intent_id
                existing_booking.recording_requested = recording_requested
                metti_in_attesa(db_session, existing_booking)
                return

            if existing_booking.status == "pending_payment":
                existing_booking.status = "confirmed"
                existing_booking.payment_status = "held"
                existing_booking.payment_method = "stripe"
                existing_booking.stripe_checkout_session_id = session_id
                existing_booking.stripe_payment_intent_id = payment_intent_id
                existing_booking.recording_requested = recording_requested
                existing_booking.updated_at = datetime.utcnow()
                db_session.add(existing_booking)
                db_session.commit()
                db_session.refresh(existing_booking)
                logger.info(f"✅ Booking {existing_booking.id} confermato dal pagamento (sessione {session_id})")
                _after_booking_confirmed(db_session, existing_booking)
                return

            # Sincronizza recording_requested se diverso (es. webhook production vecchio)
            if existing_booking.recording_requested != recording_requested:
                logger.info(f"Aggiorno recording_requested per booking {existing_booking.id}: {existing_booking.recording_requested} → {recording_requested}")
                existing_booking.recording_requested = recording_requested
                db_session.add(existing_booking)
                db_session.commit()
            logger.info(f"Booking already exists for session {session_id}")
            return
        
        # Get consultant to get price
        consultant = db_session.get(User, consultant_user_id)
        if not consultant:
            logger.error(f"Consultant {consultant_user_id} not found")
            return
        
        # Prezzo della consulenza: usa l'importo davvero incassato da Stripe.
        # Prima qui finiva la tariffa ORARIA del consulente, quindi una consulenza
        # da 90 o 120 minuti veniva registrata a un prezzo inferiore a quello
        # pagato — e il transfer a 48h (calcolato su booking.price) pagava meno
        # del dovuto al consulente.
        # Le spese di servizio sono incassate insieme alla consulenza ma non ne
        # fanno parte: vanno tolte dall'importo pagato, altrimenti gonfierebbero
        # la commissione e il pagamento al consulente.
        spese = _spese_dai_metadati(metadata)

        price = None
        if amount_total:
            price = round(amount_total / 100 - float(spese), 2)
        elif metadata.get('price'):
            try:
                price = round(float(metadata['price']), 2)
            except (TypeError, ValueError):
                price = None
        if price is None:
            hourly = consultant.prezzo_consulenza or 0
            price = round(hourly / 60 * duration_minutes, 2)
            logger.warning(
                f"⚠️ amount_total assente per la sessione {session_id}: "
                f"prezzo ricalcolato da tariffa oraria = {price}"
            )
        logger.info(f"💶 Prezzo consulenza registrato: {price} € ({duration_minutes} min)")

        # Parse booking datetime
        booking_datetime = datetime.strptime(f"{booking_date_str} {start_time}", "%Y-%m-%d %H:%M")
        
        # Calcola fine consulenza + 48h per il hold
        end_datetime = datetime.strptime(f"{booking_date_str} {end_time}", "%Y-%m-%d %H:%M")
        held_until = end_datetime + timedelta(hours=48)
        
        # Create booking
        new_booking = Booking(
            client_user_id=client_user_id,
            consultant_user_id=consultant_user_id,
            availability_block_id=int(availability_block_id) if availability_block_id else None,
            booking_date=booking_datetime,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            price=price,
            service_fee=spese or None,
            status="confirmed",
            payment_status="held",
            payment_method="stripe",
            stripe_checkout_session_id=session_id,
            stripe_payment_intent_id=payment_intent_id,
            payment_held_until=held_until,
            client_notes=client_notes or f"Prenotazione diretta",
            description=description,
            community_question_id=int(community_question_id) if community_question_id else None,
            recording_requested=recording_requested
        )
        
        db_session.add(new_booking)
        db_session.commit()
        db_session.refresh(new_booking)

        logger.info(f"✅ Direct booking {new_booking.id} created successfully for session {session_id}")
        _after_booking_confirmed(db_session, new_booking)


async def handle_consultation_offer_booking(session_id, payment_intent_id, metadata):
    """Handle consultation offer booking payment"""
    offer_id = int(metadata.get('offer_id'))
    client_user_id = int(metadata.get('client_user_id'))
    consultant_user_id = int(metadata.get('consultant_user_id'))
    selected_date = metadata.get('selected_date')
    start_time = metadata.get('start_time')
    end_time = metadata.get('end_time')
    duration_minutes = int(metadata.get('duration_minutes'))
    community_question_id = metadata.get('community_question_id', '')
    description = metadata.get('description', '')
    recording_requested_raw = metadata.get('recording_requested', 'true')
    recording_requested = recording_requested_raw == 'true'
    
    with Session(engine) as db_session:
        # Get consultation offer
        offer = db_session.get(ConsultationOffer, offer_id)
        if not offer:
            logger.error(f"Consultation offer {offer_id} not found")
            return
        
        # La riga e' gia' stata creata in 'pending_payment' quando e' partito il
        # checkout (tiene occupato lo slot): qui la si conferma.
        existing_booking = db_session.query(Booking).filter(
            Booking.stripe_checkout_session_id == session_id
        ).first()
        if not existing_booking and metadata.get('booking_id'):
            try:
                existing_booking = db_session.get(Booking, int(metadata['booking_id']))
            except (TypeError, ValueError):
                existing_booking = None

        if existing_booking:
            if existing_booking.status != "pending_payment":
                logger.info(f"Booking already exists for session {session_id}")
                return
            existing_booking.status = "confirmed"
            existing_booking.payment_status = "held"
            existing_booking.payment_method = "stripe"
            existing_booking.stripe_checkout_session_id = session_id
            existing_booking.stripe_payment_intent_id = payment_intent_id
            existing_booking.recording_requested = recording_requested
            existing_booking.updated_at = datetime.utcnow()
            db_session.add(existing_booking)

            offer.status = "accepted"
            offer.booking_id = existing_booking.id
            offer.updated_at = datetime.utcnow()
            db_session.add(offer)
            db_session.commit()
            db_session.refresh(existing_booking)

            client = db_session.get(User, client_user_id)
            client_name = f"{client.nome} {client.cognome}" if client and client.nome else "Un utente"
            _after_booking_confirmed(
                db_session, existing_booking,
                messaggio_consulente=(
                    f"{client_name} ha accettato la tua offerta e prenotato per il "
                    f"{existing_booking.booking_date:%d/%m/%Y} alle {start_time}"
                ),
            )
            logger.info(f"✅ Booking {existing_booking.id} da offerta confermato dal pagamento")
            return
        
        # Parse booking datetime
        booking_datetime = datetime.strptime(f"{selected_date} {start_time}", "%Y-%m-%d %H:%M")
        
        # Calcola fine consulenza + 48h per il hold
        end_datetime = datetime.strptime(f"{selected_date} {end_time}", "%Y-%m-%d %H:%M")
        held_until = end_datetime + timedelta(hours=48)
        
        # Create booking
        new_booking = Booking(
            client_user_id=client_user_id,
            consultant_user_id=consultant_user_id,
            booking_date=booking_datetime,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            price=offer.price,
            service_fee=_spese_dai_metadati(metadata) or None,
            status="confirmed",
            payment_status="held",
            payment_method="stripe",
            stripe_checkout_session_id=session_id,
            stripe_payment_intent_id=payment_intent_id,
            payment_held_until=held_until,
            community_question_id=int(community_question_id) if community_question_id else None,
            client_notes=description if description.strip() else f"Prenotazione da offerta consulenza #{offer.id}",
            recording_requested=recording_requested
        )
        
        db_session.add(new_booking)
        db_session.commit()
        db_session.refresh(new_booking)
        
        # Update offer status
        offer.status = "accepted"
        offer.booking_id = new_booking.id
        offer.updated_at = datetime.utcnow()
        db_session.add(offer)
        db_session.commit()
        
        client = db_session.get(User, client_user_id)
        client_name = f"{client.nome} {client.cognome}" if client and client.nome else "Un utente"
        logger.info(f"✅ Booking {new_booking.id} created successfully for session {session_id}")
        _after_booking_confirmed(
            db_session, new_booking,
            messaggio_consulente=(
                f"{client_name} ha accettato la tua offerta e prenotato per il {selected_date} alle {start_time}"
            ),
        )


@router.post("/webhook/stripe/connect")
async def stripe_connect_webhook(request: Request):
    """
    Handle Stripe Connect webhook events (account updates, payouts)
    """
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    logger.info("🔔 Stripe Connect webhook received")
    
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing Stripe signature")
    
    connect_secret = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET")
    if not connect_secret:
        connect_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    
    try:
        from app.utils.stripe_config import stripe_module as stripe_lib
        if not stripe_lib:
            raise HTTPException(status_code=503, detail="Stripe non disponibile")
        stripe_lib.Webhook.construct_event(payload, sig_header, connect_secret)
    except ValueError as e:
        logger.error(f"❌ Invalid Connect webhook payload: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"❌ Connect webhook signature error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    
    event_dict = json.loads(payload)
    event_type = event_dict.get('type', '')
    event_object = event_dict.get('data', {}).get('object', {})
    
    logger.info(f"📡 Connect event type: {event_type}")
    
    if event_type == 'account.updated':
        account_id = event_object.get('id')
        charges_enabled = event_object.get('charges_enabled', False)
        payouts_enabled = event_object.get('payouts_enabled', False)
        
        with Session(engine) as db_session:
            from sqlmodel import select
            user = db_session.exec(
                select(User).where(User.stripe_account_id == account_id)
            ).first()
            
            if user:
                is_complete = bool(charges_enabled and payouts_enabled)
                if user.stripe_onboarding_complete != is_complete:
                    user.stripe_onboarding_complete = is_complete
                    db_session.add(user)
                    db_session.commit()
                    logger.info(f"✅ Account {account_id}: onboarding_complete={is_complete}")
    
    return JSONResponse({"status": "success"})

