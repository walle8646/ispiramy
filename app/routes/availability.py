"""Route per gestione disponibilità consulenze"""
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, select, func, and_
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo
from typing import List, Optional
import logging

from app.database import engine
from app.models import User, AvailabilityBlock, Booking
from app.routes.auth import verify_token
from app.utils.orari import ORE_PREAVVISO_PRENOTAZIONE

import os

ITALY_TZ = ZoneInfo("Europe/Rome")
DEBUG_MODE = os.getenv("DEBUG", "false").lower() == "true"

router = APIRouter()
logger = logging.getLogger(__name__)


def format_time_field(time_value) -> str:
    """
    Formatta un campo time che può essere str (SQLite) o time object (PostgreSQL)
    Returns: stringa in formato HH:MM
    """
    if time_value is None:
        return None
    if isinstance(time_value, str):
        # SQLite restituisce già una stringa "HH:MM:SS" o "HH:MM"
        return time_value[:5]  # Prendi solo HH:MM
    elif isinstance(time_value, time):
        # PostgreSQL restituisce un oggetto time
        return time_value.strftime("%H:%M")
    else:
        # Fallback: converti a stringa
        return str(time_value)[:5]


def _minuti(valore) -> int:
    if isinstance(valore, time):
        return valore.hour * 60 + valore.minute
    parti = str(valore).strip().split(":")
    return int(parti[0]) * 60 + int(parti[1])


def prenotazioni_non_coperte(session, user_id: int, giorno: str, fasce) -> list:
    """Prenotazioni del giorno che non stanno interamente in una delle fasce nuove.

    Togliere la fascia non cancella la consulenza, ma lasciava il calendario
    incoerente: l'ora risultava libera da impostare e occupata da prenotare.
    """
    righe = session.exec(
        select(Booking).where(
            Booking.consultant_user_id == user_id,
            func.date(Booking.booking_date) == giorno,
            Booking.status.notin_(["cancelled", "no_show"]),
        )
    ).all()
    intervalli = [(_minuti(inizio), _minuti(fine)) for inizio, fine in fasce]
    scoperte = []
    for riga in righe:
        inizio = _minuti(riga.start_time)
        fine = _minuti(riga.end_time) if riga.end_time else inizio + int(riga.duration_minutes or 60)
        if fine <= inizio:
            fine += 24 * 60
        if not any(parte <= inizio and fine <= arriva for parte, arriva in intervalli):
            scoperte.append(riga)
    return scoperte


logger = logging.getLogger(__name__)

@router.get("/availability", response_class=HTMLResponse)
async def availability_page(request: Request):
    """Pagina gestione disponibilità"""
    user = verify_token(request)
    if not user:
        return request.app.state.templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Devi effettuare il login per accedere a questa pagina"
        })
    
    return request.app.state.templates.TemplateResponse("availability.html", {
        "request": request,
        "user": user,
        "current_user": user,
        "debug_mode": DEBUG_MODE
    })

