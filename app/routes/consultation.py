from fastapi import APIRouter, Request, Depends, HTTPException, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select
from datetime import datetime, timedelta
from typing import Optional
from decimal import Decimal
import asyncio
import os

from ..database import engine
from ..models import User, ConsultationOffer, Message, Category, CommunityQuestion, Booking
from .auth import get_current_user
from app.utils_user import has_payment_method
from app.logger_config import logger
from app.routes.booking import slot_gia_prenotato
from app.utils.stripe_config import create_checkout_session
from app.utils.prezzi import PREZZO_ORARIO_MINIMO, centesimi, spese_servizio, totale_cliente

router = APIRouter()


@router.get("/consulenza/crea/{client_user_id}", response_class=HTMLResponse)
async def show_create_consultation_form(
    request: Request,
    client_user_id: int,
    user: User = Depends(get_current_user)
):
    """Show form for consultant to create a consultation offer"""
    
    with Session(engine) as session:
        # Verify user is logged in
        if not user:
            raise HTTPException(status_code=401, detail="Devi essere loggato per creare offerte")
        
        # Verify current user is a verified consultant
        if not user.is_verified:
            raise HTTPException(status_code=403, detail="Solo i consulenti possono creare offerte di consulenza")
        
        if not has_payment_method(user):
            raise HTTPException(status_code=403, detail="Configura un metodo di pagamento (Stripe o PayPal) nel tuo profilo prima di offrire consulenze")
        
        # Get client user
        client = session.get(User, client_user_id)
        if not client:
            raise HTTPException(status_code=404, detail="Cliente non trovato")
        
        # Check if there's already a pending offer
        existing_offer = session.exec(
            select(ConsultationOffer)
            .where(ConsultationOffer.consultant_user_id == user.id)
            .where(ConsultationOffer.client_user_id == client_user_id)
            .where(ConsultationOffer.status == "pending")
            .where(ConsultationOffer.expires_at > datetime.utcnow())
        ).first()
        
        return request.app.state.templates.TemplateResponse("create_consultation_offer.html", {
            "request": request,
            "current_user": user,
            "client": client,
            "existing_offer": existing_offer,
            "default_price": user.prezzo_consulenza or 50,
            "duration_options": [60, 90, 120]
        })


