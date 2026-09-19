from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, Form, File, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select, func
from datetime import datetime, timedelta, time
from typing import Optional, List, Dict, Union
from zoneinfo import ZoneInfo
from pydantic import BaseModel
import os
import asyncio
from app.database import engine
from app.models import Booking, User, AvailabilityBlock, CallMessage, Dispute, Review, CommunityQuestion, Category
from app.routes.auth import get_current_user
from app.utils.agora_recording import start_recording, stop_recording, get_recording_url
from app.logger_config import logger
from app.utils.stripe_config import create_checkout_session
from app.utils.prezzi import centesimi, spese_servizio, totale_cliente, totale_pagato
from app.utils_user import has_payment_method
from app.utils.orari import (
    ORE_LIMITE_ANNULLAMENTO, ORE_PREAVVISO_PRENOTAZIONE, con_fuso, data_consulenza,
    iso_ora_italiana, now_italy_naive,
)
from app.utils.booking_requests import (
    RichiestaNonValida, accetta_richiesta, annulla_blocco, avvisa_annullamento,
    richiede_accettazione, scadenza_risposta,
)
from app.utils.notification_email import NOTA_BLOCCO_ANNULLATO, NOTA_RIMBORSO

DEBUG_MODE = os.getenv("DEBUG", "false").lower() == "true"
from app.utils.notification_service import send_notification
from app.utils.rate_limit import enforce_rate_limit

router = APIRouter()

# ===== PYDANTIC MODELS =====
class ChatMessageRequest(BaseModel):
    message: str

# Timezone italiano
ITALY_TZ = ZoneInfo("Europe/Rome")

# Stati che occupano uno slot. 'pending_payment' e' la prenotazione creata
# prima di mandare l'utente al checkout: senza di essa nella lista, due clienti
# potevano superare entrambi il controllo anti-doppia-prenotazione e pagare
# lo stesso orario.
# 'awaiting_acceptance' e' la richiesta pagata (importo bloccato) che il
# consulente deve ancora accettare: lo slot resta suo finche' non risponde.
BLOCKING_BOOKING_STATUSES = ['pending', 'pending_payment', 'awaiting_acceptance', 'confirmed']

# Stati in cui la call non si puo' aprire
STATI_SENZA_CALL = {'pending_payment', 'awaiting_acceptance', 'cancelled'}

# Dopo quanto una prenotazione mai pagata smette di occupare lo slot
PENDING_PAYMENT_TTL_MINUTES = 35

# Quanti appuntamenti e quante richieste da confermare mostra il profilo
MAX_APPUNTAMENTI_PROFILO = 3
MAX_RICHIESTE_PROFILO = 20

# Stati di pagamento di una consulenza effettivamente pagata. 'held' e' il
# trattenuto in attesa delle 48h, 'released' il trasferito al consulente:
# escludere 'released' faceva sparire la consulenza dallo storico dopo 48 ore.
PAID_PAYMENT_STATUSES = ['paid', 'held', 'released']

# Lock per sincronizzare join_booking per lo stesso booking
# Evita race condition quando il client chiama join multiple volte
_booking_locks: Dict[int, asyncio.Lock] = {}

def get_booking_lock(booking_id: int) -> asyncio.Lock:
    """Ottiene un lock univoco per il booking"""
    if booking_id not in _booking_locks:
        _booking_locks[booking_id] = asyncio.Lock()
    return _booking_locks[booking_id]


# Presenza "in questo momento" nella call: booking_id -> set di user_id.
# Va tenuta separata da booking.client_joined_at / consultant_joined_at, che
# sono il registro di PRESENZA STORICA usato dal job no-show per decidere
# rimborsi. Azzerare i joined_at all'uscita faceva risultare "nessuno si è
# presentato" una consulenza regolarmente svolta, con rimborso automatico.
# È stato in memoria come _screen_share_state: dopo un riavvio si perde, ma
# i recording orfani sono comunque chiusi dal job stop_orphan_recordings.
_call_presence: Dict[int, set] = {}


def mark_present(booking_id: int, user_id: int) -> None:
    _call_presence.setdefault(booking_id, set()).add(user_id)


def mark_absent(booking_id: int, user_id: int) -> set:
    """Rimuove l'utente dalla call e ritorna chi resta."""
    present = _call_presence.get(booking_id)
    if present is None:
        return set()
    present.discard(user_id)
    if not present:
        _call_presence.pop(booking_id, None)
        return set()
    return present

def motivo_chiusura_call(session, booking_id: int) -> Optional[str]:
    """'review' o 'dispute' se la consulenza e' chiusa, altrimenti None.

    Dopo una recensione la consulenza e' giudicata e finita. Dopo una
    contestazione lo e' ancora di piu': quello che e' successo e' in
    discussione, e lasciar rientrare in call permetterebbe di aggiungere
    materiale a una vicenda gia' aperta (la registrazione, che e' la prova
    principale, e' quella della consulenza vera).
    """
    if session.exec(select(Review.id).where(Review.booking_id == booking_id)).first():
        return "review"
    if session.exec(select(Dispute.id).where(Dispute.booking_id == booking_id)).first():
        return "dispute"
    return None


def slot_gia_prenotato(session, consultant_user_id: int, giorno: str, start_time: str) -> bool:
    """True se quell'orario del consulente e' gia' occupato da un'altra prenotazione.

    Stessa regola per prenotazioni dirette, PayPal e offerte di consulenza:
    il pagamento di un'offerta non lo controllava affatto.
    """
    return session.exec(
        select(Booking.id)
        .where(func.date(Booking.booking_date) == giorno)
        .where(Booking.consultant_user_id == consultant_user_id)
        .where(Booking.start_time == start_time)
        .where(Booking.status.in_(BLOCKING_BOOKING_STATUSES))
    ).first() is not None


def booking_start_datetime(booking: Booking) -> datetime:
    """Combina booking_date e start_time in un datetime naive (ora italiana)."""
    booking_date = data_consulenza(booking.booking_date)

    start_time_obj = booking.start_time
    if isinstance(start_time_obj, str):
        start_time_obj = datetime.strptime(start_time_obj, "%H:%M").time()

    return datetime.combine(booking_date, start_time_obj)


def hours_until_booking(booking: Booking) -> float:
    """Ore mancanti all'inizio della consulenza (negative se già iniziata)."""
    return (booking_start_datetime(booking) - now_italy_naive()).total_seconds() / 3600


def parse_time_to_minutes(time_input: Union[str, time]) -> int:
    """Converte una stringa HH:MM o un oggetto time in minuti dalla mezzanotte"""
    if isinstance(time_input, time):
        # Se è già un oggetto time, usa hour e minute
        return time_input.hour * 60 + time_input.minute
    # Se è una stringa, fai il parsing
    hours, minutes = map(int, time_input.split(':'))
    return hours * 60 + minutes

def minutes_to_time(minutes: int) -> str:
    """Converte minuti dalla mezzanotte in stringa HH:MM"""
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"