@router.get("/api/availability/{date_str}")
async def get_availability_by_date(
    request: Request, 
    date_str: str,
    user_id: Optional[int] = None
):
    """Ottieni disponibilità per una specifica data (formato: YYYY-MM-DD)"""
    current_user = verify_token(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    # Se user_id è specificato, carica le disponibilità di quell'utente
    # Altrimenti carica le disponibilità dell'utente corrente
    target_user_id = user_id if user_id else current_user.id
    
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido")
    
    with Session(engine) as session:
        # Usa confronto con cast a DATE per SQLite compatibility
        statement = select(AvailabilityBlock).where(
            AvailabilityBlock.user_id == target_user_id,
            func.date(AvailabilityBlock.date) == date_str,
            AvailabilityBlock.is_active == True
        ).order_by(AvailabilityBlock.start_time)
        
        blocks = session.exec(statement).all()
        
        logger.info(f"📅 Loading availability for user {target_user_id} on {date_str}: found {len(blocks)} blocks")
        
        return JSONResponse({
            "success": True,
            "date": date_str,
            "blocks": [
                {
                    "id": block.id,
                    "start_time": format_time_field(block.start_time),
                    "end_time": format_time_field(block.end_time),
                    "status": block.status
                }
                for block in blocks
            ]
        })

@router.post("/api/availability/save")
async def save_availability(
    request: Request,
    date: str = Form(...),
    blocks: str = Form(...)  # JSON string con array di blocchi
):
    """Salva disponibilità per una data specifica"""
    user = verify_token(request)
    if not user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    try:
        import json
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
        blocks_data = json.loads(blocks)
        
        # ✅ Validazione: le fasce per oggi devono rispettare il preavviso minimo
        now_italy = datetime.now(ITALY_TZ)
        today_italy = now_italy.date()
        
        if target_date == today_italy and not DEBUG_MODE:
            min_start = now_italy + timedelta(hours=ORE_PREAVVISO_PRENOTAZIONE)
            min_start_minutes = min_start.hour * 60 + min_start.minute
            
            for block_data in blocks_data:
                start_time_str = block_data.get("start_time", "")
                try:
                    parts = list(map(int, start_time_str.split(":")))
                    block_start_minutes = parts[0] * 60 + parts[1]
                except (ValueError, IndexError):
                    continue
                
                if block_start_minutes < min_start_minutes:
                    min_time_formatted = f"{min_start.hour:02d}:{min_start.minute:02d}"
                    return JSONResponse(
                        status_code=400,
                        content={
                            "success": False,
                            "message": f"Per oggi le fasce orarie devono iniziare dopo le {min_time_formatted} (almeno {ORE_PREAVVISO_PRENOTAZIONE} ore da adesso)"
                        }
                    )
        elif target_date < today_italy and not DEBUG_MODE:
            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "message": "Non puoi impostare disponibilità per date passate"
                }
            )
        
        with Session(engine) as session:
            fasce_nuove = []
            for block_data in blocks_data:
                start_time_str = block_data.get("start_time")
                end_time_str = block_data.get("end_time")
                try:
                    datetime.strptime(start_time_str, "%H:%M")
                    datetime.strptime(end_time_str, "%H:%M")
                except (TypeError, ValueError):
                    continue
                if _minuti(end_time_str) - _minuti(start_time_str) <= 0:
                    continue
                fasce_nuove.append((start_time_str, end_time_str))

            if prenotazioni_non_coperte(session, user.id, target_date.strftime("%Y-%m-%d"), fasce_nuove):
                return JSONResponse(
                    status_code=409,
                    content={
                        "success": False,
                        "message": "Ci sono prenotazioni negli orari che stai togliendo. "
                                   "Puoi cambiare la disponibilità quando quelle consulenze sono annullate.",
                    },
                )

            # Rimuovi blocchi esistenti per quella data
            existing = session.exec(
                select(AvailabilityBlock).where(
                    and_(
                        AvailabilityBlock.user_id == user.id,
                        AvailabilityBlock.date >= datetime.combine(target_date, datetime.min.time()),
                        AvailabilityBlock.date < datetime.combine(target_date, datetime.max.time())
                    )
                )
            ).all()
            
            for block in existing:
                session.delete(block)
            
            # Crea nuovi blocchi
            for block_data in blocks_data:
                start_time_str = block_data["start_time"]
                end_time_str = block_data["end_time"]
                
                # Converti stringhe a oggetti time
                try:
                    start_time_obj = datetime.strptime(start_time_str, "%H:%M").time()
                    end_time_obj = datetime.strptime(end_time_str, "%H:%M").time()
                except (ValueError, KeyError):
                    logger.warning(f"⚠️ Invalid time format: {start_time_str} or {end_time_str}")
                    continue
                
                # Calcola durata in minuti
                start_parts = list(map(int, start_time_str.split(":")))
                end_parts = list(map(int, end_time_str.split(":")))
                start_minutes = start_parts[0] * 60 + start_parts[1]
                end_minutes = end_parts[0] * 60 + end_parts[1]
                total_minutes = end_minutes - start_minutes
                
                if total_minutes <= 0:
                    continue
                
                new_block = AvailabilityBlock(
                    user_id=user.id,
                    date=datetime.combine(target_date, datetime.min.time()),
                    start_time=start_time_obj,
                    end_time=end_time_obj,
                    total_minutes=total_minutes,
                    status="available"
                )
                session.add(new_block)
                logger.info(f"💾 Saving block: date={target_date}, time={start_time_str}-{end_time_str}")
            
            session.commit()
            logger.info(f"✅ Saved {len(blocks_data)} availability blocks for user {user.id} on {date}")
            
            return JSONResponse({
                "success": True,
                "message": f"Disponibilità salvata per {date}"
            })
    
    except Exception as e:
        logger.error(f"❌ Error saving availability: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/availability/copy")
