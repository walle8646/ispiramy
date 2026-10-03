from fastapi import APIRouter, Request, Form, HTTPException, Query, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from app.database import get_session
from app.models import User, Conversation, Message, Booking, ConfigurationProperty, CommunityContact, CommunityQuestion
from sqlmodel import select, or_, and_, func
from datetime import datetime, timedelta
from app.logger_config import logger
from typing import Optional
import os

# ✅ Importa funzioni autenticazione da auth.py
from app.routes.auth import verify_token
from app.utils_user import has_payment_method, get_display_name
from app.utils.notification_service import send_notification
from app.utils.orari import now_italy_naive
from app.utils.rate_limit import enforce_rate_limit
from app.utils.ai_service import modera_testo_chat

router = APIRouter()

# ========== CONFIGURAZIONE LIMITI ==========
# Valori di ripiego, usati solo se la tabella configuration_property non è
# raggiungibile o non contiene la chiave.
# 60 è il limite della conversazione: banner, contatore e blocco all'invio
# leggono questo stesso numero. Prima il banner ne aveva uno suo, 15, e il
# backend un altro, 80.
DEFAULT_MAX_MESSAGES_PER_CONVERSATION = 60
DEFAULT_MAX_MESSAGE_LENGTH = 1000

# Valori spediti in passato e poi smentiti. 15 era scritto nel banner della
# chat. 80 era il default del backend e della riga inserita a suo tempo in
# configuration_property. Se in tabella c'è ancora uno di questi, lo si porta
# a 60. Un valore diverso, scelto di proposito, non si tocca.
_LIMITI_MESSAGGI_OBSOLETI = ("15", "80")

# Chiavi in configuration_property. Attenzione: /api/chat-config leggeva
# 'max_messages' e 'max_length', che nel database non esistono; ricadeva
# sempre sui default e la configurazione impostata dall'amministratore non
# aveva alcun effetto — per di più su valori diversi da quelli applicati
# davvero dal backend.
CONFIG_KEY_MAX_MESSAGES = "MAX_MESSAGES_PER_CONVERSATION"
CONFIG_KEY_MAX_LENGTH = "MAX_MESSAGE_LENGTH"

# Piccola cache: questi valori cambiano di rado ma verrebbero letti a ogni
# messaggio inviato e a ogni apertura della chat.
_config_cache: dict[str, tuple[float, int]] = {}
_CONFIG_TTL = 60  # secondi


def get_config_int(key: str, default: int) -> int:
    """Legge un intero da configuration_property, con cache e ripiego."""
    import time as _time

    scadenza = _config_cache.get(key)
    if scadenza and _time.time() - scadenza[0] < _CONFIG_TTL:
        return scadenza[1]

    valore = default
    try:
        with get_session() as session:
            riga = session.exec(
                select(ConfigurationProperty)
                .where(ConfigurationProperty.property_key == key)
            ).first()
            if riga and riga.property_value is not None:
                valore = int(riga.property_value)
    except (ValueError, TypeError):
        logger.warning(f"⚠️ Valore non numerico per la configurazione '{key}', uso {default}")
    except Exception as e:  # noqa: BLE001 — la chat non deve rompersi per la config
        logger.warning(f"⚠️ Configurazione '{key}' non leggibile ({e}), uso {default}")

    _config_cache[key] = (_time.time(), valore)
    return valore


def max_messaggi_per_conversazione() -> int:
    return get_config_int(CONFIG_KEY_MAX_MESSAGES, DEFAULT_MAX_MESSAGES_PER_CONVERSATION)


def max_lunghezza_messaggio() -> int:
    return get_config_int(CONFIG_KEY_MAX_LENGTH, DEFAULT_MAX_MESSAGE_LENGTH)


def svuota_cache_configurazione() -> None:
    """Usata dai test e dopo un cambio di configurazione."""
    _config_cache.clear()


def frase_messaggi(n: int) -> str:
    """'1 messaggio' oppure 'N messaggi': il numero e la parola devono accordarsi."""
    numero = int(n)
    if numero == 1:
        return "1 messaggio"
    return f"{numero} messaggi"


