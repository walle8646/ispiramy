from fastapi import APIRouter, Request, Form, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from app.database import get_session
from app.models import User, Category, CategoryHierarchy, Review
from sqlmodel import select, and_, func
from app.routes.auth import verify_token
from app.logger_config import logger
from app.utils.email import send_profile_verification_request
from app.utils.prezzi import PREZZO_ORARIO_MINIMO
from app.utils_user import has_payment_method
from app.utils.ai_service import genera_aree_interesse, genera_tags, valida_profilo, modera_immagine
from typing import Optional
import asyncio
import os
import hashlib
from datetime import datetime
from PIL import Image
import io
import json

router = APIRouter()

@router.get("/profile", response_class=HTMLResponse)
async def user_profile(request: Request):
    """Pagina profilo utente"""
    try:
        user = verify_token(request)
        
        if not user:
            logger.warning("❌ Unauthorized access to profile")
            return RedirectResponse("/login", status_code=307)
        
        with get_session() as session:
            fresh_user = session.get(User, user.id)
            
            if not fresh_user:
                logger.error(f"❌ User ID {user.id} not found in database")
                request.session.clear()
                return RedirectResponse("/login", status_code=307)
            
            categories = session.exec(select(Category).where(Category.is_principal == True).order_by(Category.id)).all()
            logger.info(f"✅ Loaded {len(categories)} principal categories")
            
            # Formatta la data created_at prima di passarla
            formatted_created_at = None
            if fresh_user.created_at:
                formatted_created_at = fresh_user.created_at.strftime("%d/%m/%Y")
            
            # Processiamo le aree di interesse
            aree_interesse_list = fresh_user.aree_interesse.split(',') if fresh_user.aree_interesse else []
            logger.info(f"🔍 DEBUG AREE - raw value: '{fresh_user.aree_interesse}'")
            logger.info(f"🔍 DEBUG AREE - is None: {fresh_user.aree_interesse is None}")
            logger.info(f"🔍 DEBUG AREE - is empty: {fresh_user.aree_interesse == ''}")
            logger.info(f"🔍 DEBUG AREE - list result: {aree_interesse_list}")
            logger.info(f"🔍 DEBUG AREE - list length: {len(aree_interesse_list)}")
            
            # Recupera la categoria dell'utente se presente
            user_category = None
            if fresh_user.category_id:
                user_category = session.get(Category, fresh_user.category_id)
            
            logger.info(f"✅ Profile loaded for user: {fresh_user.email}")
            
            # Parse languages
            user_languages = []
            user_other_language = ''
            if fresh_user.languages:
                try:
                    lang_data = json.loads(fresh_user.languages)
                    user_languages = lang_data.get('codes', [])
                    user_other_language = lang_data.get('other', '')
                except (json.JSONDecodeError, AttributeError):
                    pass
            
            # Recensioni ricevute
            reviews_raw = session.exec(
                select(Review).where(Review.consultant_user_id == fresh_user.id).order_by(Review.created_at.desc())
            ).all()
            reviews = []
            for r in reviews_raw:
                reviewer = session.get(User, r.reviewer_user_id)
                reviews.append({
                    "rating_helpful": r.rating_helpful,
                    "rating_prepared": r.rating_prepared,
                    "rating_communication": r.rating_communication,
                    "comment": r.comment,
                    "created_at": r.created_at,
                    "reviewer": reviewer,
                })
            avg_rating = None
            if reviews:
                total = sum(
                    (r["rating_helpful"] + r["rating_prepared"] + r["rating_communication"]) / 3
                    for r in reviews
                )
                avg_rating = round(total / len(reviews), 1)
            
            return request.app.state.templates.TemplateResponse(
                "profile.html",
                {
                    "request": request,
                    "user": fresh_user,
                    "current_user": fresh_user,
                    "categories": categories,
                    "aree_interesse_list": aree_interesse_list,
                    "user_category": user_category,
                    "user_languages": user_languages,
                    "user_other_language": user_other_language,
                    "reviews": reviews,
                    "avg_rating": avg_rating,
                    "formatted_created_at": formatted_created_at,
                    "has_payment_method": has_payment_method(fresh_user)
                }
            )
    
    except Exception:
        # Un errore nel costruire la pagina NON deve chiudere la sessione:
        # prima qui si faceva request.session.clear(), e siccome Stripe
        # rimanda proprio su /profile dopo il pagamento, qualunque intoppo
        # (una query fallita, un dato mancante) sloggava l'utente appena
        # prenotato. Si registra l'errore e si mostra una pagina d'errore.
        logger.exception("Error in profile")
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>Ispiramy</title>"
            "<div style=\"font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;text-align:center;color:#1b3a24\">"
            "<h1 style=\"font-size:1.4rem\">Non siamo riusciti a caricare il tuo profilo</h1>"
            "<p>Sei ancora connesso. Riprova tra qualche secondo.</p>"
            "<p><a href=\"/profile\" style=\"color:#2e7d32;font-weight:600\">Ricarica il profilo</a></p></div>",
            status_code=500,
        )