async def copy_availability(
    request: Request,
    source_date: str = Form(...),
    target_dates: str = Form(...)  # JSON array di date target
):
    """Copia disponibilità da una data sorgente a più date target"""
    user = verify_token(request)
    if not user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    try:
        import json
        target_dates_list = json.loads(target_dates)
        
        with Session(engine) as session:
            # Ottieni blocchi dalla data sorgente
            source_blocks = session.exec(
                select(AvailabilityBlock).where(
                    AvailabilityBlock.user_id == user.id,
                    func.date(AvailabilityBlock.date) == source_date,
                    AvailabilityBlock.is_active == True
                )
            ).all()
            
            if not source_blocks:
                return JSONResponse({
                    "success": False,
                    "message": "Nessuna disponibilità trovata nella data sorgente"
                })
            
            copied_count = 0
            skipped_count = 0
            giorni_con_prenotazioni = 0
            
            for target_date_str in target_dates_list:
                target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
                
                # Calcola orario minimo se la data target è oggi
                now_italy = datetime.now(ITALY_TZ)
                today_italy = now_italy.date()
                min_start_minutes = None
                if target_date == today_italy and not DEBUG_MODE:
                    min_start = now_italy + timedelta(hours=ORE_PREAVVISO_PRENOTAZIONE)
                    min_start_minutes = min_start.hour * 60 + min_start.minute
                elif target_date < today_italy and not DEBUG_MODE:
                    continue  # Salta date passate

                fasce_copia = []
                for source_block in source_blocks:
                    if min_start_minutes is not None:
                        src_start = format_time_field(source_block.start_time)
                        if src_start and _minuti(src_start) < min_start_minutes:
                            continue
                    fasce_copia.append((
                        format_time_field(source_block.start_time),
                        format_time_field(source_block.end_time),
                    ))
                if prenotazioni_non_coperte(session, user.id, target_date_str, fasce_copia):
                    giorni_con_prenotazioni += 1
                    continue

                # Rimuovi blocchi esistenti
                existing = session.exec(
                    select(AvailabilityBlock).where(
                        AvailabilityBlock.user_id == user.id,
                        func.date(AvailabilityBlock.date) == target_date_str
                    )
                ).all()
                
                for block in existing:
                    session.delete(block)
                
                # Copia blocchi
                for source_block in source_blocks:
                    # Se è oggi, verifica il preavviso minimo
                    if min_start_minutes is not None:
                        src_start = format_time_field(source_block.start_time)
                        if src_start:
                            parts = list(map(int, src_start.split(":")))
                            block_start_minutes = parts[0] * 60 + parts[1]
                            if block_start_minutes < min_start_minutes:
                                skipped_count += 1
                                continue
                    
                    new_block = AvailabilityBlock(
                        user_id=user.id,
                        date=target_date,
                        start_time=source_block.start_time,
                        end_time=source_block.end_time,
                        total_minutes=source_block.total_minutes,
                        status="available"
                    )
                    session.add(new_block)
                    copied_count += 1
            
            session.commit()
            logger.info(f"✅ Copied availability from {source_date} to {len(target_dates_list)} dates (skipped {skipped_count} blocks within 4h)")
            
            msg = f"Disponibilità copiata in {len(target_dates_list)} giorni ({copied_count} blocchi totali)"
            if skipped_count > 0:
                msg += f". {skipped_count} blocco/i esclusi perché troppo vicini all'orario attuale (min. {ORE_PREAVVISO_PRENOTAZIONE} ore)"
            if giorni_con_prenotazioni:
                msg += (
                    f". {giorni_con_prenotazioni} giorno/i non modificati: "
                    "hanno prenotazioni che la copia avrebbe scoperto"
                )
            
            return JSONResponse({
                "success": True,
                "message": msg
            })
    
    except Exception as e:
        logger.error(f"❌ Error copying availability: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/availability/block/{block_id}")
async def delete_availability_block(request: Request, block_id: int):
    """Elimina un blocco di disponibilità"""
    user = verify_token(request)
    if not user:
        raise HTTPException(status_code=401, detail="Non autorizzato")
    
    with Session(engine) as session:
        block = session.get(AvailabilityBlock, block_id)
        
        if not block or block.user_id != user.id:
            raise HTTPException(status_code=404, detail="Blocco non trovato")

        giorno = block.date.strftime("%Y-%m-%d") if hasattr(block.date, "strftime") else str(block.date)[:10]
        altri = session.exec(
            select(AvailabilityBlock).where(
                AvailabilityBlock.user_id == user.id,
                AvailabilityBlock.id != block.id,
                func.date(AvailabilityBlock.date) == giorno,
            )
        ).all()
        fasce_rimaste = [
            (format_time_field(altro.start_time), format_time_field(altro.end_time))
            for altro in altri
        ]
        if prenotazioni_non_coperte(session, user.id, giorno, fasce_rimaste):
            raise HTTPException(
                status_code=409,
                detail="Questo orario ha una prenotazione. Non si può togliere finché la consulenza è in corso.",
            )

        session.delete(block)
        session.commit()
        
        return JSONResponse({
            "success": True,
            "message": "Blocco eliminato"
        })