def campi_limite(total_messages: int) -> dict:
    """Gli stessi numeri che il server applica al blocco, da mostrare in chat.

    Il banner non deve avere un tetto suo: legge max_messages e messages_left
    da qui, cioè da max_messaggi_per_conversazione().
    """
    massimo = max_messaggi_per_conversazione()
    return {
        "max_messages": massimo,
        "messages_left": max(0, massimo - total_messages),
        "limit_reached": total_messages >= massimo,
    }


def allinea_limite_messaggi_obsoleto() -> bool:
    """Porta a 60 un limite ancora fermo su 15 o su 80.

    Ritorna True se ha aggiornato la riga. Un valore diverso resta com'è,
    così una configurazione scelta di proposito non viene riscritta a ogni avvio.
    """
    try:
        with get_session() as session:
            riga = session.exec(
                select(ConfigurationProperty)
                .where(ConfigurationProperty.property_key == CONFIG_KEY_MAX_MESSAGES)
            ).first()
            if not riga or str(riga.property_value) not in _LIMITI_MESSAGGI_OBSOLETI:
                return False
            vecchio = riga.property_value
            riga.property_value = str(DEFAULT_MAX_MESSAGES_PER_CONVERSATION)
            riga.updated_at = datetime.utcnow()
            session.add(riga)
            session.commit()
    except Exception as e:  # noqa: BLE001 — l'avvio non deve fallire per la config
        logger.warning(f"⚠️ Limite messaggi non allineato ({e})")
        return False
    svuota_cache_configurazione()
    logger.info(
        f"Limite messaggi per conversazione portato da {vecchio} a "
        f"{DEFAULT_MAX_MESSAGES_PER_CONVERSATION}"
    )
    return True


def conta_messaggi_conversazione(session, conversation: Conversation) -> int:
    """Messaggi che contano ai fini del limite della conversazione.

    Il conteggio riparte dall'ultima consulenza **pagata** fra i due utenti:
    prenotare sblocca la chat. Prima il filtro era `status == 'confirmed'`, ma
    15 minuti dopo la consulenza il job no-show porta lo stato a 'completed' o
    'no_show': la riga smetteva di corrispondere, il conteggio tornava a
    includere tutta la cronologia e la conversazione si richiudeva da sola —
    stavolta per sempre, perché il totale può solo crescere.

    Sono esclusi i messaggi di sistema (offerte di consulenza): li genera la
    piattaforma, non devono consumare il credito degli utenti.
    """
    ultima_pagata = session.exec(
        select(Booking)
        .where(
            Booking.payment_status.in_(["paid", "held", "released"]),
            or_(
                and_(
                    Booking.client_user_id == conversation.user1_id,
                    Booking.consultant_user_id == conversation.user2_id,
                ),
                and_(
                    Booking.client_user_id == conversation.user2_id,
                    Booking.consultant_user_id == conversation.user1_id,
                ),
            ),
        )
        .order_by(Booking.created_at.desc())
    ).first()

    query = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.is_system_message == False,  # noqa: E712
        )
    )
    if ultima_pagata:
        query = query.where(Message.created_at > ultima_pagata.created_at)

    return session.exec(query).one()

# ========== API ENDPOINTS ==========

@router.get("/api/current-user")
async def api_get_current_user(request: Request):
    """API endpoint to get current logged-in user info"""
    user = verify_token(request)
    if not user:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    
    return JSONResponse({
        "id": user.id,
        "email": user.email,
        "nome": user.nome,
        "cognome": user.cognome,
        "is_verified": user.is_verified,
        "has_payment_method": has_payment_method(user),
        "category_id": user.category_id
    })

# ========== HELPER FUNCTIONS ==========

def find_conversation(session, user1_id: int, user2_id: int) -> Optional[Conversation]:
    """Cerca la conversazione fra due utenti, senza crearla."""
    return session.exec(
        select(Conversation).where(
            and_(
                Conversation.user1_id == min(user1_id, user2_id),
                Conversation.user2_id == max(user1_id, user2_id)
            )
        )
    ).first()