@router.get("/api/profile/liked-questions")
async def get_liked_questions(request: Request):
    """Recupera le ultime 6 domande a cui l'utente ha messo like"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        with get_session() as session:
            from app.models import CommunityQuestion, CommunityLike, User as UserModel, Category
            
            # Query per recuperare le ultime 6 domande amate ordinate per data decrescente
            liked_questions = session.exec(
                select(CommunityQuestion)
                .join(CommunityLike, CommunityQuestion.id == CommunityLike.question_id)
                .where(CommunityLike.user_id == user.id)
                .order_by(CommunityLike.created_at.desc())
                .limit(6)
            ).all()
            
            questions_data = []
            for q in liked_questions:
                # Recupera l'autore della domanda
                author = session.get(UserModel, q.user_id)
                cat = session.get(Category, q.category_id) if q.category_id else None
                
                questions_data.append({
                    "id": q.id,
                    "title": q.title,
                    "description": q.description[:150] + "..." if len(q.description) > 150 else q.description,  # Preview
                    "author_name": author.nome if author else "Utente Anonimo",
                    "author_id": q.user_id,
                    "category_id": q.category_id,
                    "category_name": cat.name if cat else "Generale",
                    "category_icon": cat.icon if cat else "💬",
                    "upvotes": q.upvotes,
                    "views": q.views,
                    "created_at": q.created_at.strftime("%d/%m/%Y"),
                    "url": f"/community/question/{q.id}"
                })
            
            return JSONResponse({
                "success": True,
                "questions": questions_data
            })
    
    except Exception as e:
        logger.exception("Error getting liked questions")
        return JSONResponse(
            {"error": "Errore nel recupero delle domande"},
            status_code=500
        )

@router.get("/api/profile/user-questions")
async def get_user_questions(request: Request):
    """Recupera le ultime 4 domande scritte dall'utente"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        with get_session() as session:
            from app.models import CommunityQuestion, User as UserModel, Category
            
            # Query per recuperare le domande scritte dall'utente
            user_questions = session.exec(
                select(CommunityQuestion)
                .where(CommunityQuestion.user_id == user.id)
                .order_by(CommunityQuestion.created_at.desc())
            ).all()
            
            questions_data = []
            for q in user_questions:
                # Recupera l'autore della domanda (dovrebbe essere l'utente stesso)
                author = session.get(UserModel, q.user_id)
                cat = session.get(Category, q.category_id) if q.category_id else None
                
                # Check editabile: <48h e nessuna interazione
                from app.models import CommunityLike, CommunityContact, CommunityQuestionFollow
                from sqlmodel import func
                likes_count = session.exec(select(func.count()).select_from(CommunityLike).where(CommunityLike.question_id == q.id)).one()
                contacts_count = session.exec(select(func.count()).select_from(CommunityContact).where(CommunityContact.question_id == q.id)).one()
                follows_count = session.exec(select(func.count()).select_from(CommunityQuestionFollow).where(CommunityQuestionFollow.question_id == q.id)).one()
                total_interactions = likes_count + contacts_count + follows_count
                age_hours = (datetime.utcnow() - q.created_at).total_seconds() / 3600
                can_edit = age_hours < 48 and total_interactions == 0
                
                questions_data.append({
                    "id": q.id,
                    "title": q.title,
                    "description": q.description[:150] + "..." if len(q.description) > 150 else q.description,
                    "author_name": author.nome if author else "Utente Anonimo",
                    "author_id": q.user_id,
                    "category_id": q.category_id,
                    "category_name": cat.name if cat else "Generale",
                    "category_icon": cat.icon if cat else "💬",
                    "upvotes": q.upvotes,
                    "views": q.views,
                    "created_at": q.created_at.strftime("%d/%m/%Y"),
                    "can_edit": can_edit,
                    "url": f"/community/question/{q.id}"
                })
            
            return JSONResponse({
                "success": True,
                "questions": questions_data
            })
    
    except Exception as e:
        logger.exception("Error getting user questions")
        return JSONResponse(
            {"error": "Errore nel recupero delle domande"},
            status_code=500
        )