@router.post("/consulenza/crea/{client_user_id}")
async def create_consultation_offer(
    request: Request,
    client_user_id: int,
    price: float = Form(...),
    duration_minutes: int = Form(...),
    custom_message: Optional[str] = Form(None),
    user: User = Depends(get_current_user)
):
    """Create a new consultation offer and send automated message"""
    
    with Session(engine) as session:
        # Verify user is logged in
        if not user:
            raise HTTPException(status_code=401, detail="Devi essere loggato per creare offerte")
        
        # Verify current user is a verified consultant
        if not user.is_verified:
            raise HTTPException(status_code=403, detail="Solo i consulenti possono creare offerte di consulenza")
        
        if not has_payment_method(user):
            raise HTTPException(status_code=403, detail="Configura un metodo di pagamento (Stripe o PayPal) nel tuo profilo prima di offrire consulenze")
        
        # Validate inputs
        if duration_minutes not in [60, 90, 120]:
            raise HTTPException(status_code=400, detail="Durata non valida. Scegli tra 60, 90 o 120 minuti")

        # Il minimo è orario: un'offerta da 30 minuti a 15€ vale 30€/ora ed è
        # regolare, una da 2 ore a 30€ vale 15€/ora e non lo è.
        minimo = PREZZO_ORARIO_MINIMO * duration_minutes / 60
        if price < minimo:
            raise HTTPException(
                status_code=400,
                detail=f"La tariffa minima è {PREZZO_ORARIO_MINIMO}€/ora: "
                       f"per {duration_minutes} minuti servono almeno {minimo:.0f}€",
            )
        
        # Get client user
        client = session.get(User, client_user_id)
        if not client:
            raise HTTPException(status_code=404, detail="Cliente non trovato")
        
        # Expire any previous pending offers from this consultant to this client
        previous_offers = session.exec(
            select(ConsultationOffer)
            .where(ConsultationOffer.consultant_user_id == user.id)
            .where(ConsultationOffer.client_user_id == client_user_id)
            .where(ConsultationOffer.status == "pending")
        ).all()
        
        for offer in previous_offers:
            offer.status = "expired"
            offer.updated_at = datetime.utcnow()
            session.add(offer)
        
        # Create new consultation offer (expires in 7 days)
        new_offer = ConsultationOffer(
            consultant_user_id=user.id,
            client_user_id=client_user_id,
            price=price,
            duration_minutes=duration_minutes,
            status="pending",
            message=custom_message,
            expires_at=datetime.utcnow() + timedelta(days=7)
        )
        session.add(new_offer)
        session.commit()
        session.refresh(new_offer)
    
        # Send automated message to client
        message_content = f"""🎯 **Offerta di Consulenza**

Ho creato un'offerta di consulenza per te:
• Durata: {duration_minutes} minuti
• Prezzo: €{price:.2f}"""
        
        if custom_message:
            message_content += f"\n• Messaggio: {custom_message}"
        
        message_content += f"""

[📅 Prenota ora](/consulenza/prenota/{new_offer.id})

_Questa offerta scade il {new_offer.expires_at.strftime('%d/%m/%Y alle %H:%M')}_"""
        
        # Get or create conversation between consultant and client
        from app.models import Conversation
        user1_id = min(user.id, client_user_id)
        user2_id = max(user.id, client_user_id)
        
        conversation = session.exec(
            select(Conversation)
            .where(Conversation.user1_id == user1_id)
            .where(Conversation.user2_id == user2_id)
        ).first()
        
        if not conversation:
            conversation = Conversation(
                user1_id=user1_id,
                user2_id=user2_id
            )
            session.add(conversation)
            session.commit()
            session.refresh(conversation)
        
        # Insert system message into conversation
        system_message = Message(
            conversation_id=conversation.id,
            sender_id=user.id,
            content=message_content,
            is_system_message=True,
            consultation_offer_id=new_offer.id
        )
        session.add(system_message)
        
        # Update conversation timestamp
        conversation.updated_at = datetime.utcnow()
        session.add(conversation)
        
        session.commit()
        
        # Return JSON response instead of redirect
        return JSONResponse({
            "success": True,
            "message": "Messaggio per la prenotazione inviato correttamente",
            "offer_id": new_offer.id,
            "client_user_id": client_user_id
        })