def get_or_create_conversation(session, user1_id: int, user2_id: int) -> Conversation:
    """Ottieni conversazione esistente o creane una nuova.

    Da usare solo nei percorsi di scrittura: chiamarla su una GET faceva
    nascere una riga di conversazione a ogni apertura di una chat, anche verso
    utenti con cui non si e' mai scambiato un messaggio.
    """
    min_id = min(user1_id, user2_id)
    max_id = max(user1_id, user2_id)

    conversation = find_conversation(session, user1_id, user2_id)

    if not conversation:
        conversation = Conversation(
            user1_id=min_id,
            user2_id=max_id
        )
        session.add(conversation)
        session.commit()
        session.refresh(conversation)
    
    return conversation

# ========== PAGINA LISTA CONVERSAZIONI ==========

@router.get("/messaggi", response_class=HTMLResponse)
async def messages_inbox_page(request: Request):
    """Pagina inbox con lista conversazioni"""
    current_user = verify_token(request)
    
    if not current_user:
        return RedirectResponse("/login?redirect=/messaggi", status_code=302)
    
    return request.app.state.templates.TemplateResponse(
        "messages_inbox.html",
        {
            "request": request,
            "user": current_user  # ✅ USA "user" come in profile.py
        }
    )

# ========== PAGINA CHAT CON UTENTE SPECIFICO ==========

@router.get("/messaggi/{other_user_id}", response_class=HTMLResponse)
async def chat_page(request: Request, other_user_id: int):
    """Pagina chat con un altro utente"""
    current_user = verify_token(request)
    
    if not current_user:
        return RedirectResponse(f"/login?redirect=/messaggi/{other_user_id}", status_code=302)
    
    with get_session() as session:
        other_user = session.get(User, other_user_id)
        
        if not other_user:
            raise HTTPException(status_code=404, detail="Utente non trovato")
        
        if current_user.id == other_user_id:
            raise HTTPException(status_code=400, detail="Non puoi chattare con te stesso")
        
        return request.app.state.templates.TemplateResponse(
            "chat.html",
            {
                "request": request,
                "user": current_user,  # ✅ USA "user"
                "other_user": other_user
            }
        )

# ========== API: Lista Conversazioni ==========

@router.get("/api/conversations")
async def get_conversations(request: Request):
    """Ottieni lista conversazioni dell'utente loggato"""
    # ✅ Usa verify_token da auth.py
    current_user = verify_token(request)
    
    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)
    
    try:
        with get_session() as session:
            user_id = current_user.id
            
            conversations = session.exec(
                select(Conversation)
                .where(
                    or_(
                        Conversation.user1_id == user_id,
                        Conversation.user2_id == user_id
                    )
                )
                .order_by(Conversation.updated_at.desc())
            ).all()
            
            result = []
            for conv in conversations:
                other_user_id = conv.user2_id if conv.user1_id == user_id else conv.user1_id
                other_user = session.get(User, other_user_id)
                
                if not other_user:
                    continue
                
                last_message = session.exec(
                    select(Message)
                    .where(Message.conversation_id == conv.id)
                    .order_by(Message.created_at.desc())
                    .limit(1)
                ).first()
                
                unread_count = session.exec(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        and_(
                            Message.conversation_id == conv.id,
                            Message.sender_id != user_id,
                            Message.is_read == False
                        )
                    )
                ).one()
                
                result.append({
                    "conversation_id": conv.id,
                    "other_user": {
                        "id": other_user.id,
                        "nome": other_user.nome or "Utente",
                        "cognome": other_user.cognome or "",
                        "profile_picture": other_user.profile_picture or None,
                        "genere": other_user.genere or None,
                        "professione": other_user.professione or ""
                    },
                    "last_message": {
                        "id": last_message.id if last_message else None,  # ✅ Aggiunto ID
                        "content": last_message.content if last_message else None,
                        "created_at": last_message.created_at.isoformat() if last_message else None,
                        "is_mine": last_message.sender_id == user_id if last_message else False,
                        "is_sender": last_message.sender_id == user_id if last_message else False  # ✅ Aggiunto is_sender
                    } if last_message else None,
                    "unread_count": unread_count,
                    "updated_at": conv.updated_at.isoformat()
                })
            
            return JSONResponse({"conversations": result}, status_code=200)
    
    except Exception as e:
        logger.error(f"Error getting conversations: {e}", exc_info=True)
        return JSONResponse({"error": "Errore caricamento conversazioni"}, status_code=500)

# ========== API: Ottieni Messaggi ==========