def calculate_available_slots(
    availability_blocks: List[AvailabilityBlock],
    existing_bookings: List[Booking],
    duration_minutes: int,
    date_str: str
) -> List[Dict]:
    """
    Calcola gli slot disponibili per una data e durata specificata.
    
    Args:
        availability_blocks: Blocchi di disponibilità del consulente
        existing_bookings: Prenotazioni già esistenti
        duration_minutes: Durata desiderata (60, 90, 120)
        date_str: Data in formato "YYYY-MM-DD"
    
    Returns:
        Lista di slot disponibili con start_time e end_time
    """
    available_slots = []
    
    # Determina l'ora minima per la data odierna (timezone italiano)
    now_italy = datetime.now(ITALY_TZ)
    today = now_italy.date()
    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    
    # Calcola il minimo datetime: il preavviso minimo dal momento attuale
    if DEBUG_MODE:
        current_time_minutes = 0
    else:
        min_datetime = now_italy + timedelta(hours=ORE_PREAVVISO_PRENOTAZIONE)

        # Se il minimo datetime è dopo il target_date (cioè il target_date è nel passato rispetto al limite),
        # allora non ci sono slot disponibili per questa data
        if target_date < min_datetime.date():
            print(f"🕐 Data {target_date} è prima del preavviso minimo ({min_datetime.date()}), nessuno slot disponibile")
            return []

        # Calcola i minuti da inizio giornata per il minimo time
        if min_datetime.date() == target_date:
            # Il limite cade nello stesso giorno della prenotazione
            current_time_minutes = min_datetime.hour * 60 + min_datetime.minute
        else:
            # Il limite cade in un giorno precedente (target_date è dopo il limite)
            # Quindi nessun limite per questo giorno (può iniziare da 00:00)
            current_time_minutes = 0
    
    print(f"🕐 calculate_available_slots: now={now_italy}, target_date={target_date}, current_time_minutes={current_time_minutes}, debug={DEBUG_MODE}")
    
    for block in availability_blocks:
        # Converti start_time e end_time in minuti
        block_start = parse_time_to_minutes(block.start_time)
        block_end = parse_time_to_minutes(block.end_time)
        
        # Minimo di tempo richiesto: il preavviso dal momento attuale
        min_start_time = current_time_minutes if current_time_minutes is not None else 0

        if block_end <= min_start_time:
            # Tutto il blocco è nel passato o dentro il preavviso, saltalo
            continue
        # Aggiorna il block_start se parte del blocco è nel passato o dentro il preavviso
        if block_start < min_start_time:
            # Arrotonda al prossimo slot di 30 minuti
            block_start = ((min_start_time + 29) // 30) * 30
        
        # Crea lista di intervalli occupati in questo blocco
        occupied_intervals = []
        for booking in existing_bookings:
            if booking.availability_block_id == block.id or (
                booking.booking_date.strftime('%Y-%m-%d') == date_str and
                booking.status not in ['cancelled', 'no_show']
            ):
                booking_start = parse_time_to_minutes(booking.start_time)
                booking_end = parse_time_to_minutes(booking.end_time)
                occupied_intervals.append((booking_start, booking_end))
        
        # Ordina gli intervalli occupati
        occupied_intervals.sort()
        
        # Calcola slot disponibili
        current_time = block_start
        
        for occupied_start, occupied_end in occupied_intervals:
            # C'è spazio prima di questo intervallo occupato?
            while current_time + duration_minutes <= occupied_start:
                slot = {
                    'start_time': minutes_to_time(current_time),
                    'end_time': minutes_to_time(current_time + duration_minutes),
                    'availability_block_id': block.id
                }
                available_slots.append(slot)
                print(f"   ✅ Slot aggiunto: {slot['start_time']} - {slot['end_time']}")
                current_time += 15  # Incremento di 15 minuti per slot successivo
            
            # Salta l'intervallo occupato
            current_time = max(current_time, occupied_end)
        
        # Slot disponibili dopo l'ultimo intervallo occupato
        while current_time + duration_minutes <= block_end:
            slot = {
                'start_time': minutes_to_time(current_time),
                'end_time': minutes_to_time(current_time + duration_minutes),
                'availability_block_id': block.id
            }
            available_slots.append(slot)
            print(f"   ✅ Slot aggiunto (dopo): {slot['start_time']} - {slot['end_time']}")
            current_time += 15
    
    return available_slots

# ========== PAGINA PRENOTAZIONE ==========

@router.get("/book/{consultant_id}", response_class=HTMLResponse, name="booking_page")
async def booking_page(
    request: Request,
    consultant_id: int
):
    """Pagina di prenotazione con il consulente"""
    # Verifica che l'utente sia autenticato
    current_user = get_current_user(request)
    if not current_user:
        return request.app.state.templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Devi effettuare il login per prenotare una consulenza"
        })
    
    # Verifica che l'utente non sia in modalità anonima
    if current_user.is_anonymous:
        raise HTTPException(
            status_code=403, 
            detail="Non puoi prenotare una consulenza mentre sei in modalità anonima. Disattiva la modalità anonima dal tuo profilo per procedere."
        )
    
    with Session(engine) as session:
        # Prendi i dati del consulente
        consultant = session.get(User, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consulente non trovato")
        
        # Non puoi prenotare con te stesso
        if current_user.id == consultant_id:
            raise HTTPException(status_code=400, detail="Non puoi prenotare una consulenza con te stesso")
        
        # Prendi le domande community del cliente (validate)
        client_questions = session.exec(
            select(CommunityQuestion)
            .where(CommunityQuestion.user_id == current_user.id)
            .where(CommunityQuestion.validation == True)
            .order_by(CommunityQuestion.created_at.desc())
        ).all()
        
        # Arricchisci con nomi categoria
        for q in client_questions:
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
        
        # Categoria del consulente
        consultant_category = None
        if consultant.category_id:
            consultant_category = session.get(Category, consultant.category_id)
        
        # Aree di interesse parsate
        consultant_skills = []
        if consultant.aree_interesse:
            consultant_skills = [s.strip() for s in consultant.aree_interesse.split(',') if s.strip()]
        
        return request.app.state.templates.TemplateResponse("booking.html", {
            "request": request,
            "user": current_user,
            "current_user": current_user,  # Per la navbar
            "consultant": consultant,
            "consultant_category": consultant_category,
            "consultant_skills": consultant_skills,
            "debug_mode": DEBUG_MODE,
            "stripe_available": bool(getattr(consultant, 'stripe_onboarding_complete', False)),
            "paypal_available": _is_paypal_available() and bool(getattr(consultant, 'paypal_email', None)),
            "consultant_has_payment": has_payment_method(consultant),
            "requires_acceptance": richiede_accettazione(consultant),
            "spese_servizio": float(spese_servizio()),
            "client_questions": client_questions
        })


def _is_paypal_available():
    try:
        from app.utils.paypal_config import is_configured
        return is_configured()
    except Exception:
        return False

# ========== API ENDPOINTS ==========

@router.get("/api/booking/available-slots/{consultant_id}")
async def get_available_slots(
    consultant_id: int,
    date: str,
    duration: int
):
    """
    Restituisce gli slot disponibili per un consulente in una data specifica.
    
    Args:
        consultant_id: ID del consulente
        date: Data in formato YYYY-MM-DD
        duration: Durata in minuti (60, 90, 120)
    """
    # Validazione durata
    if duration not in [60, 90, 120]:
        raise HTTPException(status_code=400, detail="Durata non valida. Valori ammessi: 60, 90, 120 minuti")
    
    with Session(engine) as session:
        # Verifica che il consulente esista
        consultant = session.get(User, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consulente non trovato")
        
        # Parse della data
        try:
            target_date = datetime.strptime(date, '%Y-%m-%d').date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato data non valido. Usa YYYY-MM-DD")
        
        # Non si può prenotare nel passato (usa timezone italiano)
        today_italy = datetime.now(ITALY_TZ).date()
        if target_date < today_italy and not DEBUG_MODE:
            raise HTTPException(status_code=400, detail="Non puoi prenotare nel passato")
        
        # Prendi i blocchi di disponibilità per quella data
        availability_blocks = session.exec(
            select(AvailabilityBlock)
            .where(func.date(AvailabilityBlock.date) == date)
            .where(AvailabilityBlock.user_id == consultant_id)
            .where(AvailabilityBlock.is_active == True)
            .where(AvailabilityBlock.status == "available")
        ).all()
        
        # DEBUG: Log dei blocchi trovati
        print(f"🔍 DEBUG - Date: {date}, Consultant: {consultant_id}")
        print(f"📅 Blocchi trovati: {len(availability_blocks)}")
        for block in availability_blocks:
            print(f"   Block ID {block.id}: {block.start_time} - {block.end_time} (status: {block.status}, active: {block.is_active})")
        
        if not availability_blocks:
            return {"slots": [], "message": "Il consulente non è disponibile in questa data"}
        
        # Prendi le prenotazioni esistenti per quella data
        existing_bookings = session.exec(
            select(Booking)
            .where(func.date(Booking.booking_date) == date)
            .where(Booking.consultant_user_id == consultant_id)
            .where(Booking.status.in_(BLOCKING_BOOKING_STATUSES))
        ).all()
        
        # Calcola gli slot disponibili
        available_slots = calculate_available_slots(
            availability_blocks,
            existing_bookings,
            duration,
            date
        )
        
        return {
            "slots": available_slots,
            "consultant": {
                "id": consultant.id,
                "nome": consultant.nome,
                "cognome": consultant.cognome,
                "prezzo": consultant.prezzo_consulenza
            },
            "date": date,
            "duration_minutes": duration
        }


@router.post("/api/booking/validate-description")
async def validate_booking_description(request: Request):
    """Valida la descrizione della consulenza tramite AI"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Non autenticato")

    body = await request.json()
    description = (body.get("description") or "").strip()

    if not description:
        return {"approved": False, "reason": "La descrizione è vuota"}

    try:
        from app.utils.ai_service import valida_descrizione_consulenza
        result = await valida_descrizione_consulenza(description)
        return result
    except Exception as e:
        logger.error(f"Errore validazione AI descrizione: {e}")
        return {"approved": True, "reason": "OK"}


@router.post("/api/booking/create")
async def create_booking(
    request: Request,
    booking_data: dict
):
    """
    Crea Stripe Checkout Session per prenotazione.
    
    Body:
        consultant_user_id: int
        booking_date: str (YYYY-MM-DD)
        start_time: str (HH:MM)
        end_time: str (HH:MM)
        duration_minutes: int
        availability_block_id: int (optional)
        client_notes: str (optional)
    """
    import os
    
    # Verifica autenticazione
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    # 🆕 Verifica che l'utente non sia in modalità anonima
    if current_user.is_anonymous:
        raise HTTPException(
            status_code=403, 
            detail="Non puoi prenotare una consulenza mentre sei in modalità anonima. Disattiva la modalità anonima dal tuo profilo per procedere."
        )
    
    # Validazione dati
    consultant_id = booking_data.get('consultant_user_id')
    booking_date_str = booking_data.get('booking_date')
    start_time = booking_data.get('start_time')
    end_time = booking_data.get('end_time')
    duration_minutes = booking_data.get('duration_minutes')
    availability_block_id = booking_data.get('availability_block_id')
    client_notes = booking_data.get('client_notes', '')
    description = booking_data.get('description', '')  # 🆕 Descrizione della consulenza
    community_question_id = booking_data.get('community_question_id')  # Domanda community associata
    price = booking_data.get('price')  # Prezzo calcolato dal frontend
    recording_requested = booking_data.get('recording_requested', True)  # Default: registra
    logger.info(f"📹 recording_requested ricevuto dal frontend: {recording_requested} (tipo: {type(recording_requested).__name__})")
    
    # Validazioni
    if not all([consultant_id, booking_date_str, start_time, end_time, duration_minutes, price]):
        raise HTTPException(status_code=400, detail="Campi obbligatori mancanti")
    
    # Validazione descrizione (obbligatoria solo se nessuna domanda community associata)
    if not community_question_id and (not description or not description.strip()):
        raise HTTPException(status_code=400, detail="Descrizione della consulenza obbligatoria")
    
    if duration_minutes not in [60, 90, 120]:
        raise HTTPException(status_code=400, detail="Durata non valida (minimo 60 minuti)")
    
    # Non puoi prenotare con te stesso
    if current_user.id == consultant_id:
        raise HTTPException(status_code=400, detail="Non puoi prenotare con te stesso")
    
    with Session(engine) as session:
        # Verifica che il consulente esista
        consultant = session.get(User, consultant_id)
        if not consultant:
            raise HTTPException(status_code=404, detail="Consulente non trovato")
        
        if not has_payment_method(consultant):
            raise HTTPException(status_code=400, detail="Il consulente non ha configurato un metodo di pagamento. Non è possibile prenotare.")
        
        # Parse della data
        try:
            booking_date = datetime.strptime(booking_date_str, '%Y-%m-%d')
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato data non valido")
        
        # ✅ Validazione: prenotazione con il preavviso minimo
        if not DEBUG_MODE:
            # Combina data + ora di inizio (entrambi in ora italiana)
            booking_datetime = datetime.strptime(f"{booking_date_str} {start_time}", '%Y-%m-%d %H:%M')
            time_until_booking = (booking_datetime - now_italy_naive()).total_seconds() / 3600  # in ore

            if time_until_booking < ORE_PREAVVISO_PRENOTAZIONE:
                raise HTTPException(
                    status_code=400,
                    detail=f"La consulenza deve essere prenotata almeno {ORE_PREAVVISO_PRENOTAZIONE} ore nel futuro",
                )
        
        # Verifica che lo slot sia ancora disponibile (prevenzione double booking)
        if slot_gia_prenotato(session, consultant_id, booking_date_str, start_time):
            raise HTTPException(status_code=409, detail="Questo slot è già stato prenotato")
        
        # Validazione prezzo ricevuto dal frontend
        if not price or price <= 0:
            raise HTTPException(status_code=400, detail="Prezzo non valido")
        
        # Verifica che il prezzo sia coerente con la tariffa del consulente
        hourly_rate = consultant.prezzo_consulenza if consultant.prezzo_consulenza else 0
        if hourly_rate <= 0:
            raise HTTPException(status_code=400, detail="Il consulente non ha impostato un prezzo")
        
        # Calcola il prezzo atteso basato sulla durata
        expected_price = (hourly_rate / 60) * duration_minutes
        # Tolleranza di 1 euro per arrotondamenti
        if abs(price - expected_price) > 1:
            raise HTTPException(status_code=400, detail="Prezzo non valido per la durata selezionata")
        
        # Get APP_URL from environment
        app_url = os.getenv("BASE_URL", "http://localhost:8080")

        # Prenota lo slot PRIMA di mandare l'utente al checkout, come gia' fa il
        # flusso PayPal. Finche' la riga non esisteva, il booking nasceva solo nel
        # webhook: due clienti potevano superare entrambi il controllo qui sopra e
        # pagare lo stesso orario. La riga resta 'pending_payment' e viene liberata
        # da release_expired_pending_payments se il pagamento non arriva.
        booking_datetime_full = datetime.strptime(f"{booking_date_str} {start_time}", '%Y-%m-%d %H:%M')
        end_datetime_full = datetime.strptime(f"{booking_date_str} {end_time}", '%Y-%m-%d %H:%M')

        # Consulente che conferma a mano: la scadenza valorizzata segna la
        # prenotazione come richiesta (ricalcolata quando arriva il pagamento).
        con_accettazione = richiede_accettazione(consultant)

        pending_booking = Booking(
            client_user_id=current_user.id,
            consultant_user_id=consultant_id,
            availability_block_id=int(availability_block_id) if availability_block_id else None,
            booking_date=booking_datetime_full,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            price=price,
            status="pending_payment",
            payment_status="pending",
            payment_method="stripe",
            payment_held_until=end_datetime_full + timedelta(hours=48),
            acceptance_deadline=scadenza_risposta(booking_datetime_full) if con_accettazione else None,
            service_fee=spese_servizio(),
            client_notes=client_notes or "Prenotazione diretta",
            description=description,
            community_question_id=int(community_question_id) if community_question_id else None,
            # Puo' arrivare come booleano o come stringa: bool("false") sarebbe True
            recording_requested=(
                recording_requested if isinstance(recording_requested, bool)
                else str(recording_requested).lower() == "true"
            ),
        )
        session.add(pending_booking)
        session.commit()
        session.refresh(pending_booking)

        # Create Stripe Checkout Session
        try:
            # Il cliente paga la consulenza piu' le spese di servizio; a
            # booking.price resta il solo valore della consulenza, che e' la
            # base della commissione e del pagamento al consulente
            amount_cents = centesimi(totale_cliente(price))

            # Pagamento alla piattaforma — il trasferimento al consulente avviene dopo 48h
            if consultant.stripe_account_id and consultant.stripe_onboarding_complete:
                logger.info(f"💰 Pagamento trattenuto: consulente {consultant.stripe_account_id} riceverà dopo 48h dalla fine consulenza")

            checkout_session = await asyncio.to_thread(
                create_checkout_session,
                amount=amount_cents,
                currency='eur',
                success_url=f"{app_url}/profile",
                cancel_url=f"{app_url}/book/{consultant_id}?cancelled=true",
                metadata={
                    'booking_type': 'direct',  # differenzia da consultation offer
                    'booking_id': str(pending_booking.id),  # riga gia' creata da confermare
                    'client_user_id': str(current_user.id),
                    'consultant_user_id': str(consultant_id),
                    'booking_date': booking_date_str,
                    'start_time': start_time,
                    'end_time': end_time,
                    'duration_minutes': str(duration_minutes),
                    'availability_block_id': str(availability_block_id) if availability_block_id else '',
                    'client_notes': client_notes,
                    'description': description,
                    'community_question_id': str(community_question_id) if community_question_id else '',
                    'price': f"{float(price):.2f}",  # prezzo totale, non la tariffa oraria
                    'service_fee': f"{float(spese_servizio()):.2f}",  # quanto dell'incasso non e' consulenza
                    'recording_requested': str(recording_requested).lower(),  # 👈 Valore inviato a Stripe
                    'requires_acceptance': 'true' if con_accettazione else 'false',
                },
                capture_manual=con_accettazione,
            )

            pending_booking.stripe_checkout_session_id = checkout_session.id
            session.add(pending_booking)
            session.commit()

            return {
                "success": True,
                "checkout_url": checkout_session.url,
                "session_id": checkout_session.id
            }

        except Exception as e:
            # Niente checkout, niente slot occupato
            session.delete(pending_booking)
            session.commit()
            logger.error(f"Error creating Stripe checkout session: {e}")
            raise HTTPException(status_code=500, detail=f"Errore nella creazione del pagamento: {str(e)}")

@router.get("/api/booking/my-bookings")
async def get_my_bookings(
    request: Request
):
    """Restituisce tutte le prenotazioni dell'utente corrente (come cliente o consulente)"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        # Prenotazioni come cliente (solo quelle pagate)
        bookings_as_client = session.exec(
            select(Booking)
            .where(Booking.client_user_id == current_user.id)
            .where(Booking.payment_status.in_(PAID_PAYMENT_STATUSES))
            .order_by(Booking.booking_date.desc())
        ).all()
        
        # Prenotazioni come consulente (solo quelle pagate)
        bookings_as_consultant = session.exec(
            select(Booking)
            .where(Booking.consultant_user_id == current_user.id)
            .where(Booking.payment_status.in_(PAID_PAYMENT_STATUSES))
            .order_by(Booking.booking_date.desc())
        ).all()
        
        # Formatta i risultati
        def format_booking(booking: Booking, role: str):
            other_user_id = booking.consultant_user_id if role == 'client' else booking.client_user_id
            other_user = session.get(User, other_user_id)
            
            return {
                "id": booking.id,
                "date": booking.booking_date.strftime('%Y-%m-%d'),
                "start_time": booking.start_time,
                "end_time": booking.end_time,
                "duration_minutes": booking.duration_minutes,
                "status": booking.status,
                "payment_status": booking.payment_status,
                "price": float(booking.price) if booking.price else 0,
                "role": role,
                "other_user": {
                    "id": other_user.id,
                    "nome": other_user.nome,
                    "cognome": other_user.cognome,
                    "profile_picture": other_user.profile_picture
                } if other_user else None,
                "meeting_link": booking.meeting_link,
                "notes": booking.client_notes if role == 'client' else booking.consultant_notes
            }
        
        return {
            "as_client": [format_booking(b, 'client') for b in bookings_as_client],
            "as_consultant": [format_booking(b, 'consultant') for b in bookings_as_consultant]
        }

@router.get("/api/booking/upcoming")
async def get_upcoming_bookings(request: Request):
    """Ottiene i prossimi 3 appuntamenti futuri dell'utente"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        # Ora italiana, come gli orari delle prenotazioni. datetime.now() è
        # l'ora del server (UTC su Render): "mancano X minuti" risultava sfasato
        # di 1-2 ore e il bottone per entrare compariva all'ora sbagliata.
        now = now_italy_naive()
        
        # Prenotazioni FUTURE confermate e pagate (incluso 'held' per pagamenti in
        # attesa di rilascio), piu' le richieste con importo bloccato che il
        # consulente deve ancora accettare.
        confermate = Booking.status.in_(['confirmed', 'pending']) & Booking.payment_status.in_(PAID_PAYMENT_STATUSES)
        in_attesa = (Booking.status == 'awaiting_acceptance') & (Booking.payment_status == 'authorized')
        statement = select(Booking).where(
            (Booking.client_user_id == current_user.id) | (Booking.consultant_user_id == current_user.id),
            confermate | in_attesa,
            Booking.booking_date >= now.date()
        ).order_by(Booking.booking_date, Booking.start_time)
        
        bookings = session.exec(statement).all()

        # Due elenchi separati: gli appuntamenti veri e propri (al massimo 3,
        # come prima) e le richieste ancora da confermare, che nel profilo
        # stanno in un gruppo a parte e possono essere molte.
        upcoming = []
        richieste = []
        for booking in bookings:
            # Calcola quando inizia l'appuntamento
            # booking.booking_date potrebbe essere date o datetime, convertiamo sempre a date
            booking_date = data_consulenza(booking.booking_date)
                
            booking_datetime = datetime.combine(
                booking_date,
                datetime.strptime(booking.start_time, "%H:%M").time()
            )
            
            # Calcola i minuti fino all'inizio
            time_until = (booking_datetime - now).total_seconds() / 60
            
            # Se qualcuno è in call, mostra sempre il booking (anche se il tempo è scaduto)
            someone_in_call = booking.client_joined_at is not None or booking.consultant_joined_at is not None or booking.call_started_at is not None
            
            # FILTRO: Salta appuntamenti passati, MA tieni quelli con call attiva
            if time_until < -booking.duration_minutes and not someone_in_call:
                continue
            

            # Determina il ruolo dell'utente corrente
            is_client = booking.client_user_id == current_user.id
            role = 'client' if is_client else 'consultant'
            
            # Ottieni i dati dell'altra persona
            other_user_id = booking.consultant_user_id if is_client else booking.client_user_id
            other_user = session.get(User, other_user_id)
            
            # Determina lo stato per l'UI
            can_join = time_until <= 10 and time_until >= -10  # Da 10 min prima a 10 min dopo inizio
            has_joined = booking.client_joined_at is not None if is_client else booking.consultant_joined_at is not None
            other_joined = booking.consultant_joined_at is not None if is_client else booking.client_joined_at is not None
            can_start_call = has_joined and other_joined
            
            # Se la call è stata avviata (call_started_at set o recording attiva), considera entrambi come joined
            call_actually_happened = booking.call_started_at is not None or booking.recording_status not in ("not_started", None)
            if call_actually_happened:
                has_joined = True
                other_joined = True
                can_start_call = True
            
            motivo_chiusura = motivo_chiusura_call(session, booking.id)

            in_attesa = booking.status == 'awaiting_acceptance'
            elenco = richieste if in_attesa else upcoming
            if len(elenco) >= (MAX_RICHIESTE_PROFILO if in_attesa else MAX_APPUNTAMENTI_PROFILO):
                if len(upcoming) >= MAX_APPUNTAMENTI_PROFILO and len(richieste) >= MAX_RICHIESTE_PROFILO:
                    break
                continue

            elenco.append({
                "id": booking.id,
                "date": str(booking_date) if not isinstance(booking.booking_date, str) else booking.booking_date,
                "start_time": booking.start_time,
                "end_time": booking.end_time,
                "duration": booking.duration_minutes,
                "status": booking.status,
                "payment_status": booking.payment_status,
                "stripe_payment_intent_id": booking.stripe_payment_intent_id,
                "role": role,
                "other_user": {
                    "id": other_user.id if other_user else None,
                    "name": f"{other_user.nome or ''} {other_user.cognome or ''}".strip() if other_user else "Utente",
                    "profession": other_user.professione if other_user else "",
                    "picture": other_user.profile_picture if other_user else None
                },
                "time_until_minutes": int(time_until),
                "can_join": can_join,
                "has_joined": has_joined,
                "other_joined": other_joined,
                "can_start_call": can_start_call,
                # Recensione o contestazione chiudono la consulenza: niente
                # più "Entra in call"
                "closed_by_review": motivo_chiusura == "review",
                "closed_by_dispute": motivo_chiusura == "dispute",
                "awaiting_acceptance": in_attesa,
                "acceptance_deadline": iso_ora_italiana(booking.acceptance_deadline),
                "acceptance_expired": bool(
                    in_attesa and booking.acceptance_deadline and now >= booking.acceptance_deadline
                ),
            })

        return {"bookings": upcoming, "requests": richieste}

@router.get("/api/booking/history")
async def get_booking_history(request: Request):
    """Ottiene lo storico completo degli appuntamenti passati dell'utente"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        now = now_italy_naive()

        # Tutto cio' che ha mosso del denaro: svolte, annullate, rimborsate,
        # assenze. Prima restavano fuori le annullate (e le assenze, rimborsate)
        # e il cliente non trovava piu' traccia di cosa era successo.
        # Restano fuori i checkout mai completati ('pending') e le richieste
        # ancora da confermare ('authorized'), che hanno un gruppo tutto loro.
        statement = select(Booking).where(
            (Booking.client_user_id == current_user.id) | (Booking.consultant_user_id == current_user.id),
            Booking.payment_status.in_(PAID_PAYMENT_STATUSES + ['refunded', 'partially_refunded', 'voided'])
        ).order_by(Booking.booking_date.desc(), Booking.start_time.desc())
        
        bookings = session.exec(statement).all()
        
        history = []
        for booking in bookings:
            booking_date = data_consulenza(booking.booking_date)
                
            booking_datetime = datetime.combine(
                booking_date,
                datetime.strptime(booking.start_time, "%H:%M").time()
            )
            
            end_datetime = booking_datetime + timedelta(minutes=booking.duration_minutes)

            annullata = booking.status == 'cancelled'
            # Solo appuntamenti già terminati (le annullate si mostrano subito:
            # non ci sarà nessuna call da aspettare)
            if end_datetime >= now and not annullata:
                continue
            
            is_client = booking.client_user_id == current_user.id
            role = 'client' if is_client else 'consultant'
            other_user_id = booking.consultant_user_id if is_client else booking.client_user_id
            other_user = session.get(User, other_user_id)
            
            # Check if a dispute already exists for this booking
            existing_dispute = session.exec(
                select(Dispute).where(Dispute.booking_id == booking.id)
            ).first()
            
            # Check if a review already exists for this booking
            existing_review = session.exec(
                select(Review).where(Review.booking_id == booking.id)
            ).first()
            
            # Can dispute: client, within 48h, recording was requested, no existing dispute
            hours_since_end = (now - end_datetime).total_seconds() / 3600
            can_dispute = (
                not annullata
                and is_client
                and hours_since_end <= 48
                and booking.recording_requested
                and existing_dispute is None
            )
            
            # Can review: client, no existing review, booking completed
            can_review = (
                is_client
                and existing_review is None
                and booking.status == 'completed'
            )

            if annullata:
                if booking.cancelled_by == current_user.id:
                    annullata_da = 'tu'
                elif booking.cancelled_by:
                    annullata_da = 'other'
                else:
                    annullata_da = 'system'
            else:
                annullata_da = None
            
            history.append({
                "id": booking.id,
                "date": str(booking_date),
                "start_time": booking.start_time,
                "end_time": booking.end_time,
                "duration": booking.duration_minutes,
                "status": booking.status,
                "payment_status": booking.payment_status,
                "cancelled_by": annullata_da,
                "cancellation_reason": booking.cancellation_reason if annullata else None,
                "role": role,
                # Importi: il cliente deve ritrovare quanto ha pagato davvero,
                # spese di servizio comprese. Le prenotazioni fatte prima delle
                # spese hanno service_fee a zero, quindi totale uguale a prezzo.
                "price": float(booking.price or 0),
                "service_fee": float(booking.service_fee or 0),
                "total_paid": float(totale_pagato(booking)),
                "refund_amount": float(booking.refund_amount or 0),
                "can_dispute": can_dispute,
                "has_dispute": existing_dispute is not None,
                "dispute_status": existing_dispute.status if existing_dispute else None,
                "has_review": existing_review is not None,
                "can_review": can_review,
                "other_user": {
                    "id": other_user.id if other_user else None,
                    "name": f"{other_user.nome or ''} {other_user.cognome or ''}".strip() if other_user else "Utente",
                    "profession": other_user.professione if other_user else "",
                    "picture": other_user.profile_picture if other_user else None
                }
            })
        
        return {"bookings": history}

@router.post("/api/booking/{booking_id}/join")
async def join_booking(booking_id: int, request: Request):
    """Segna che l'utente ha cliccato 'Partecipa' per un appuntamento"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")

    # Il lock deve coprire TUTTA la sezione critica (lettura stato + avvio
    # registrazione): prima racchiudeva solo get_current_user, quindi due join
    # simultanei potevano entrambi far partire una registrazione.
    lock = get_booking_lock(booking_id)
    async with lock:
        with Session(engine) as session:
            booking = session.get(Booking, booking_id)
            if not booking:
                raise HTTPException(status_code=404, detail="Prenotazione non trovata")

            # Verifica che l'utente sia parte della prenotazione
            if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
                raise HTTPException(status_code=403, detail="Non autorizzato")

            if booking.status in STATI_SENZA_CALL:
                raise HTTPException(status_code=403, detail="Questa consulenza non è confermata")

            if motivo_chiusura_call(session, booking_id):
                raise HTTPException(status_code=403, detail="Questa consulenza è chiusa")

            # Determina il ruolo e salva il timestamp
            is_client = booking.client_user_id == current_user.id
            now = now_italy_naive()
            mark_present(booking_id, current_user.id)

            if is_client:
                if booking.client_joined_at is None:  # Solo se non ha già joinato
                    booking.client_joined_at = now
            else:
                if booking.consultant_joined_at is None:  # Solo se non ha già joinato
                    booking.consultant_joined_at = now

            booking.updated_at = now
            session.add(booking)
            session.commit()
            session.refresh(booking)

            # Controlla se entrambi hanno joinato
            client_joined = booking.client_joined_at is not None
            consultant_joined = booking.consultant_joined_at is not None

            logger.info(f"🔍 Join check for booking {booking_id}: client_joined={client_joined}, consultant_joined={consultant_joined}, recording_status={booking.recording_status}")

            if booking.recording_status == "failed" and (client_joined or consultant_joined):
                logger.warning(
                    f"♻️ Previous recording attempt failed for booking {booking_id}; resetting state to allow retry"
                )
                booking.recording_status = "not_started"
                booking.recording_sid = None
                booking.recording_resource_id = None
                booking.recording_started_at = None
                booking.recording_filename = None
                booking.updated_at = now
                session.add(booking)
                session.commit()
                session.refresh(booking)
                logger.info(f"✅ Recording state reset for booking {booking_id}; new attempt permitted")

            # 🎥 NUOVO: Avvia registrazione automatica se almeno uno ha joinato
            # Se lo status è "completed" significa che gli utenti hanno riiniziato dopo aver chiuso
            # → riavvia una nuova registrazione con un nuovo session counter
            # ⚠️ Non registrare se il cliente non ha richiesto la registrazione
            should_start_recording = booking.recording_requested and (client_joined or consultant_joined) and booking.recording_status not in ("recording", "failed")

            if not booking.recording_requested:
                logger.info(f"ℹ️ Recording NOT requested for booking {booking_id} - skipping recording")

            # Se la registrazione era completata e qualcuno rejoin → incrementa session counter
            if booking.recording_requested and booking.recording_status == "completed" and (client_joined or consultant_joined):
                logger.info(f"🔄 User rejoined after previous recording completed - starting new session")
                booking.recording_session_count = (booking.recording_session_count or 0) + 1
                # "not_started" e non None: su un DB creato da SQLModel la colonna
                # e' NOT NULL e il commit fallirebbe.
                booking.recording_status = "not_started"  # Reset per far ripartire la registrazione
                session.add(booking)
                session.commit()
                session.refresh(booking)

            logger.info(f"🎥 Should start recording? {should_start_recording} (status={booking.recording_status})")

            if should_start_recording:
                try:
                    logger.info(f"🎯 [join_booking] Attempting to prepare recording...")
                    from app.utils.agora_token import generate_agora_token, ROLE_PUBLISHER

                    recorder_uid = 0  # uid=0 per permettere a qualsiasi uid di registrare
                    channel_name = f"booking_{booking_id}"

                    logger.info(f"🔧 Generating token for recorder (uid={recorder_uid}, channel={channel_name})...")
                    # Genera token per il recorder
                    recorder_token = generate_agora_token(channel_name, recorder_uid, ROLE_PUBLISHER, 7200)
                    logger.info(f"✓ Token generated successfully")

                    # Salva token per quando il frontend è pronto
                    booking.recording_status = "ready"
                    booking.updated_at = now
                    session.add(booking)
                    session.commit()
                    session.refresh(booking)

                    logger.info(f"✅ Recording prepared for booking {booking_id} - waiting for frontend signal")
                except Exception as e:
                    logger.error(f"❌ Error preparing recording for booking {booking_id}: {e}", exc_info=True)
                    logger.error(f"❌ Exception type: {type(e).__name__}, Message: {str(e)}")
                    # Continua anche se la registrazione fallisce
            else:
                if not (client_joined or consultant_joined):
                    logger.info(f"ℹ️ Skipping recording start: neither client nor consultant have joined yet")
                elif booking.recording_status == "recording":
                    logger.info(f"ℹ️ Skipping recording start: already recording")
                elif booking.recording_status == "failed":
                    logger.info(f"ℹ️ Skipping recording start: previous recording failed")

            return {
                "success": True,
                "has_joined": True,
                "other_joined": consultant_joined if is_client else client_joined,
                "can_start_call": client_joined and consultant_joined,
                "recording_status": booking.recording_status
            }

@router.get("/api/booking/{booking_id}/agora-token")
async def get_agora_token(booking_id: int, request: Request):
    """Genera un token Agora per accedere alla video call"""
    from app.utils.agora_token import generate_booking_call_token
    
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia parte della prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        if booking.status in STATI_SENZA_CALL:
            raise HTTPException(status_code=403, detail="Questa consulenza non è confermata")

        # Verifica che entrambi abbiano joinato
        if not booking.client_joined_at or not booking.consultant_joined_at:
            raise HTTPException(status_code=403, detail="Entrambi i partecipanti devono aver cliccato 'Partecipa'")

        # La finestra della call va verificata anche qui, non solo dal browser:
        # senza questo controllo il token si poteva ottenere (e rientrare nel
        # canale) a consulenza finita, chiamando l'endpoint direttamente.
        end_time_obj = datetime.strptime(booking.end_time, "%H:%M").time()
        call_deadline = datetime.combine(data_consulenza(booking.booking_date), end_time_obj) + timedelta(minutes=5)
        if now_italy_naive() >= call_deadline:
            raise HTTPException(status_code=403, detail="Il tempo della consulenza è scaduto")

        # Recensione o contestazione chiudono la call (stessa regola di
        # /call-status, che il frontend usa per bloccare il rientro)
        motivo = motivo_chiusura_call(session, booking_id)
        if motivo == "review":
            raise HTTPException(status_code=403, detail="La consulenza è stata chiusa con una recensione")
        if motivo == "dispute":
            raise HTTPException(status_code=403, detail="La consulenza è stata chiusa da una contestazione")


        # Genera il token Agora
        try:
            token_data = generate_booking_call_token(booking_id, current_user.id)
            
            # Determina il ruolo dell'utente
            is_client = booking.client_user_id == current_user.id
            
            # Ottieni i dati dell'altro partecipante
            other_user_id = booking.consultant_user_id if is_client else booking.client_user_id
            other_user = session.get(User, other_user_id)
            
            return {
                "success": True,
                "token": token_data["token"],
                "app_id": token_data["app_id"],
                "channel_name": token_data["channel_name"],
                "uid": token_data["uid"],
                "expiration": token_data["expiration"],
                "booking": {
                    "id": booking.id,
                    "duration_minutes": booking.duration_minutes,
                    "start_time": booking.start_time,
                    "end_time": booking.end_time
                },
                "user_role": "client" if is_client else "consultant",
                "other_user": {
                    "name": f"{other_user.nome or ''} {other_user.cognome or ''}".strip() if other_user else "Utente",
                    "profession": other_user.professione if other_user else ""
                }
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Errore generazione token: {str(e)}")

@router.post("/api/booking/{booking_id}/recording-preference")
async def update_recording_preference(booking_id: int, request: Request):
    """Aggiorna la preferenza di registrazione prima dell'inizio della call"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    body = await request.json()
    recording_requested = body.get('recording_requested')
    if recording_requested is None:
        raise HTTPException(status_code=400, detail="Campo recording_requested mancante")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        if current_user.id != booking.client_user_id:
            raise HTTPException(status_code=403, detail="Solo il cliente può modificare questa preferenza")
        
        booking.recording_requested = bool(recording_requested)
        booking.updated_at = datetime.utcnow()
        session.add(booking)
        session.commit()
        
        logger.info(f"📹 recording_requested aggiornato per booking {booking_id}: {booking.recording_requested}")
        return {"success": True, "recording_requested": booking.recording_requested}

@router.get("/booking/call/{booking_id}")
async def call_page(booking_id: int, request: Request):
    """Pagina placeholder per la call"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")

        if booking.status in STATI_SENZA_CALL:
            return RedirectResponse(url="/profile", status_code=303)

        # Con una recensione o una contestazione la consulenza è conclusa:
        # non è più possibile rientrare nella call (nemmeno entro la fascia oraria).
        motivo = motivo_chiusura_call(session, booking_id)
        if motivo:
            return RedirectResponse(url=f"/profile?call_closed={motivo}", status_code=303)

        # Recupera nomi reali per i label video
        client_user = session.get(User, booking.client_user_id)
        consultant_user = session.get(User, booking.consultant_user_id)
        
        def get_full_name(u, fallback):
            if not u:
                return fallback
            nome = u.nome or ""
            cognome = u.cognome or ""
            full = f"{nome} {cognome}".strip()
            return full if full else fallback
        
        if current_user.id == booking.client_user_id:
            local_user_name = get_full_name(client_user, "Tu")
            remote_user_name = get_full_name(consultant_user, "Partecipante")
        else:
            local_user_name = get_full_name(consultant_user, "Tu")
            remote_user_name = get_full_name(client_user, "Partecipante")
        
        return request.app.state.templates.TemplateResponse("call.html", {
            "request": request,
            "user": current_user,
            "current_user": current_user,
            "booking": booking,
            "local_user_name": local_user_name,
            "remote_user_name": remote_user_name
        })

@router.delete("/api/booking/cancel/{booking_id}")
async def cancel_booking(
    booking_id: int,
    request: Request,
    reason: Optional[str] = None
):
    """Cancella una prenotazione"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Solo il cliente o il consulente possono cancellare
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Non si può cancellare una prenotazione già completata
        if booking.status in ['completed', 'cancelled', 'no_show']:
            raise HTTPException(status_code=400, detail="Non puoi cancellare questa prenotazione")

        # ⛔ Finestra di cancellazione, come per il rifiuto del consulente. Senza
        # questo controllo il cliente poteva cancellare (e farsi rimborsare per
        # intero) anche a consulenza avvenuta, nei 15 minuti prima che il job
        # no-show la marcasse come completed.
        # Una richiesta non ancora accettata non e' stata addebitata: si puo'
        # ritirare finche' il consulente non risponde.
        if not DEBUG_MODE and booking.status != 'awaiting_acceptance':
            hours_left = hours_until_booking(booking)
            if hours_left < ORE_LIMITE_ANNULLAMENTO:
                if hours_left < 0:
                    detail = "La consulenza è già iniziata: non può più essere annullata. Se c'è stato un problema, apri una contestazione."
                else:
                    detail = f"Puoi annullare la consulenza solo fino a {ORE_LIMITE_ANNULLAMENTO} ore prima dell'inizio."
                raise HTTPException(status_code=400, detail=detail)

        # Aggiorna lo stato
        booking.status = 'cancelled'
        booking.cancelled_by = current_user.id
        booking.cancelled_at = datetime.utcnow()
        booking.cancellation_reason = reason
        booking.updated_at = datetime.utcnow()
        
        # Importo solo bloccato (richiesta non accettata): si annulla il blocco
        if booking.payment_status == 'authorized':
            if annulla_blocco(booking):
                booking.payment_status = "voided"
            else:
                logger.error(f"❌ Blocco non annullato per booking {booking_id}: da annullare a mano")

        # Rimborsa se il pagamento è stato effettuato
        if booking.payment_status in ('paid', 'held'):
            if booking.payment_method == "paypal" and booking.paypal_capture_id:
                try:
                    from app.utils.paypal_config import refund_capture
                    result = refund_capture(booking.paypal_capture_id)
                    if result:
                        logger.info(f"✅ Rimborso PayPal creato per booking {booking_id}")
                        booking.payment_status = "refunded"
                    else:
                        logger.error(f"❌ Errore rimborso PayPal per booking {booking_id}")
                except Exception as e:
                    logger.error(f"❌ Errore nel rimborso PayPal: {e}")
            elif booking.stripe_payment_intent_id:
                try:
                    import stripe as stripe_module
                    stripe_module.api_key = os.getenv("STRIPE_SECRET_KEY")
                    refund = stripe_module.Refund.create(
                        payment_intent=booking.stripe_payment_intent_id,
                        reason='requested_by_customer'
                    )
                    logger.info(f"✅ Rimborso creato: {refund.id} per booking {booking_id}")
                    booking.payment_status = "refunded"
                except Exception as e:
                    logger.error(f"❌ Errore nel rimborso: {e}")
        
        # Cancella il rilascio pagamento schedulato
        try:
            from app.scheduler import cancel_payment_release
            cancel_payment_release(booking_id)
        except Exception as e:
            logger.warning(f"⚠️ Impossibile cancellare rilascio pagamento schedulato: {e}")
        
        session.add(booking)
        session.commit()
        session.refresh(booking)

        # L'altro partecipante deve saperlo: prima non riceveva nulla e si
        # accorgeva dell'annullamento solo non trovando più l'appuntamento.
        try:
            avvisa_annullamento(session, booking, current_user.id, reason)
        except Exception as e:  # noqa: BLE001
            logger.error(f"❌ Avviso di annullamento non inviato per booking {booking_id}: {e}")

        return {
            "success": True,
            "message": "Prenotazione cancellata"
        }

# ========== CLOUD RECORDING ENDPOINTS ==========

@router.post("/api/booking/{booking_id}/recording/start")
async def start_booking_recording(booking_id: int, request: Request):
    """Avvia la registrazione cloud per una prenotazione"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Solo client e consultant possono avviare recording
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        if booking.status in STATI_SENZA_CALL:
            raise HTTPException(status_code=403, detail="Questa consulenza non è confermata")

        # Verifica che entrambi abbiano joinato
        if not booking.client_joined_at or not booking.consultant_joined_at:
            raise HTTPException(status_code=400, detail="Entrambi gli utenti devono essere presenti")
        
        # Non avviare se già in recording
        if booking.recording_status == "recording":
            raise HTTPException(status_code=400, detail="Recording già avviato")
        
        # Genera token per il bot recorder (UID speciale)
        from app.utils.agora_token import generate_agora_token, ROLE_PUBLISHER
        
        recorder_uid = 0  # UID fisso per il bot recorder
        channel_name = f"booking_{booking_id}"
        recorder_token = generate_agora_token(channel_name, recorder_uid, ROLE_PUBLISHER, 7200)
        
        # Avvia recording (chiamate HTTP ad Agora: fuori dall'event loop)
        result = await asyncio.to_thread(start_recording, channel_name, recorder_uid, recorder_token)
        
        if not result:
            raise HTTPException(status_code=500, detail="Errore avvio registrazione")
        
        # Aggiorna booking
        booking.recording_sid = result["sid"]
        booking.recording_resource_id = result["resource_id"]
        booking.recording_status = "recording"
        booking.recording_started_at = datetime.utcnow()
        booking.updated_at = datetime.utcnow()
        
        session.add(booking)
        session.commit()
        
        return {
            "success": True,
            "recording_sid": result["sid"],
            "message": "Registrazione avviata"
        }

@router.post("/api/booking/{booking_id}/recording/start-now")
async def start_recording_now(booking_id: int, request: Request):
    """Endpoint che il frontend chiama quando è pronto a registrare"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia parte della prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Verifica che il cliente abbia richiesto la registrazione
        if not booking.recording_requested:
            logger.info(f"ℹ️ Recording non richiesto per booking {booking_id} - skip start")
            return {"success": False, "message": "Recording non richiesto dal cliente"}
        
        # Se non è in uno stato di registrazione, non fare nulla
        if booking.recording_status not in ("ready", "not_started", None):
            logger.info(f"⚠️ Recording not in 'ready' state for booking {booking_id} (current: {booking.recording_status})")
            return {"success": False, "message": f"Recording not ready (status={booking.recording_status})"}
        
        try:
            from app.utils.agora_recording import start_recording
            from app.utils.agora_token import generate_agora_token, ROLE_PUBLISHER
            
            recorder_uid = 0
            channel_name = f"booking_{booking_id}"
            
            logger.info(f"🎬 [start-now] Frontend is ready, starting recording for booking {booking_id}...")
            
            # Genera token per il recorder
            recorder_token = generate_agora_token(channel_name, recorder_uid, ROLE_PUBLISHER, 7200)
            
            # Avvia registrazione (chiamate HTTP ad Agora: fuori dall'event loop)
            result = await asyncio.to_thread(start_recording, channel_name, recorder_uid, recorder_token)
            
            if result:
                now = datetime.utcnow()
                timestamp_str = booking.created_at.strftime("%Y%m%d_%H%M%S")
                session_num = booking.recording_session_count or 1
                recording_name = f"booking_{booking_id}_{timestamp_str}_session{session_num}"
                
                logger.info(f"✅ Recording started successfully: {recording_name}")
                
                booking.recording_sid = result["sid"]
                booking.recording_resource_id = result["resource_id"]
                booking.recording_status = "recording"
                booking.recording_started_at = now
                booking.recording_filename = recording_name
                booking.updated_at = now
                
                session.add(booking)
                session.commit()
                session.refresh(booking)
                
                return {
                    "success": True,
                    "message": "Recording started",
                    "sid": result["sid"],
                    "filename": recording_name
                }
            else:
                logger.error(f"❌ Failed to start recording for booking {booking_id}")
                booking.recording_status = "failed"
                session.add(booking)
                session.commit()
                
                return {"success": False, "message": "Failed to start recording"}
        
        except Exception as e:
            logger.error(f"❌ Error in start_recording_now: {e}", exc_info=True)
            booking.recording_status = "failed"
            session.add(booking)
            session.commit()
            
            raise HTTPException(status_code=500, detail=f"Error starting recording: {str(e)}")

@router.post("/api/booking/{booking_id}/recording/stop")
async def stop_booking_recording(booking_id: int, request: Request):
    """Ferma la registrazione cloud"""
    current_user = get_current_user(request)
    if not current_user:
        # Se chiamato da sendBeacon, potrebbe non avere la sessione
        # Tentiamo comunque di fermare la registrazione
        with Session(engine) as session:
            booking = session.get(Booking, booking_id)
            if booking and booking.recording_status == "recording":
                # Ferma senza autenticazione (emergenza)
                recorder_uid = 0
                channel_name = f"booking_{booking_id}"
                
                try:
                    result = await asyncio.to_thread(
                        stop_recording,
                        booking.recording_resource_id,
                        booking.recording_sid,
                        channel_name,
                        recorder_uid
                    )
                    
                    if result:
                        file_name = result["file_name"]
                        recording_url = get_recording_url(file_name)
                        booking.recording_url = recording_url
                        booking.recording_duration = result.get("mix_duration", 0)
                        booking.recording_status = "completed"
                    else:
                        booking.recording_status = "failed"
                    
                    booking.recording_completed_at = datetime.utcnow()
                    booking.updated_at = datetime.utcnow()
                    session.add(booking)
                    session.commit()
                except Exception as e:
                    print(f"Errore stop recording (no auth): {e}")
                    pass
        
        return {"success": True, "message": "Recording stop tentato"}
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Solo client e consultant possono fermare recording
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Se già fermato, non è un errore (potrebbe essere stato fermato dall'altro utente)
        if booking.recording_status != "recording":
            return {
                "success": True,
                "message": "Recording già fermato",
                "recording_url": booking.recording_url,
                "duration": booking.recording_duration
            }
        
        if not booking.recording_sid or not booking.recording_resource_id:
            raise HTTPException(status_code=400, detail="Dati recording mancanti")
        
        # Ferma recording (in thread separato per non bloccare event loop)
        recorder_uid = 0
        channel_name = f"booking_{booking_id}"
        
        result = await asyncio.to_thread(
            stop_recording,
            booking.recording_resource_id,
            booking.recording_sid,
            channel_name,
            recorder_uid
        )
        
        if not result:
            booking.recording_status = "failed"
        else:
            # Genera URL per accedere al video
            file_name = result["file_name"]
            recording_url = get_recording_url(file_name)
            
            booking.recording_url = recording_url
            booking.recording_duration = result.get("mix_duration", 0)
            booking.recording_status = "completed"
            booking.recording_completed_at = datetime.utcnow()
        
        booking.updated_at = datetime.utcnow()
        session.add(booking)
        session.commit()
        
        return {
            "success": True,
            "recording_url": booking.recording_url,
            "duration": booking.recording_duration,
            "message": "Registrazione completata"
        }

@router.get("/api/booking/{booking_id}/recording")
async def get_booking_recording(booking_id: int, request: Request):
    """Ottiene info sulla registrazione"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Solo client e consultant possono vedere recording
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        return {
            "booking_id": booking.id,
            "recording_status": booking.recording_status,
            "recording_url": booking.recording_url,
            "recording_duration": booking.recording_duration,
            "recording_file_size": booking.recording_file_size,
            "recording_started_at": booking.recording_started_at.isoformat() if booking.recording_started_at else None,
            "recording_completed_at": booking.recording_completed_at.isoformat() if booking.recording_completed_at else None
        }

@router.get("/booking/success", response_class=HTMLResponse)
async def booking_success(request: Request):
    """Payment success page"""
    current_user = get_current_user(request)
    return request.app.state.templates.TemplateResponse("booking_success.html", {
        "request": request,
        "user": current_user,
        "current_user": current_user
    })

@router.get("/booking/cancel", response_class=HTMLResponse)
async def booking_cancel(request: Request, offer_id: Optional[int] = None):
    """Payment cancelled page"""
    current_user = get_current_user(request)
    back_url = f"/consulenza/prenota/{offer_id}" if offer_id else "/profile"
    return request.app.state.templates.TemplateResponse("booking_cancel.html", {
        "request": request,
        "user": current_user,
        "current_user": current_user,
        "back_url": back_url
    })

@router.post("/api/booking/{booking_id}/refuse")
async def refuse_booking(booking_id: int, request: Request):
    """Consulente rifiuta una prenotazione"""
    from pydantic import BaseModel
    
    class RefuseRequest(BaseModel):
        reason: Optional[str] = None
    
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    
    body = await request.json()
    refuse_reason = body.get("reason", "")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia il consulente
        if booking.consultant_user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Solo il consulente può rifiutare la prenotazione")
        
        # Verifica che lo stato sia refusabile
        if booking.status not in ['pending', 'confirmed', 'awaiting_acceptance']:
            raise HTTPException(status_code=400, detail="Non puoi rifiutare una prenotazione in questo stato")

        # ✅ Validazione: annullamento entro la finestra. Una richiesta ancora da
        # accettare si rifiuta fino alla sua scadenza (un'ora prima dell'inizio).
        richiesta = booking.status == 'awaiting_acceptance'
        if not richiesta and hours_until_booking(booking) < ORE_LIMITE_ANNULLAMENTO:
            raise HTTPException(
                status_code=400,
                detail=f"Puoi annullare la consulenza solo fino a {ORE_LIMITE_ANNULLAMENTO} ore prima dell'inizio",
            )
        
        # ✅ 1. Cambia lo stato
        booking.status = "cancelled"
        booking.cancellation_reason = refuse_reason or "Rifiutato dal consulente"
        booking.cancelled_by = current_user.id
        booking.cancelled_at = datetime.utcnow()
        session.add(booking)
        
        # ✅ 2. Importo solo bloccato: si annulla il blocco, niente da rimborsare
        if booking.payment_status == 'authorized':
            if annulla_blocco(booking):
                booking.payment_status = "voided"
            else:
                logger.error(f"❌ Blocco non annullato per booking {booking_id}: da annullare a mano")

        # ✅ 2b. Rimborsa se il pagamento è avvenuto
        if booking.payment_status in ('paid', 'held'):
            if booking.payment_method == "paypal" and booking.paypal_capture_id:
                try:
                    from app.utils.paypal_config import refund_capture
                    result = refund_capture(booking.paypal_capture_id)
                    if result:
                        logger.info(f"✅ Rimborso PayPal creato per booking {booking_id}")
                        booking.payment_status = "refunded"
                    else:
                        logger.error(f"❌ Errore rimborso PayPal per booking {booking_id}")
                except Exception as e:
                    logger.error(f"❌ Errore nel rimborso PayPal: {e}")
            elif booking.stripe_payment_intent_id:
                try:
                    import stripe as stripe_module
                    stripe_module.api_key = os.getenv("STRIPE_SECRET_KEY")
                    
                    # Effettua il rimborso
                    refund = stripe_module.Refund.create(
                        payment_intent=booking.stripe_payment_intent_id,
                        reason='requested_by_customer'
                    )
                    logger.info(f"✅ Rimborso creato: {refund.id} per booking {booking_id}")
                    booking.payment_status = "refunded"
                except stripe_module.error.InvalidRequestError as e:
                    # Se la charge è già stata rimborsata, non è un errore
                    if "already been refunded" in str(e):
                        logger.info(f"⚠️ Booking {booking_id} era già stato rimborsato prima")
                        booking.payment_status = "refunded"
                    else:
                        logger.error(f"❌ Errore nel rimborso: {e}")
                        # Continua comunque, il rimborso manuale può essere fatto dopo
                except Exception as e:
                    logger.error(f"❌ Errore nel rimborso: {e}")
                    # Continua comunque, il rimborso manuale può essere fatto dopo
        
        # ✅ 2b. Cancella il rilascio pagamento schedulato
        try:
            from app.scheduler import cancel_payment_release
            cancel_payment_release(booking_id)
        except Exception as e:
            logger.warning(f"⚠️ Impossibile cancellare rilascio pagamento schedulato: {e}")
        
        session.commit()
        
        # ✅ 3. Invia notifica e email al cliente
        client = session.get(User, booking.client_user_id)
        consultant = session.get(User, booking.consultant_user_id)
        
        if client:
            client_name = f"{client.nome} {client.cognome}" if client.nome else "Cliente"
            consultant_name = f"{consultant.nome} {consultant.cognome}" if consultant and consultant.nome else "Il consulente"
            
            # Prepara la sezione motivo (opzionale).
            # Il motivo è testo libero scritto dal consulente e finisce in una
            # email HTML: va escapato, altrimenti può iniettarci markup.
            reason_section = ""
            if refuse_reason:
                from html import escape as _html_escape
                reason_section = f"""
            <div style="background: #fff3cd; padding: 15px; border-radius: 5px; border-left: 4px solid #ffc107; margin: 20px 0;">
                <p><strong>📝 Motivo del rifiuto:</strong></p>
                <p>{_html_escape(refuse_reason)}</p>
            </div>
            """
            
            # Notifica nel sistema usando il servizio centralizzato
            send_notification(
                user_id=booking.client_user_id,
                type_key='booking_refused',
                title="Consulenza Rifiutata",
                message=f"{consultant_name} ha rifiutato la tua consulenza del {booking.booking_date.strftime('%d/%m/%Y')} alle {con_fuso(booking.start_time)}",
                template_data={
                    'client_name': client_name,
                    'consultant_name': consultant_name,
                    'date': booking.booking_date.strftime('%d/%m/%Y'),
                    'time': booking.start_time,
                    'reason_section': reason_section,
                    'refund_note': NOTA_BLOCCO_ANNULLATO if booking.payment_status == 'voided' else NOTA_RIMBORSO,
                    'action_url': f"{os.getenv('BASE_URL', 'http://localhost:8080')}/profile#bookings"
                },
                related_booking_id=booking_id,
                action_url="/profile#bookings"
            )
        
        return {
            "success": True,
            "message": "Consulenza rifiutata con successo",
            "booking_id": booking_id,
            "refunded": booking.payment_status in ("refunded", "voided")
        }


@router.post("/api/booking/{booking_id}/accept")
async def accept_booking(booking_id: int, request: Request):
    """Il consulente accetta una richiesta di consulenza: si incassa l'importo bloccato."""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autenticato")

    # Stesso lock di /join: due click su "Accetta" non devono incassare due volte
    async with get_booking_lock(booking_id):
        with Session(engine) as session:
            booking = session.get(Booking, booking_id)
            if not booking:
                raise HTTPException(status_code=404, detail="Prenotazione non trovata")
            try:
                await asyncio.to_thread(accetta_richiesta, session, booking, current_user.id)
            except RichiestaNonValida as e:
                raise HTTPException(status_code=e.status_code, detail=e.messaggio)
            return {"success": True, "booking_id": booking_id, "status": booking.status}


# ========== SCREEN SHARE STATE TRACKING ==========

# Variabile in-memory per tracciare lo stato di screen share per booking
# In produzione, usare Redis per multi-server
_screen_share_state = {}

@router.post("/api/booking/{booking_id}/screen-share/start")
async def screen_share_start(
    request: Request,
    booking_id: int
):
    """Notifica che un utente sta iniziando a condividere lo schermo - con state tracking"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia coinvolto nella prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Traccia chi sta condividendo lo schermo
        _screen_share_state[booking_id] = {
            'is_sharing': True,
            'user_id': current_user.id,
            'timestamp': datetime.now(ITALY_TZ)
        }
        
        print(f"📺 Screen share avviato per booking {booking_id} da utente {current_user.id}")
        
        return {"success": True, "message": "Screen share avviato"}


@router.post("/api/booking/{booking_id}/screen-share/stop")
async def screen_share_stop(
    request: Request,
    booking_id: int
):
    """Notifica che un utente ha smesso di condividere lo schermo - con state tracking"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia coinvolto nella prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Pulisci lo stato di screen share
        if booking_id in _screen_share_state:
            del _screen_share_state[booking_id]
        
        print(f"🎥 Screen share fermato per booking {booking_id} da utente {current_user.id}")
        
        return {"success": True, "message": "Screen share fermato"}


@router.get("/api/booking/{booking_id}/screen-share/status")
async def screen_share_status(
    request: Request,
    booking_id: int
):
    """Ottiene lo stato di screen share per il booking"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia coinvolto nella prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Determina chi è l'altro utente
        other_user_id = booking.consultant_user_id if current_user.id == booking.client_user_id else booking.client_user_id
        
        # Controlla se c'è screen share attivo
        share_state = _screen_share_state.get(booking_id)
        
        # Se c'è screen share, verifica se è dell'altro utente
        is_remote_sharing = share_state is not None and share_state['user_id'] != current_user.id
        
        return {
            "booking_id": booking_id,
            "is_remote_sharing": is_remote_sharing,
            "sharing_user_id": share_state['user_id'] if share_state else None,
            "current_user_id": current_user.id
        }


# ========== CALL TIME VALIDATION ==========

@router.get("/api/booking/{booking_id}/call-status")
async def call_status(
    request: Request,
    booking_id: int
):
    """Verifica se la call è ancora attiva (non è scaduta)"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia coinvolto nella prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Calcola l'orario di scadenza: end_time + 5 minuti
        now_italy = datetime.now(ITALY_TZ)
        
        # Combina booking_date + end_time (converte end_time da stringa a time)
        end_time_obj = datetime.strptime(booking.end_time, "%H:%M").time()
        end_datetime_naive = datetime.combine(booking.booking_date, end_time_obj)
        end_datetime = end_datetime_naive.replace(tzinfo=ITALY_TZ)
        call_deadline = end_datetime + timedelta(minutes=5)

        # Recensione o contestazione: la call è chiusa, non rientrabile.
        motivo_chiusura = motivo_chiusura_call(session, booking_id)
        if motivo_chiusura:
            return {
                "booking_id": booking_id,
                "is_active": False,
                "is_expired": True,
                "closed_by_review": motivo_chiusura == "review",
                "closed_by_dispute": motivo_chiusura == "dispute",
                "remaining_seconds": 0,
                "end_time": booking.end_time,
                "booking_date": booking.booking_date.isoformat(),
                "client_joined": booking.client_joined_at is not None,
            }

        # Controlla se la call è scaduta
        is_expired = now_italy >= call_deadline

        # Secondi rimanenti
        remaining_seconds = int((call_deadline - now_italy).total_seconds())

        print(f"📞 Call status check - booking {booking_id}: now={now_italy}, deadline={call_deadline}, expired={is_expired}, remaining={remaining_seconds}s")
        
        return {
            "booking_id": booking_id,
            "is_active": not is_expired,
            "is_expired": is_expired,
            "remaining_seconds": max(0, remaining_seconds),
            "end_time": booking.end_time,
            "booking_date": booking.booking_date.isoformat(),
            "client_joined": booking.client_joined_at is not None
        }


@router.post("/api/booking/{booking_id}/call-end")
async def call_end(
    request: Request,
    booking_id: int
):
    """Marca la call come terminata e aggiorna lo stato del booking"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia coinvolto nella prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Calcola l'orario di scadenza: end_time + 5 minuti
        now_italy = datetime.now(ITALY_TZ)
        
        # Combina booking_date + end_time (converte end_time da stringa a time)
        end_time_obj = datetime.strptime(booking.end_time, "%H:%M").time()
        end_datetime_naive = datetime.combine(booking.booking_date, end_time_obj)
        end_datetime = end_datetime_naive.replace(tzinfo=ITALY_TZ)
        call_deadline = end_datetime + timedelta(minutes=5)
        
        # Controlla se il tempo è scaduto
        if now_italy >= call_deadline:
            print(f"✅ Call terminata per booking {booking_id} - deadline scaduto")
            # Pulisci lo stato di screen share se esiste
            if booking_id in _screen_share_state:
                del _screen_share_state[booking_id]
            
            return {
                "success": True,
                "message": "Call terminata",
                "booking_id": booking_id,
                "reason": "deadline_exceeded"
            }
        else:
            remaining_minutes = int((call_deadline - now_italy).total_seconds() / 60)
            print(f"⚠️ Tentativo di terminare call prematuramente - booking {booking_id}: {remaining_minutes} minuti rimasti")
            
            return {
                "success": False,
                "message": f"Call non può essere terminata - rimangono {remaining_minutes} minuti",
                "booking_id": booking_id,
                "reason": "time_remaining",
                "remaining_minutes": remaining_minutes
            }


@router.post("/api/booking/{booking_id}/call-start")
async def mark_call_started(
    request: Request,
    booking_id: int
):
    """Marca il momento in cui la call viene avviata (per permettere ripresa)"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia coinvolto nella prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Marca il momento in cui la call è stata avviata (se non già marcata).
        # Ora italiana SENZA fuso, come gli orari delle prenotazioni. Un valore
        # con fuso finiva su PostgreSQL come timestamptz e, entrando in una
        # colonna senza fuso, veniva convertito nel fuso della sessione (UTC su
        # Render): si salvava 13:30 invece di 15:30 e al ricaricamento della
        # pagina il cronometro della call saltava avanti di due ore.
        if booking.call_started_at is None:
            booking.call_started_at = now_italy_naive()
            session.add(booking)
            session.commit()
            print(f"📞 Call avviata per booking {booking_id} da utente {current_user.id}")
        
        return {
            "success": True,
            "call_started_at": iso_ora_italiana(booking.call_started_at)
        }


@router.get("/api/booking/{booking_id}/call-status-extended")
async def get_call_status_extended(
    request: Request,
    booking_id: int
):
    """Verifica stato della call: se scaduta, se già avviata, se può essere ripresa"""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia coinvolto nella prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Calcola deadline della call
        now_italy = datetime.now(ITALY_TZ)
        end_time_obj = datetime.strptime(booking.end_time, "%H:%M").time()
        end_datetime_naive = datetime.combine(booking.booking_date, end_time_obj)
        end_datetime = end_datetime_naive.replace(tzinfo=ITALY_TZ)
        call_deadline = end_datetime + timedelta(minutes=5)
        
        # Stato della call
        # Recensione o contestazione chiudono la consulenza (stessa regola di
        # /call-status): il profilo non deve più proporre "Entra in call".
        motivo_chiusura = motivo_chiusura_call(session, booking_id)
        closed_by_review = motivo_chiusura == "review"
        closed_by_dispute = motivo_chiusura == "dispute"

        is_expired = now_italy >= call_deadline or motivo_chiusura is not None
        call_has_started = booking.call_started_at is not None
        can_resume = call_has_started and not is_expired
        
        remaining_seconds = int((call_deadline - now_italy).total_seconds()) if not is_expired else 0

        return {
            "booking_id": booking_id,
            "is_expired": is_expired,
            "closed_by_review": closed_by_review,
            "closed_by_dispute": closed_by_dispute,
            "call_has_started": call_has_started,
            "can_resume": can_resume,
            "remaining_seconds": max(0, remaining_seconds),
            "call_started_at": iso_ora_italiana(booking.call_started_at)
        }


# ========== CALL CHAT ENDPOINTS ==========

# Lazy singleton per il client S3 degli allegati chat
_chat_s3_client = None

def _get_chat_s3_client():
    """Restituisce un client S3 riutilizzabile (creato una sola volta)"""
    global _chat_s3_client
    if _chat_s3_client is None:
        import boto3
        aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        s3_region = os.getenv("AWS_REGION", "eu-west-1")
        if not aws_access_key or not aws_secret_key:
            return None
        _chat_s3_client = boto3.client(
            "s3",
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=s3_region
        )
    return _chat_s3_client


# Estensioni che il browser può mostrare da sé, con il tipo con cui servirle.
# Tutto il resto (Office, zip...) si può solo scaricare.
_ALLEGATI_VISUALIZZABILI = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
}


def _nome_file_sicuro(nome: str, estensione: str) -> str:
    """Nome ASCII senza caratteri speciali, usabile nella chiave S3 e negli header.

    Un nome con accenti o virgolette ("fattura_è.pdf") finiva così com'era
    nell'header Content-Disposition e l'upload falliva.
    """
    import re
    import unicodedata

    base = unicodedata.normalize("NFKD", nome or "").encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    if not base.lower().endswith(f".{estensione}"):
        base = f"{base}.{estensione}"
    return base[-120:]


def _chiave_allegato_chat(booking_id: int, url: str) -> Optional[str]:
    """Chiave S3 dell'allegato, solo se appartiene alla chat di questo booking.

    Gli allegati sono salvati nei messaggi come URL S3: accettiamo solo quelli
    del nostro bucket e sotto call-attachments/<booking_id>/, così nessuno può
    usare l'endpoint per leggere altri file del bucket o di altre consulenze.
    """
    from urllib.parse import unquote, urlparse

    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    bucket = os.getenv("S3_BUCKET_NAME", "ispiramy-images")
    if parsed.scheme != "https" or not parsed.netloc.startswith(f"{bucket}.s3."):
        return None
    chiave = unquote(parsed.path.lstrip("/"))
    prefisso = f"call-attachments/{booking_id}/"
    resto = chiave[len(prefisso):]
    if not chiave.startswith(prefisso) or not resto or "/" in resto or ".." in resto:
        return None
    return chiave


def _nome_da_chiave(chiave: str) -> str:
    """Nome file dalla chiave <user>_<data>_<ora>_<micro>_<nome>."""
    ultimo = chiave.rsplit("/", 1)[-1]
    parti = ultimo.split("_", 4)
    return parti[4] if len(parti) == 5 else ultimo


@router.post("/api/booking/{booking_id}/background/check")
async def verifica_sfondo_call(
    booking_id: int,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Controlla l'immagine che l'utente vuole usare come sfondo in call.

    Lo sfondo lo vede l'altra persona, quindi passa dagli stessi controlli
    degli allegati e delle immagini della community. L'immagine arriva gia'
    ridotta dal browser (1280x720) e non viene salvata da nessuna parte: serve
    solo per la verifica.
    """
    from app.utils.ai_service import modera_immagine_chat

    enforce_rate_limit(
        request, "sfondo_call", limit=20, window_seconds=3600,
        extra_key=str(current_user.id),
        message="Hai provato troppe immagini di sfondo. Riprova fra {attesa} secondi.",
    )

    with Session(engine) as session:
        booking = session.exec(
            select(Booking.client_user_id, Booking.consultant_user_id)
            .where(Booking.id == booking_id)
        ).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Prenotazione non trovata")
    if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
        raise HTTPException(status_code=403, detail="Non autorizzato")

    body = await request.json()
    immagine = body.get("image") or ""
    if not immagine.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="Immagine non valida")
    if len(immagine) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Immagine troppo pesante")

    esito = await modera_immagine_chat(immagine)
    if not esito["approved"]:
        logger.warning(f"🚫 Sfondo call rifiutato per l'utente {current_user.id}: {esito['reason']}")
    return {"approved": esito["approved"], "reason": esito["reason"]}


@router.post("/api/booking/{booking_id}/chat/upload-attachment")
async def upload_chat_attachment(
    booking_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """Carica un allegato su S3 per la chat della call"""
    try:
        with Session(engine) as session:
            booking = session.exec(
                select(Booking.client_user_id, Booking.consultant_user_id)
                .where(Booking.id == booking_id)
            ).first()
            if not booking:
                raise HTTPException(status_code=404, detail="Booking non trovato")
            if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
                raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Limita a 50MB, controllando PRIMA di bufferizzare tutto in memoria
        MAX_ATTACHMENT = 50 * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_ATTACHMENT + 8192:
            raise HTTPException(status_code=413, detail="File troppo grande (max 50MB)")

        contents = await file.read(MAX_ATTACHMENT + 1)
        file_size = len(contents)
        if file_size > MAX_ATTACHMENT:
            raise HTTPException(status_code=413, detail="File troppo grande (max 50MB)")
        
        # Validazione tipo file
        allowed_extensions = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'zip', 'csv', 'ppt', 'pptx'}
        file_extension = file.filename.split('.')[-1].lower() if file.filename and '.' in file.filename else ''
        if file_extension not in allowed_extensions:
            raise HTTPException(status_code=400, detail=f"Tipo file non supportato. Formati accettati: {', '.join(allowed_extensions)}")

        # Moderazione immagini in tempo reale: blocca contenuti osceni/offensivi prima dell'upload.
        # Limitata a immagini fino a 12MB per non inviare base64 enormi alla Moderation API;
        # le immagini più grandi saltano il controllo live e restano coperte dalla verifica dispute.
        if file_extension in {'jpg', 'jpeg', 'png', 'gif', 'webp'} and file_size <= 12 * 1024 * 1024:
            from app.utils.ai_service import modera_immagine_chat
            import base64
            mime = 'jpeg' if file_extension == 'jpg' else file_extension
            data_url = f"data:image/{mime};base64,{base64.b64encode(contents).decode()}"
            moderazione_img = await modera_immagine_chat(data_url)
            if not moderazione_img["approved"]:
                logger.warning(f"🚫 Immagine chat rifiutata nel booking {booking_id} da user {current_user.id}: {moderazione_img['reason']}")
                raise HTTPException(status_code=400, detail=moderazione_img["reason"])

        # Ottieni client S3 riutilizzabile
        s3_client = _get_chat_s3_client()
        if not s3_client:
            raise HTTPException(status_code=500, detail="Servizio upload non disponibile")
        
        s3_bucket = os.getenv("S3_BUCKET_NAME", "ispiramy-images")
        s3_region = os.getenv("AWS_REGION", "eu-west-1")
        
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        safe_filename = _nome_file_sicuro(file.filename, file_extension)
        s3_key = f"call-attachments/{booking_id}/{current_user.id}_{timestamp}_{safe_filename}"
        
        # Determina il content-type per il download
        content_type = file.content_type or "application/octet-stream"
        
        # Esegui l'upload in un thread separato per non bloccare l'event loop
        await asyncio.to_thread(
            s3_client.put_object,
            Bucket=s3_bucket,
            Key=s3_key,
            Body=contents,
            ContentType=content_type,
            ContentDisposition=f'attachment; filename="{safe_filename}"'
        )
        
        s3_url = f"https://{s3_bucket}.s3.{s3_region}.amazonaws.com/{s3_key}"
        logger.info(f"📎 Allegato call caricato su S3: {safe_filename} ({file_size} bytes) per booking {booking_id}")
        
        return {
            "success": True,
            "url": s3_url,
            "filename": file.filename,
            "file_size": file_size,
            "file_type": content_type
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Errore upload allegato call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/booking/{booking_id}/chat/send")
async def send_call_message(
    booking_id: int,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Invia un messaggio durante la call con allegati opzionali (URL S3)"""
    
    try:
        import json
        
        body = await request.json()
        message_text = body.get("message", "").strip()
        # Lista di {url, filename, file_size, file_type} restituiti da upload-attachment.
        # Arriva dal browser: teniamo solo gli URL della chat di questo booking
        # (niente link esterni o javascript: mostrati all'altro partecipante)
        # e solo i campi attesi.
        attachments_data = []
        for att in body.get("attachments") or []:
            if not isinstance(att, dict) or not _chiave_allegato_chat(booking_id, att.get("url")):
                continue
            try:
                dimensione = max(0, int(att.get("file_size") or 0))
            except (TypeError, ValueError):
                dimensione = 0
            attachments_data.append({
                "url": att["url"],
                "filename": str(att.get("filename") or "file")[:255],
                "file_size": dimensione,
                "file_type": str(att.get("file_type") or "")[:100],
            })

        if not message_text and not attachments_data:
            raise HTTPException(status_code=400, detail="Messaggio o allegato richiesto")

        # Moderazione contenuti in tempo reale: blocca testo osceno/offensivo
        if message_text:
            from app.utils.ai_service import modera_testo_chat
            moderazione = await modera_testo_chat(message_text)
            if not moderazione["approved"]:
                logger.warning(f"🚫 Messaggio chat rifiutato nel booking {booking_id} da user {current_user.id}: {moderazione['reason']}")
                raise HTTPException(status_code=400, detail=moderazione["reason"])

        # Usa current_user già disponibile dal Depends (evita query extra)
        user_name = f"{current_user.nome} {current_user.cognome}" if current_user.nome and current_user.cognome else current_user.email
        
        with Session(engine) as session:
            booking = session.exec(
                select(Booking.client_user_id, Booking.consultant_user_id)
                .where(Booking.id == booking_id)
            ).first()
            
            if not booking:
                raise HTTPException(status_code=404, detail="Booking non trovato")
            
            if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
                raise HTTPException(status_code=403, detail="Non autorizzato")
            
            # Crea il messaggio
            call_msg = CallMessage(
                booking_id=booking_id,
                user_id=current_user.id,
                message=message_text,
                attachments=json.dumps(attachments_data) if attachments_data else None
            )
            session.add(call_msg)
            session.commit()
            session.refresh(call_msg)
            
            logger.info(f"💬 Messaggio call inviato nel booking {booking_id} da {user_name} con {len(attachments_data)} allegati")
            
            return {
                "id": call_msg.id,
                "user_id": call_msg.user_id,
                "user_name": user_name,
                "message": call_msg.message,
                "attachments": attachments_data,
                "created_at": call_msg.created_at.isoformat()
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Errore invio messaggio call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/booking/{booking_id}/chat/messages")
async def get_call_messages(
    booking_id: int,
    current_user: User = Depends(get_current_user),
    after_id: Optional[int] = None
):
    """Ottiene i messaggi della call, opzionalmente solo quelli dopo after_id"""
    
    try:
        import json
        
        with Session(engine) as session:
            # Verifica che il booking esista e che l'utente sia parte della call
            booking = session.exec(
                select(Booking).where(Booking.id == booking_id)
            ).first()
            
            if not booking:
                raise HTTPException(status_code=404, detail="Booking non trovato")
            
            # Verifica che l'utente sia il client o il consultant
            if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
                raise HTTPException(status_code=403, detail="Non autorizzato")
            
            # Query messaggi, filtra per after_id se presente
            query = select(CallMessage).where(CallMessage.booking_id == booking_id)
            if after_id is not None:
                query = query.where(CallMessage.id > after_id)
            query = query.order_by(CallMessage.created_at)
            
            messages = session.exec(query).all()
            
            if not messages:
                return {"messages": []}
            
            # Fetch tutti gli utenti coinvolti in una sola query
            user_ids = list(set(msg.user_id for msg in messages))
            users = session.exec(select(User).where(User.id.in_(user_ids))).all()
            user_map = {u.id: u for u in users}
            
            # Costruisci la risposta
            messages_data = []
            for msg in messages:
                user = user_map.get(msg.user_id)
                attachments = json.loads(msg.attachments) if msg.attachments else []
                messages_data.append({
                    "id": msg.id,
                    "user_id": msg.user_id,
                    "user_name": f"{user.nome} {user.cognome}" if user and user.nome and user.cognome else (user.email if user else "Utente"),
                    "message": msg.message,
                    "attachments": attachments,
                    "created_at": msg.created_at.isoformat()
                })
            
            return {"messages": messages_data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Errore lettura messaggi call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/booking/{booking_id}/chat/attachment")
async def open_call_attachment(
    booking_id: int,
    url: str,
    mode: str = "view",
    current_user: User = Depends(get_current_user)
):
    """Apre un allegato della chat: mode=view lo mostra nel browser, mode=download lo scarica.

    I file sono stati caricati con "Content-Disposition: attachment", quindi il
    link S3 diretto li scaricava sempre (una foto non si poteva guardare).
    Qui si firma un link temporaneo che sovrascrive disposition e tipo.
    """
    from urllib.parse import quote

    with Session(engine) as session:
        booking = session.exec(
            select(Booking.client_user_id, Booking.consultant_user_id)
            .where(Booking.id == booking_id)
        ).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking non trovato")
    if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
        raise HTTPException(status_code=403, detail="Non autorizzato")

    chiave = _chiave_allegato_chat(booking_id, url)
    if not chiave:
        raise HTTPException(status_code=404, detail="Allegato non trovato")

    s3_client = _get_chat_s3_client()
    if not s3_client:
        raise HTTPException(status_code=503, detail="Servizio allegati non disponibile")

    nome = _nome_da_chiave(chiave)
    estensione = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
    params = {"Bucket": os.getenv("S3_BUCKET_NAME", "ispiramy-images"), "Key": chiave}
    if mode == "view" and estensione in _ALLEGATI_VISUALIZZABILI:
        params["ResponseContentDisposition"] = "inline"
        params["ResponseContentType"] = _ALLEGATI_VISUALIZZABILI[estensione]
    else:
        # I file caricati prima della sanificazione possono avere accenti o
        # virgolette nel nome: nell'header va la versione ASCII, più quella UTF-8.
        nome_ascii = _nome_file_sicuro(nome, estensione) if estensione else "file"
        params["ResponseContentDisposition"] = (
            f'attachment; filename="{nome_ascii}"; filename*=UTF-8\'\'{quote(nome)}'
        )

    try:
        link = s3_client.generate_presigned_url("get_object", Params=params, ExpiresIn=300)
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Link allegato call non generato ({chiave}): {e}")
        raise HTTPException(status_code=500, detail="Impossibile aprire l'allegato")
    return RedirectResponse(url=link, status_code=302)


# ========== AUTO RECORDING ENDPOINTS ==========

def _stop_recording_background(booking_id: int, resource_id: str, sid: str, channel_name: str):
    """Background task: ferma recording e salva risultato nel DB"""
    from app.utils.agora_recording import stop_recording, get_recording_url
    logger.info(f"🎥 [BG] Background recording stop for booking {booking_id}")
    try:
        result = stop_recording(resource_id, sid, channel_name, 0)
        logger.info(f"🎥 [BG] stop_recording result: {result}")
        
        with Session(engine) as session:
            booking = session.get(Booking, booking_id)
            if not booking:
                return
            if result:
                file_name = result["file_name"]
                recording_url = get_recording_url(file_name)
                booking.recording_url = recording_url
                booking.recording_duration = result.get("mix_duration", 0)
                booking.recording_status = "completed"
                booking.recording_completed_at = datetime.utcnow()
                logger.info(f"✅ [BG] Recording saved: {recording_url}")
            else:
                booking.recording_status = "failed"
                logger.warning(f"⚠️ [BG] Recording stop returned no result")
            booking.updated_at = datetime.utcnow()
            session.add(booking)
            session.commit()
    except Exception as e:
        logger.error(f"❌ [BG] Error stopping recording: {e}", exc_info=True)
        try:
            with Session(engine) as session:
                booking = session.get(Booking, booking_id)
                if booking:
                    booking.recording_status = "failed"
                    booking.updated_at = datetime.utcnow()
                    session.add(booking)
                    session.commit()
        except Exception:
            pass


@router.post("/api/booking/{booking_id}/leave")
async def leave_booking(booking_id: int, request: Request, background_tasks: BackgroundTasks):
    """Segna che l'utente è uscito dalla call - ferma recording se nessuno rimane"""
    print(f"\n🔔 [LEAVE] Function called for booking {booking_id}")
    current_user = get_current_user(request)
    if not current_user:
        # sendBeacon potrebbe non avere sessione — ignora silenziosamente
        return {"success": True, "message": "Leave acknowledged"}
    
    print(f"🔔 [LEAVE] Current user: {current_user.email}")
    
    with Session(engine) as session:
        booking = session.get(Booking, booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="Prenotazione non trovata")
        
        # Verifica che l'utente sia parte della prenotazione
        if current_user.id not in [booking.client_user_id, booking.consultant_user_id]:
            raise HTTPException(status_code=403, detail="Non autorizzato")
        
        # Segna l'uscita dell'utente
        is_client = booking.client_user_id == current_user.id
        now = now_italy_naive()

        print(f"🔔 [LEAVE] Booking found: {booking_id}, is_client={is_client}, recording_status={booking.recording_status}")
        logger.info(f"👋 User leaving booking {booking_id} (is_client={is_client})")

        # NB: client_joined_at / consultant_joined_at NON vanno azzerati: sono la
        # prova che la consulenza si è svolta, e il job no-show ci si basa per
        # decidere se rimborsare. La presenza istantanea è tracciata a parte.
        still_present = mark_absent(booking_id, current_user.id)

        booking.updated_at = now

        # Controlla se rimane qualcuno in call
        client_still_in = booking.client_user_id in still_present
        consultant_still_in = booking.consultant_user_id in still_present
        anyone_in_call = bool(still_present)
        
        print(f"🔔 [LEAVE] After update - client_still_in={client_still_in}, consultant_still_in={consultant_still_in}, anyone_in_call={anyone_in_call}")
        
        # 🎥 Se nessuno rimane in call e la registrazione è attiva, ferma in background
        # Usa un update atomico: imposta "stopping" solo se ancora "recording"
        should_stop = False
        recording_resource_id = booking.recording_resource_id
        recording_sid = booking.recording_sid
        
        if not anyone_in_call and booking.recording_status == "recording":
            booking.recording_status = "stopping"
            session.add(booking)
            session.commit()
            session.refresh(booking)
            # Verifica che siamo noi ad averlo impostato a "stopping"
            if booking.recording_status == "stopping":
                should_stop = True
                logger.info(f"🎥 All users left - scheduling background recording stop for booking {booking_id}")
                channel_name = f"booking_{booking_id}"
                background_tasks.add_task(
                    _stop_recording_background,
                    booking_id,
                    recording_resource_id,
                    recording_sid,
                    channel_name
                )
        else:
            session.add(booking)
            session.commit()
            if anyone_in_call:
                logger.info(f"ℹ️ User left but others still in call - keeping recording active")
            else:
                logger.info(f"ℹ️ Recording not active (status={booking.recording_status}) - nothing to stop")
        
        print(f"🔔 [LEAVE] Booking saved - recording stop scheduled in background: {should_stop}")
        
        return {
            "success": True,
            "has_left": True,
            "client_in_call": client_still_in,
            "consultant_in_call": consultant_still_in,
            "still_has_participants": anyone_in_call,
            "recording_status": booking.recording_status
        }