@router.get("/consulenza/prenota/{offer_id}", response_class=HTMLResponse)
async def show_booking_page(
    request: Request,
    offer_id: int,
    user: User = Depends(get_current_user)
):
    """Show booking page for client to book consultation"""
    
    if not user:
        return RedirectResponse("/login", status_code=302)
    
    with Session(engine) as session:
        # Get consultation offer
        offer = session.get(ConsultationOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offerta non trovata")
        
        # Verify user is the client
        if user.id != offer.client_user_id:
            raise HTTPException(status_code=403, detail="Non sei autorizzato a prenotare questa consulenza")
        
        # Check if offer is still valid
        if offer.status != "pending":
            raise HTTPException(status_code=400, detail=f"Questa offerta non è più disponibile (stato: {offer.status})")
        
        if offer.expires_at < datetime.utcnow():
            offer.status = "expired"
            offer.updated_at = datetime.utcnow()
            session.add(offer)
            session.commit()
            raise HTTPException(status_code=400, detail="Questa offerta è scaduta")
        
        # Get consultant
        consultant = session.get(User, offer.consultant_user_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consulente non trovato")
        
        # Categoria del consulente
        consultant_category = None
        if consultant.category_id:
            consultant_category = session.get(Category, consultant.category_id)
        
        # Aree di interesse parsate
        consultant_skills = []
        if consultant.aree_interesse:
            consultant_skills = [s.strip() for s in consultant.aree_interesse.split(',') if s.strip()]
        
        # Domande community del cliente (validate)
        client_questions = []
        try:
            questions = session.exec(
                select(CommunityQuestion).where(
                    CommunityQuestion.user_id == user.id,
                    CommunityQuestion.validation == True
                ).order_by(CommunityQuestion.created_at.desc())
            ).all()
            for q in questions:
                q._category_name = None
                q._primary_category_name = None
                if q.category_id:
                    cat = session.get(Category, q.category_id)
                    if cat:
                        q._category_name = cat.name
                if q.primary_category_id:
                    pcat = session.get(Category, q.primary_category_id)
                    if pcat:
                        q._primary_category_name = pcat.name
                client_questions.append(q)
        except Exception as e:
            logger.error(f"Error loading client questions: {e}")
        
        return request.app.state.templates.TemplateResponse("book_consultation_offer.html", {
            "request": request,
            "user": user,
            "current_user": user,
            "offer": offer,
            "consultant": consultant,
            "consultant_category": consultant_category,
            "consultant_skills": consultant_skills,
            "client_questions": client_questions,
            "spese_servizio": float(spese_servizio()),
            "stripe_available": bool(getattr(consultant, 'stripe_onboarding_complete', False)),
            "paypal_available": _is_paypal_available() and bool(getattr(consultant, 'paypal_email', None)),
            "debug_mode": os.getenv("DEBUG", "false").lower() == "true"
        })


def _is_paypal_available():
    try:
        from app.utils.paypal_config import is_configured
        return is_configured()
    except Exception:
        return False


@router.get("/api/consultation-offers/mine")
async def get_my_consultation_offers(user: User = Depends(get_current_user)):
    """Offerte di consulenza inviate (come consulente) e ricevute (come cliente) dall'utente loggato."""
    if not user:
        raise HTTPException(status_code=401, detail="Non autenticato")

    with Session(engine) as session:
        sent = session.exec(
            select(ConsultationOffer)
            .where(ConsultationOffer.consultant_user_id == user.id)
            .order_by(ConsultationOffer.created_at.desc())
        ).all()
        received = session.exec(
            select(ConsultationOffer)
            .where(ConsultationOffer.client_user_id == user.id)
            .order_by(ConsultationOffer.created_at.desc())
        ).all()

        def serialize(offer, other_user_id, direction):
            other = session.get(User, other_user_id)
            other_name = "Utente"
            if other:
                other_name = (f"{other.nome or ''} {other.cognome or ''}").strip() or f"Utente #{other.id}"
            return {
                "id": offer.id,
                "direction": direction,
                "other_user_id": other_user_id,
                "other_user_name": other_name,
                "price": offer.price,
                "duration_minutes": offer.duration_minutes,
                "status": offer.status,
                "message": offer.message,
                "booking_id": offer.booking_id,
                "expires_at": offer.expires_at.isoformat() if offer.expires_at else None,
                "created_at": offer.created_at.isoformat() if offer.created_at else None,
            }

        return JSONResponse({
            "sent": [serialize(o, o.client_user_id, "sent") for o in sent],
            "received": [serialize(o, o.consultant_user_id, "received") for o in received],
        })


@router.get("/api/consultation-offers/{offer_id}")
async def get_consultation_offer(
    offer_id: int,
    user: User = Depends(get_current_user)
):
    """Get consultation offer details (API endpoint)"""
    
    with Session(engine) as session:
        offer = session.get(ConsultationOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offerta non trovata")
        
        # Verify user is either consultant or client
        if user.id not in [offer.consultant_user_id, offer.client_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        consultant = session.get(User, offer.consultant_user_id)
        client = session.get(User, offer.client_user_id)
        
        return JSONResponse({
            "id": offer.id,
            "consultant": {
                "id": consultant.id,
                "nome": consultant.nome,
                "cognome": consultant.cognome,
                "profile_picture": consultant.profile_picture
            },
            "client": {
                "id": client.id,
                "nome": client.nome,
                "cognome": client.cognome
            },
            "price": offer.price,
            "duration_minutes": offer.duration_minutes,
            "status": offer.status,
            "message": offer.message,
            "expires_at": offer.expires_at.isoformat(),
            "created_at": offer.created_at.isoformat()
        })


@router.post("/consulenza/prenota/{offer_id}/confirm")
async def confirm_booking(
    request: Request,
    offer_id: int,
    user: User = Depends(get_current_user)
):
    """Create Stripe Checkout Session for consultation booking"""
    import json
    import os
    
    # Get slot data from request body
    body = await request.json()
    selected_date = body.get('date')
    start_time = body.get('start_time')
    end_time = body.get('end_time')
    community_question_id = body.get('community_question_id')
    description = body.get('description', '')
    recording_requested = body.get('recording_requested', True)
    
    if not selected_date or not start_time or not end_time:
        raise HTTPException(status_code=400, detail="Dati slot mancanti")
    
    if not community_question_id and not description.strip():
        raise HTTPException(status_code=400, detail="Descrizione della consulenza obbligatoria")
    
    with Session(engine) as session:
        # Get consultation offer
        offer = session.get(ConsultationOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offerta non trovata")
        
        # Verify user is the client
        if user.id != offer.client_user_id:
            raise HTTPException(status_code=403, detail="Non sei autorizzato")
        
        # Check if offer is still valid
        if offer.status != "pending":
            raise HTTPException(status_code=400, detail="Questa offerta non è più disponibile")
        
        if offer.expires_at < datetime.utcnow():
            offer.status = "expired"
            session.add(offer)
            session.commit()
            raise HTTPException(status_code=400, detail="Questa offerta è scaduta")

        # Lo slot dev'essere ancora libero: l'offerta puo' essere stata mandata
        # giorni prima, e nel frattempo quell'ora puo' essere stata prenotata.
        if slot_gia_prenotato(session, offer.consultant_user_id, selected_date, start_time):
            raise HTTPException(status_code=409, detail="Questo slot è già stato prenotato")

        # Prenota lo slot prima di mandare al checkout, come per le prenotazioni
        # diritte: senza la riga, due clienti potevano pagare lo stesso orario.
        inizio = datetime.strptime(f"{selected_date} {start_time}", "%Y-%m-%d %H:%M")
        fine = datetime.strptime(f"{selected_date} {end_time}", "%Y-%m-%d %H:%M")
        pending_booking = Booking(
            client_user_id=offer.client_user_id,
            consultant_user_id=offer.consultant_user_id,
            booking_date=inizio,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=offer.duration_minutes,
            price=offer.price,
            status="pending_payment",
            payment_status="pending",
            payment_method="stripe",
            payment_held_until=fine + timedelta(hours=48),
            service_fee=spese_servizio(),
            community_question_id=int(community_question_id) if community_question_id else None,
            client_notes=description if description.strip() else f"Prenotazione da offerta consulenza #{offer.id}",
            description=description,
            recording_requested=recording_requested if isinstance(recording_requested, bool) else str(recording_requested).lower() == "true",
        )
        session.add(pending_booking)
        session.commit()
        session.refresh(pending_booking)

        # Get APP_URL from environment
        app_url = os.getenv("BASE_URL", "http://localhost:8080")

        # Create Stripe Checkout Session
        try:
            # Il cliente paga la consulenza piu' le spese di servizio
            amount_cents = centesimi(totale_cliente(offer.price))
            
            # Pagamento alla piattaforma — il trasferimento al consulente avviene dopo 48h
            
            # Chiamata HTTP a Stripe: fuori dall'event loop
            checkout_session = await asyncio.to_thread(
                create_checkout_session,
                amount=amount_cents,
                currency='eur',
                success_url=f"{app_url}/profile",
                cancel_url=f"{app_url}/consulenza/prenota/{offer_id}?cancelled=true",
                metadata={
                    'offer_id': str(offer.id),
                    'booking_id': str(pending_booking.id),  # riga gia' creata da confermare
                    'client_user_id': str(offer.client_user_id),
                    'consultant_user_id': str(offer.consultant_user_id),
                    'selected_date': selected_date,
                    'start_time': start_time,
                    'end_time': end_time,
                    'duration_minutes': str(offer.duration_minutes),
                    'community_question_id': str(community_question_id) if community_question_id else '',
                    'description': description,
                    'service_fee': f"{float(spese_servizio()):.2f}",
                    'recording_requested': str(recording_requested).lower()
                },
            )
            
            pending_booking.stripe_checkout_session_id = checkout_session.id
            session.add(pending_booking)
            session.commit()

            return JSONResponse({
                "success": True,
                "checkout_url": checkout_session.url,
                "session_id": checkout_session.id
            })

        except Exception as e:
            # Niente checkout, niente slot occupato
            session.delete(pending_booking)
            session.commit()
            logger.error(f"Error creating Stripe checkout session: {e}")
            raise HTTPException(status_code=500, detail=f"Errore nella creazione del pagamento: {str(e)}")