@router.get("/api/messaggi/{other_user_id}")
async def get_messages(
    request: Request,
    other_user_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, le=100)
):
    """Ottieni messaggi di una conversazione"""
    current_user = verify_token(request)
    
    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)
    
    try:
        with get_session() as session:
            user_id = current_user.id

            # Sola lettura: se la conversazione non esiste ancora si risponde
            # con una lista vuota, senza crearla. Prima ogni GET inseriva una
            # riga, e bastava scorrere gli id utente per riempire la tabella.
            conversation = find_conversation(session, user_id, other_user_id)
            if not conversation:
                return JSONResponse({
                    "messages": [],
                    "total": 0,
                    "showing": 0,
                    "has_more": False,
                    **campi_limite(0),
                }, status_code=200)

            # ✅ Ottieni gli ultimi 100 messaggi (modificato da 15)
            messages = session.exec(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc())
                .limit(100)  # ✅ Carica ultimi 100 messaggi
            ).all()
            
            # Messaggi che contano ai fini del limite (riparte dall'ultima
            # consulenza pagata fra i due utenti)
            total_messages = conta_messaggi_conversazione(session, conversation)
            
            # Marca messaggi come letti
            unread_messages = session.exec(
                select(Message)
                .where(
                    and_(
                        Message.conversation_id == conversation.id,
                        Message.sender_id == other_user_id,
                        Message.is_read == False
                    )
                )
            ).all()
            
            for msg in unread_messages:
                msg.is_read = True
                session.add(msg)
            
            if unread_messages:
                session.commit()
            
            result = [
                {
                    "id": msg.id,
                    "content": msg.content,
                    "sender_id": msg.sender_id,
                    "is_sender": msg.sender_id == user_id,  # ✅ Rinominato da is_mine a is_sender per il frontend
                    "is_mine": msg.sender_id == user_id,  # ✅ Mantenuto per backward compatibility
                    "is_system_message": msg.is_system_message if hasattr(msg, 'is_system_message') else False,  # ✅ Aggiunto per messaggi di sistema
                    "created_at": msg.created_at.isoformat(),
                    "is_read": msg.is_read
                }
                for msg in reversed(messages)
            ]
            
            return JSONResponse({
                "messages": result,
                "total": total_messages,
                "showing": len(result),
                "has_more": total_messages > 100,  # ✅ Indica se ci sono più di 100 messaggi
                **campi_limite(total_messages),
            }, status_code=200)
    
    except Exception as e:
        logger.error(f"Error getting messages: {e}", exc_info=True)
        return JSONResponse({"error": "Errore caricamento messaggi"}, status_code=500)

# ========== API: Invia Messaggio ==========