@router.get("/api/profile/subcategories/{category_id}")
async def get_subcategories(category_id: int):
    """Get subcategories for a principal category"""
    try:
        with get_session() as session:
            hierarchy_entries = session.exec(
                select(CategoryHierarchy)
                .where(CategoryHierarchy.parent_category_id == category_id)
                .order_by(CategoryHierarchy.position)
            ).all()
            
            subcategories = []
            for entry in hierarchy_entries:
                cat = session.get(Category, entry.child_category_id)
                if cat:
                    subcategories.append({
                        "id": cat.id,
                        "name": cat.name,
                        "icon": cat.icon
                    })
            
            return {"subcategories": subcategories}
    except Exception as e:
        logger.exception("Error getting subcategories")
        return JSONResponse(
            {"error": "Errore nel recupero delle sottocategorie"},
            status_code=500
        )

@router.post("/api/profile/update")
async def update_profile(
    request: Request,
    nome: str = Form(None),
    cognome: str = Form(None),  # ✅ AGGIUNGI cognome
    professione: str = Form(None),
    descrizione: str = Form(None),
    category_id: Optional[int] = Form(None),
    aree_interesse: str = Form(None),
    prezzo_consulenza: Optional[int] = Form(None),
    is_anonymous: Optional[bool] = Form(None),  # ✅ NUOVO: flag anonimato
    notify_category_requests: Optional[bool] = Form(None),  # ✅ NUOVO: notifiche categoria
    selected_subcategories: str = Form(None),  # ✅ NUOVO: JSON array di subcategory IDs
    genere: Optional[str] = Form(None),  # ✅ Genere: M/F/None
    tags: str = Form(None),  # 🏷️ JSON array di tags generati da AI
    languages: str = Form(None),  # 🌐 JSON array di language codes
    other_language: str = Form(None),  # 🌍 Altra lingua specificata
    paypal_email: Optional[str] = Form(None),  # 💰 PayPal email per ricevere pagamenti
    simple_mode: Optional[str] = Form(None)  # 🔄 Modalità semplice (no AI, no verifica)
):
    """Aggiorna profilo utente"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        with get_session() as session:
            db_user = session.get(User, user.id)
            
            if not db_user:
                return JSONResponse({"error": "Utente non trovato"}, status_code=404)
            
            # Check if user was already verified before update
            was_verified_before = db_user.is_verified
            descrizione_originale = (db_user.descrizione or "").strip()

            if nome is not None:
                db_user.nome = nome
            if cognome is not None:  # ✅ AGGIUNGI questo
                db_user.cognome = cognome
            if professione is not None:
                db_user.professione = professione
            if descrizione is not None:
                db_user.descrizione = descrizione
            if category_id is not None:
                db_user.category_id = category_id
            if aree_interesse is not None:
                db_user.aree_interesse = aree_interesse
                logger.info(f"✅ Aree di interesse aggiornate per user {db_user.id}: '{aree_interesse}'")
            if prezzo_consulenza is not None:
                if prezzo_consulenza < PREZZO_ORARIO_MINIMO:
                    return JSONResponse(
                        {"error": f"Il prezzo della consulenza deve essere almeno {PREZZO_ORARIO_MINIMO}€/ora"},
                        status_code=400
                    )
                db_user.prezzo_consulenza = prezzo_consulenza
            if is_anonymous is not None:  # ✅ NUOVO: aggiorna flag anonimato
                # Converti la stringa "true"/"false" a booleano
                if isinstance(is_anonymous, str):
                    is_anonymous = is_anonymous.lower() == 'true'
                db_user.is_anonymous = is_anonymous
                logger.info(f"{'🔒' if is_anonymous else '👤'} User {db_user.id} set anonymous mode: {is_anonymous}")
            if notify_category_requests is not None:  # ✅ NUOVO: aggiorna flag notifiche categoria
                # Converti la stringa "true"/"false" a booleano
                if isinstance(notify_category_requests, str):
                    notify_category_requests = notify_category_requests.lower() == 'true'
                db_user.notify_category_requests = notify_category_requests
                logger.info(f"{'🔔' if notify_category_requests else '🔕'} User {db_user.id} set category notifications: {notify_category_requests}")
            if selected_subcategories is not None:  # ✅ NUOVO: salva JSON array
                db_user.selected_subcategories = selected_subcategories
                logger.info(f"✅ Subcategories updated for user: {db_user.id} - {selected_subcategories}")
            if genere is not None:
                # Accetta solo 'M', 'F' o stringa vuota (→ None)
                db_user.genere = genere if genere in ('M', 'F') else None
                logger.info(f"✅ Genere updated for user {db_user.id}: {db_user.genere}")
            if tags is not None:
                db_user.tags = tags
                logger.info(f"🏷️ Tags updated for user {db_user.id}: {tags}")
            if languages is not None:
                db_user.languages = languages
                logger.info(f"🌐 Languages updated for user {db_user.id}: {languages}")
            if paypal_email is not None:
                import re
                paypal_email_clean = paypal_email.strip()
                if paypal_email_clean == "":
                    db_user.paypal_email = None
                    logger.info(f"💰 PayPal email rimossa per user {db_user.id}")
                elif re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', paypal_email_clean):
                    db_user.paypal_email = paypal_email_clean
                    logger.info(f"💰 PayPal email aggiornata per user {db_user.id}: {paypal_email_clean}")
                else:
                    return JSONResponse({"error": "Email PayPal non valida"}, status_code=400)
            
            is_simple = simple_mode and simple_mode.lower() == 'true'

            if is_simple:
                # Modalità semplice: salva solo i campi base, niente AI e niente verifica
                session.add(db_user)
                session.commit()
                session.refresh(db_user)
                logger.info(f"🔄 Simple mode save for user {db_user.email}, skipping AI validation")
                return JSONResponse({
                    "message": "Profilo aggiornato con successo!",
                    "user": {
                        "nome": db_user.nome,
                        "cognome": db_user.cognome,
                        "professione": db_user.professione
                    },
                    "is_verified": db_user.is_verified
                })

            # Check if profile meets verification criteria (valori in memoria, NON ancora salvati)
            has_professione = db_user.professione and db_user.professione.strip() != ""
            has_category = db_user.category_id is not None
            has_aree_interesse = db_user.aree_interesse and db_user.aree_interesse.strip() != ""
            has_descrizione = db_user.descrizione and len(db_user.descrizione.strip()) >= 200

            profile_complete = has_professione and has_category and has_aree_interesse and has_descrizione
            logger.info(f"🔍 Profile complete: {profile_complete} for user {db_user.email}")

            # ========== VALIDAZIONE AI DEL PROFILO (BLOCCANTE) ==========
            # Validiamo PRIMA di salvare. Per non bloccare chi è già verificato e modifica
            # altri campi, ri-validiamo solo se la descrizione è cambiata o non era ancora
            # verificato. Se l'AI giudica la descrizione non genuina/incoerente, blocchiamo
            # il salvataggio (nessun commit = rollback) e chiediamo di riscriverla.
            descrizione_cambiata = (db_user.descrizione or "").strip() != descrizione_originale

            if profile_complete and (descrizione_cambiata or not was_verified_before):
                try:
                    validazione = await valida_profilo(
                        descrizione=db_user.descrizione,
                        professione=db_user.professione,
                        aree_interesse=db_user.aree_interesse
                    )
                    ai_verified = validazione.get("approved", False)
                    ai_reason = validazione.get("reason", "")
                except Exception:
                    logger.exception("❌ Errore validazione AI profilo")
                    # Errore TECNICO dell'AI (rete/API): non blocchiamo, salviamo non verificato
                    db_user.is_verified = False
                    session.add(db_user)
                    session.commit()
                    session.refresh(db_user)
                    return JSONResponse({
                        "message": "Profilo salvato, ma la verifica automatica non è disponibile in questo momento.",
                        "user": {
                            "nome": db_user.nome,
                            "cognome": db_user.cognome,
                            "professione": db_user.professione
                        },
                        "is_verified": False,
                        "verification_warning": "Verifica automatica non disponibile, riprova più tardi."
                    })

                if not ai_verified:
                    # 🚫 BLOCCO: profilo non genuino → non salviamo nulla (return senza commit)
                    logger.warning(f"🚫 Salvataggio bloccato per {db_user.email}: {ai_reason}")
                    return JSONResponse({
                        "error": ai_reason or "La descrizione non sembra autentica o coerente con la professione indicata. Riscrivila in modo genuino per completare la verifica.",
                        "validation_failed": True
                    }, status_code=400)

                # ✅ Profilo approvato dall'AI
                db_user.is_verified = True
                logger.info(f"✅ Profilo VERIFICATO automaticamente per {db_user.email}: {ai_reason}")
            elif profile_complete:
                # Profilo completo e già verificato, descrizione invariata → mantieni la verifica
                db_user.is_verified = True
            else:
                # Profilo incompleto → salvato ma non verificato
                db_user.is_verified = False

            session.add(db_user)
            session.commit()
            session.refresh(db_user)

            logger.info(f"✅ Profile updated for user: {db_user.email}")

            return JSONResponse({
                "message": "Profilo aggiornato con successo!",
                "user": {
                    "nome": db_user.nome,
                    "cognome": db_user.cognome,
                    "professione": db_user.professione
                },
                "is_verified": db_user.is_verified
            })
    
    except Exception as e:
        logger.exception("Error updating profile")
        return JSONResponse(
            {"error": "Errore durante l'aggiornamento"},
            status_code=500
        )


@router.post("/api/profile/generate-interests")
async def generate_interests(request: Request):
    """Genera automaticamente le aree di interesse dalla descrizione usando AI"""
    try:
        user = verify_token(request)
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        body = await request.json()
        descrizione = body.get("descrizione", "").strip()
        professione = body.get("professione", "").strip()
        
        if not descrizione or len(descrizione) < 50:
            return JSONResponse(
                {"error": "La descrizione deve essere di almeno 50 caratteri per generare le aree di interesse."},
                status_code=400
            )
        
        aree = await genera_aree_interesse(descrizione, professione or None)
        
        return JSONResponse({
            "success": True,
            "aree_interesse": ", ".join(aree),
            "aree_list": aree
        })
    
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("❌ Errore generazione aree di interesse")
        return JSONResponse(
            {"error": "Errore durante la generazione delle aree di interesse. Riprova."},
            status_code=500
        )


@router.post("/api/profile/generate-tags")
async def generate_tags(request: Request):
    """Genera tag di ricerca dalla descrizione e aree di interesse usando AI"""
    try:
        user = verify_token(request)
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        body = await request.json()
        descrizione = body.get("descrizione", "").strip()
        aree_interesse = body.get("aree_interesse", "").strip()
        professione = body.get("professione", "").strip()
        
        if not descrizione or len(descrizione) < 50:
            return JSONResponse(
                {"error": "La descrizione deve essere di almeno 50 caratteri per generare i tag."},
                status_code=400
            )
        
        tags = await genera_tags(descrizione, aree_interesse or None, professione or None)
        
        # Salva i tag nel database
        with get_session() as session:
            db_user = session.get(User, user.id)
            if db_user:
                db_user.tags = json.dumps(tags)
                session.add(db_user)
                session.commit()
                logger.info(f"🏷️ Tags salvati per user {user.id}: {tags}")
        
        return JSONResponse({
            "success": True,
            "tags": tags
        })
    
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("❌ Errore generazione tags")
        return JSONResponse(
            {"error": "Errore durante la generazione dei tag. Riprova."},
            status_code=500
        )


@router.post("/api/upload-profile-picture")
async def upload_profile_picture(request: Request, file: UploadFile = File(...)):
    """Upload immagine profilo su S3"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        # Verifica che sia un'immagine
        if file.content_type not in ["image/jpeg", "image/png", "image/webp", "image/gif"]:
            return JSONResponse({"error": "Formato file non supportato"}, status_code=400)

        # Il limite va applicato PRIMA di tenere tutto in memoria: con
        # `await file.read()` secco, un upload da qualche GB veniva caricato per
        # intero solo per poi essere rifiutato.
        MAX_UPLOAD = 5 * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_UPLOAD + 8192:
            return JSONResponse({"error": "File troppo grande (max 5MB)"}, status_code=413)

        contents = await file.read(MAX_UPLOAD + 1)
        if len(contents) > MAX_UPLOAD:
            return JSONResponse({"error": "File troppo grande (max 5MB)"}, status_code=413)
        
        # ========== MODERAZIONE AI ==========
        import base64
        image_b64 = base64.b64encode(contents).decode("utf-8")
        moderation = await modera_immagine(image_b64, "Foto profilo", "Immagine del profilo utente sulla piattaforma Ispiramy")
        
        if not moderation.get("approved", False):
            reason = moderation.get("reason", "Immagine non approvata")
            logger.warning(f"🚫 Foto profilo rifiutata per user {user.id}: {reason}")
            return JSONResponse(
                {"error": f"Immagine rifiutata: {reason}"},
                status_code=400
            )
        
        # Importa boto3 per S3
        import boto3
        from datetime import datetime
        
        # Configura AWS S3
        aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        s3_bucket = os.getenv("S3_BUCKET_NAME", "ispiramy-images")
        s3_region = os.getenv("AWS_REGION", "eu-west-1")
        
        # Mai loggare porzioni della secret key: e' comunque materiale segreto.
        logger.info(
            f"🔍 Upload S3 - credenziali {'presenti' if (aws_access_key and aws_secret_key) else 'MANCANTI'}, "
            f"bucket={s3_bucket}, region={s3_region}"
        )
        
        if not aws_access_key or not aws_secret_key:
            # Errore: AWS deve essere configurato
            logger.error("❌ AWS credentials NOT configured - S3 is REQUIRED")
            return JSONResponse({
                "error": "Errore di configurazione server: AWS S3 non disponibile"
            }, status_code=500)
        
        try:
            logger.info(f"🔍 DEBUG - Creating S3 client...")
            s3_client = boto3.client(
                "s3",
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                region_name=s3_region
            )
            logger.info(f"✅ S3 client created successfully")
            
            # Genera nome unico per il file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_extension = file.filename.split(".")[-1].lower()
            s3_key = f"profile-pictures/{user.id}_{timestamp}.{file_extension}"
            
            logger.info(f"🔍 DEBUG - Uploading to S3: {s3_bucket}/{s3_key}")
            # Upload su S3, fuori dall'event loop: boto3 è sincrono e su una
            # rete lenta terrebbe fermo tutto il sito per l'intera durata.
            await asyncio.to_thread(
                s3_client.put_object,
                Bucket=s3_bucket,
                Key=s3_key,
                Body=contents,
                ContentType=file.content_type,
                CacheControl="max-age=31536000"  # Cache per 1 anno
            )
            logger.info(f"✅ File uploaded successfully to S3")

            # Genera URL pubblico (o signed URL se bucket è privato)
            try:
                # Prova a generare URL pubblico
                s3_url = f"https://{s3_bucket}.s3.{s3_region}.amazonaws.com/{s3_key}"

                # Verifica se il file è accessibile
                try:
                    await asyncio.to_thread(s3_client.head_object, Bucket=s3_bucket, Key=s3_key)
                    logger.info(f"✅ File uploaded to S3: {s3_url}")
                except Exception:
                    # Se non è accessibile, genera signed URL
                    s3_url = s3_client.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': s3_bucket, 'Key': s3_key},
                        ExpiresIn=31536000  # 1 anno in secondi
                    )
                    logger.info(f"✅ File uploaded to S3 (signed URL): {s3_url}")
            except Exception as e:
                logger.warning(f"⚠️ Could not generate URL: {e}")
                s3_url = f"https://{s3_bucket}.s3.{s3_region}.amazonaws.com/{s3_key}"
            
            # Salva URL nel database
            with get_session() as session:
                db_user = session.get(User, user.id)
                db_user.profile_picture = s3_url
                session.add(db_user)
                session.commit()
                logger.info(f"✅ Profile picture updated for user {user.id}")
            
            return JSONResponse({
                "success": True,
                "url": s3_url,
                "message": "Immagine caricata con successo!"
            })
        
        except Exception as s3_error:
            logger.exception("❌ S3 upload error")
            # Fallback a salvataggio locale
            return save_profile_picture_locally(user, contents)
    
    except Exception as e:
        logger.exception("❌ Error uploading profile picture")
        return JSONResponse(
            {"error": "Errore durante l'upload"},
            status_code=500
        )