@router.post("/api/messaggi/{other_user_id}")
async def send_message(
    request: Request,
    other_user_id: int,
    content: str = Form(...)
):
    """Invia un messaggio"""
    current_user = verify_token(request)

    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)

    # Limite di frequenza: il limite per conversazione non impedisce di
    # martellare molti destinatari diversi, né di far partire una notifica
    # (email inclusa) a ogni invio.
    enforce_rate_limit(
        request, "invio_messaggi", limit=30, window_seconds=60,
        extra_key=str(current_user.id),
        message="Stai inviando messaggi troppo in fretta. Riprova fra {attesa} secondi.",
    )

    max_lunghezza = max_lunghezza_messaggio()
    max_messaggi = max_messaggi_per_conversazione()

    # ✅ VALIDAZIONE LUNGHEZZA
    if not content or len(content.strip()) == 0:
        return JSONResponse({"error": "Messaggio vuoto"}, status_code=400)

    if len(content) > max_lunghezza:
        return JSONResponse({
            "error": f"Messaggio troppo lungo (max {max_lunghezza} caratteri)"
        }, status_code=400)

    # Stessa moderazione della chat durante la call: prima qui non c'era nulla,
    # e la chat fra utenti era l'unico posto senza controlli sui contenuti.
    moderazione = await modera_testo_chat(content)
    if not moderazione["approved"]:
        logger.warning(f"🚫 Messaggio rifiutato dalla moderazione (utente {current_user.id})")
        return JSONResponse({"error": moderazione["reason"]}, status_code=400)
    
    try:
        with get_session() as session:
            user_id = current_user.id
            
            other_user = session.get(User, other_user_id)
            if not other_user:
                return JSONResponse({"error": "Utente non trovato"}, status_code=404)
            
            if user_id == other_user_id:
                return JSONResponse({"error": "Non puoi inviare messaggi a te stesso"}, status_code=400)
            
            conversation = get_or_create_conversation(session, user_id, other_user_id)
            
            # ⏰ CONTROLLO PER NOTIFICA: primo messaggio O >30 minuti dall'ultimo
            should_notify = False
            
            # Trova l'ultimo messaggio nella conversazione
            last_message = session.exec(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc())
            ).first()
            
            if last_message:
                # Calcola tempo trascorso dall'ultimo messaggio
                time_since_last = datetime.utcnow() - last_message.created_at
                should_notify = time_since_last > timedelta(minutes=30)
            else:
                # Primo messaggio assoluto nella conversazione
                should_notify = True
            
            # ✅ VERIFICA LIMITE MESSAGGI
            total_messages = conta_messaggi_conversazione(session, conversation)

            if total_messages >= max_messaggi:
                return JSONResponse({
                    "error": (
                        f"Hai raggiunto il limite di {frase_messaggi(max_messaggi)} per questa "
                        "conversazione. Prenota una consulenza per continuare a scrivere."
                    ),
                    **campi_limite(total_messages),
                }, status_code=400)
            
            # ✅ CREA MESSAGGIO
            message = Message(
                conversation_id=conversation.id,
                sender_id=user_id,
                content=content.strip(),
                is_read=False
            )
            session.add(message)
            
            conversation.updated_at = datetime.utcnow()
            session.add(conversation)
            
            session.commit()
            session.refresh(message)
            
            # � Incrementa contatore community se questo è il primo messaggio per un contatto pendente
            try:
                pending_contacts = session.exec(
                    select(CommunityContact)
                    .join(CommunityQuestion, CommunityContact.question_id == CommunityQuestion.id)
                    .where(
                        and_(
                            CommunityContact.user_id == user_id,
                            CommunityContact.message_sent == False,
                            CommunityQuestion.user_id == other_user_id
                        )
                    )
                ).all()
                
                for contact in pending_contacts:
                    contact.message_sent = True
                    session.add(contact)
                    question = session.get(CommunityQuestion, contact.question_id)
                    if question:
                        question.views += 1
                        session.add(question)
                        logger.info(f"📬 Community contact confirmed: user {user_id} → question {contact.question_id} (views: {question.views})")
                
                if pending_contacts:
                    session.commit()
            except Exception as cc_error:
                logger.error(f"⚠️ Errore aggiornamento contatti community: {cc_error}")
            
            # �📧🔔 Invia notifica al destinatario SE should_notify è True
            if should_notify:
                try:
                    base_url = os.getenv("BASE_URL", "http://localhost:8080")
                    sender_name = get_display_name(current_user)
                    recipient_name = get_display_name(other_user, include_full_name=False)
                    
                    # Il link porta dritto alla conversazione. Prima puntava a
                    # /messages, che non esiste: sia la campanella sia il
                    # bottone "Apri Chat" dell'email finivano su un 404.
                    percorso_chat = f"/messaggi/{current_user.id}"
                    notification_sent = send_notification(
                        user_id=other_user.id,
                        type_key='community_contact',
                        title="Nuovo messaggio",
                        message=f"{sender_name} vuole contattarti!",
                        template_data={
                            'author_name': recipient_name,
                            'contact_name': sender_name,
                            'question_title': f'Nuovo messaggio da {sender_name}',
                            'contact_date': now_italy_naive().strftime('%d/%m/%Y alle %H:%M'),
                            'action_url': f"{base_url}{percorso_chat}",
                        },
                        related_user_id=current_user.id,
                        action_url=percorso_chat,
                    )
                    
                    if notification_sent:
                        pass
                    else:
                        logger.warning(f"⚠️ Notifica messaggio non inviata a user {other_user.id}")
                        
                except Exception as notif_error:
                    logger.error(f"❌ Errore invio notifica messaggio: {notif_error}")
                    # Non bloccare l'invio del messaggio anche se la notifica fallisce
            
            return JSONResponse({
                "success": True,
                "message": {
                    "id": message.id,
                    "content": message.content,
                    "created_at": message.created_at.isoformat(),
                    "is_mine": True
                },
                **campi_limite(total_messages + 1),
            }, status_code=201)
    
    except Exception as e:
        logger.error(f"Error sending message: {e}", exc_info=True)
        return JSONResponse({"error": "Errore invio messaggio"}, status_code=500)