def save_profile_picture_locally(user, file_contents):
    """Salva immagine profilo localmente come fallback"""
    try:
        from datetime import datetime
        from pathlib import Path
        
        upload_dir = Path("uploads/profile_pictures")
        upload_dir.mkdir(parents=True, exist_ok=True)
        
        # Genera nome unico
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{user.id}_{timestamp}.jpg"
        file_path = upload_dir / filename
        
        # Salva file
        with open(file_path, "wb") as f:
            f.write(file_contents)
        
        # URL relativo
        url = f"/uploads/profile_pictures/{filename}"
        
        # Aggiorna database
        with get_session() as session:
            db_user = session.get(User, user.id)
            db_user.profile_picture = url
            session.add(db_user)
            session.commit()
            logger.info(f"✅ Profile picture saved locally for user {user.id}: {url}")
        
        return JSONResponse({
            "success": True,
            "url": url,
            "message": "Immagine caricata con successo!"
        })
    
    except Exception as e:
        logger.exception("❌ Error saving profile picture locally")
        return JSONResponse(
            {"error": "Errore durante il salvataggio dell'immagine"},
            status_code=500
        )


@router.post("/api/user/set-anonymous")
async def set_anonymous_mode(request: Request):
    """Imposta la modalità anonima dell'utente"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        # Leggi il body della richiesta
        body = await request.json()
        is_anonymous = body.get("is_anonymous", False)
        
        with get_session() as session:
            db_user = session.get(User, user.id)
            
            if not db_user:
                return JSONResponse({"error": "Utente non trovato"}, status_code=404)
            
            # Aggiorna il flag anonimato
            db_user.is_anonymous = is_anonymous
            session.add(db_user)
            session.commit()
            session.refresh(db_user)
            
            logger.info(f"🔒 User {db_user.id} set anonymous mode: {is_anonymous}")
            
            return JSONResponse({
                "success": True,
                "is_anonymous": db_user.is_anonymous,
                "message": "Modalità anonima aggiornata con successo"
            })
    
    except Exception as e:
        logger.exception("❌ Error setting anonymous mode")
        return JSONResponse(
            {"error": "Errore durante l'aggiornamento della modalità anonima"},
            status_code=500
        )

@router.post("/api/user/set-auto-accept")
async def set_auto_accept_bookings(request: Request):
    """Conferma automatica delle prenotazioni (consulente).

    Con la conferma automatica spenta ogni prenotazione diretta diventa una
    richiesta da accettare o rifiutare. Si salva subito, senza passare dal
    form del profilo (che rivalida tutto il profilo con l'AI).
    """
    user = verify_token(request)
    if not user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)

    body = await request.json()
    valore = body.get("auto_accept_bookings")
    if not isinstance(valore, bool):
        return JSONResponse({"error": "Valore non valido"}, status_code=400)

    with get_session() as session:
        db_user = session.get(User, user.id)
        if not db_user:
            return JSONResponse({"error": "Utente non trovato"}, status_code=404)
        db_user.auto_accept_bookings = valore
        session.add(db_user)
        session.commit()

    logger.info(f"📩 User {user.id} conferma automatica prenotazioni: {valore}")
    return JSONResponse({"success": True, "auto_accept_bookings": valore})


@router.get("/api/category-requests")
async def get_category_requests(request: Request):
    """Ritorna le richieste della comunità della categoria dell'utente"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        with get_session() as session:
            db_user = session.get(User, user.id)
            
            if not db_user or not db_user.category_id:
                return JSONResponse({
                    "questions": [],
                    "message": "Nessuna categoria configurata"
                })
            
            # Importa il modello CommunityQuestion
            from app.models import CommunityQuestion
            from sqlmodel import or_
            
            # Recupera le domande della categoria o della sottocategoria
            # Cerca sia per primary_category_id che per category_id
            questions = session.exec(
                select(CommunityQuestion)
                .where(
                    or_(
                        CommunityQuestion.primary_category_id == db_user.category_id,
                        CommunityQuestion.category_id == db_user.category_id
                    )
                )
                .order_by(CommunityQuestion.created_at.desc())
                .limit(50)
            ).all()
            
            # Formatta le domande per il frontend
            formatted_questions = []
            for q in questions:
                author = session.get(User, q.user_id) if q.user_id else None
                formatted_questions.append({
                    "id": q.id,
                    "title": q.title,
                    "description": q.description,
                    "author_name": f"{author.nome} {author.cognome}" if author else "Anonimo",
                    "created_at": q.created_at.isoformat(),
                    "likes_count": q.upvotes if hasattr(q, 'upvotes') else 0
                })
            
            logger.info(f"✅ Loaded {len(formatted_questions)} category requests for user {db_user.id}")
            
            return JSONResponse({
                "questions": formatted_questions,
                "total": len(formatted_questions)
            })
    
    except Exception as e:
        logger.exception("❌ Error loading category requests")
        return JSONResponse(
            {"error": "Errore nel caricamento delle richieste", "questions": []},
            status_code=500
        )

@router.get("/api/category-requests/unread-count")
async def get_unread_category_requests_count(request: Request):
    """Ritorna il numero di richieste non lette della categoria dell'utente"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        with get_session() as session:
            db_user = session.get(User, user.id)
            
            if not db_user or not db_user.category_id:
                return JSONResponse({
                    "unread_count": 0
                })
            
            from app.models import CategoryRequestNotification
            
            # Conta le notifiche non lette per questo utente
            unread_count = session.exec(
                select(func.count(CategoryRequestNotification.id))
                .where(
                    and_(
                        CategoryRequestNotification.consultant_user_id == db_user.id,
                        CategoryRequestNotification.is_read == False
                    )
                )
            ).first() or 0
            
            logger.info(f"✅ Unread category requests for user {db_user.id}: {unread_count}")
            
            return JSONResponse({
                "unread_count": unread_count
            })
    
    except Exception as e:
        logger.error(f"❌ Error counting unread category requests: {e}", exc_info=True)
        return JSONResponse(
            {"error": "Errore nel conteggio", "unread_count": 0},
            status_code=500
        )

@router.post("/api/category-requests/mark-as-read")
async def mark_category_requests_as_read(request: Request):
    """Marca tutte le notifiche di richieste come lette"""
    try:
        user = verify_token(request)
        
        if not user:
            return JSONResponse({"error": "Non autenticato"}, status_code=401)
        
        with get_session() as session:
            from app.models import CategoryRequestNotification
            from sqlalchemy import update as sql_update
            
            # Aggiorna il flag is_read
            session.execute(
                sql_update(CategoryRequestNotification)
                .where(
                    and_(
                        CategoryRequestNotification.consultant_user_id == user.id,
                        CategoryRequestNotification.is_read == False
                    )
                )
                .values(is_read=True)
            )
            session.commit()
            
            logger.info(f"✅ Marked all category requests as read for user {user.id}")
            
            return JSONResponse({
                "success": True,
                "message": "Notifiche marcate come lette"
            })
    
    except Exception as e:
        logger.error(f"❌ Error marking as read: {e}", exc_info=True)
        return JSONResponse(
            {"error": "Errore nel marcamento", "success": False},
            status_code=500
        )