# ========== API: Elimina Messaggio ==========

@router.delete("/api/messaggi/{message_id}")
async def delete_message(request: Request, message_id: int):
    """Elimina un messaggio (solo il mittente può eliminare)"""
    # ✅ Usa verify_token da auth.py
    current_user = verify_token(request)
    
    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)
    
    try:
        with get_session() as session:
            message = session.get(Message, message_id)
            
            if not message:
                return JSONResponse({"error": "Messaggio non trovato"}, status_code=404)
            
            if message.sender_id != current_user.id:
                return JSONResponse({"error": "Non autorizzato"}, status_code=403)
            
            session.delete(message)
            session.commit()
            
            return JSONResponse({"success": True}, status_code=200)
    
    except Exception as e:
        logger.error(f"Error deleting message: {e}", exc_info=True)
        return JSONResponse({"error": "Errore eliminazione messaggio"}, status_code=500)

# ========== API: Conta Messaggi Non Letti Totali ==========

@router.get("/api/unread-count")
async def get_unread_count(request: Request):
    """Ottieni conteggio totale messaggi non letti"""
    # ✅ Usa verify_token da auth.py
    current_user = verify_token(request)
    
    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)
    
    try:
        with get_session() as session:
            user_id = current_user.id
            
            unread_count = session.exec(
                select(func.count())
                .select_from(Message)
                .join(Conversation)
                .where(
                    and_(
                        or_(
                            Conversation.user1_id == user_id,
                            Conversation.user2_id == user_id
                        ),
                        Message.sender_id != user_id,
                        Message.is_read == False
                    )
                )
            ).one()
            
            return JSONResponse({"unread_count": unread_count}, status_code=200)
    
    except Exception as e:
        logger.error(f"Error getting unread count: {e}", exc_info=True)
        return JSONResponse({"error": "Errore conteggio messaggi"}, status_code=500)

# ========== API: Chat Configuration ==========

@router.get("/api/chat-config")
async def get_chat_config(request: Request):
    """Ottieni configurazione limiti chat (max messaggi e lunghezza).

    Usa le stesse funzioni dell'invio: prima questo endpoint cercava le chiavi
    'max_messages' e 'max_length', che nel database non esistono, e ricadeva su
    default diversi da quelli applicati davvero dal backend — l'interfaccia
    mostrava un limite e il server ne imponeva un altro.
    """
    return JSONResponse({
        "max_messages": max_messaggi_per_conversazione(),
        "max_length": max_lunghezza_messaggio(),
    }, status_code=200)

# ========== API: User Online Status ==========

@router.get("/api/user/{user_id}/status")
async def get_user_status(user_id: int, request: Request):
    """Ottieni lo stato online/offline di un utente"""
    current_user = verify_token(request)
    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)
    
    try:
        with get_session() as session:
            user = session.get(User, user_id)
            if not user:
                return JSONResponse({"error": "Utente non trovato"}, status_code=404)
            
            status_label = "Offline"
            is_online = False
            
            if user.last_seen:
                now = datetime.utcnow()
                diff = now - user.last_seen
                minutes = int(diff.total_seconds() / 60)
                
                if minutes < 5:
                    status_label = "Online ora"
                    is_online = True
                elif minutes < 60:
                    status_label = f"Attivo {minutes} min fa"
                elif minutes < 1440:
                    hours = minutes // 60
                    status_label = f"Attivo {hours} or{'a' if hours == 1 else 'e'} fa"
                elif minutes < 43200:
                    days = minutes // 1440
                    status_label = f"Attivo {days} giorn{'o' if days == 1 else 'i'} fa"
                else:
                    months = minutes // 43200
                    status_label = f"Attivo {months} mes{'e' if months == 1 else 'i'} fa"
            
            return JSONResponse({"status": status_label, "is_online": is_online}, status_code=200)
    
    except Exception as e:
        logger.error(f"Error getting user status: {e}", exc_info=True)
        return JSONResponse({"status": "Offline", "is_online": False}, status_code=200)