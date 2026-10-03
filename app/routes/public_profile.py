from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from app.models import User, Category, CategoryHierarchy, FavoriteConsultant, Review
from app.database import get_session
from app.logger_config import logger
from app.utils.template_helpers import get_all_categories
from app.utils_user import has_payment_method
from app.utils.verifica_categorie import etichette_verificate, id_categorie_verificate
from sqlmodel import select
import json

router = APIRouter()

@router.get("/user/{user_id}", response_class=HTMLResponse)
def public_user_profile(request: Request, user_id: int):
    """Visualizza il profilo pubblico di un utente"""
    with get_session() as session:
        user = session.get(User, user_id)
        if not user:
            return RedirectResponse("/")
        
        # Carica la categoria
        category = None
        if user.category_id:
            category = session.get(Category, user.category_id)
        
        # Carica le sottocategorie selezionate dall'utente
        subcategories = []
        if user.selected_subcategories:
            try:
                sub_ids = json.loads(user.selected_subcategories)
                sub_ids_int = [int(sid) for sid in sub_ids]
                for sid in sub_ids_int:
                    sub_cat = session.get(Category, sid)
                    if sub_cat:
                        subcategories.append(sub_cat)
            except (json.JSONDecodeError, ValueError):
                pass
        
        # Converti aree_interesse da stringa a lista
        aree_interesse_list = user.aree_interesse.split(',') if user.aree_interesse else []
        
        # Ottieni l'utente loggato (se c'è) - USA verify_token con Request
        current_user = None
        try:
            from app.routes.auth import verify_token
            current_user = verify_token(request)
        except:
            pass
        
        # Carica tutte le categorie principali per il dropdown nel navbar
        categories = get_all_categories()
        
        logger.info(f"Public profile viewed: {user.email} (ID: {user.id}) by {current_user.email if current_user else 'anonymous'}")
        
        # Check if current user has favorited this consultant
        is_favorited = False
        if current_user and current_user.id != user.id:
            fav = session.exec(
                select(FavoriteConsultant).where(
                    FavoriteConsultant.user_id == current_user.id,
                    FavoriteConsultant.consultant_id == user.id
                )
            ).first()
            is_favorited = fav is not None
        
        # Parse languages
        user_languages = []
        user_other_language = ''
        if user.languages:
            try:
                lang_data = json.loads(user.languages)
                user_languages = lang_data.get('codes', [])
                user_other_language = lang_data.get('other', '')
            except (json.JSONDecodeError, AttributeError):
                pass
        
        # Compute last seen label
        last_seen_label = None
        if user.last_seen:
            from datetime import datetime
            now = datetime.utcnow()
            diff = now - user.last_seen
            minutes = int(diff.total_seconds() / 60)
            if minutes < 5:
                last_seen_label = "Online ora"
            elif minutes < 60:
                last_seen_label = f"Attivo {minutes} min fa"
            elif minutes < 1440:
                hours = minutes // 60
                last_seen_label = f"Attivo {hours} or{'a' if hours == 1 else 'e'} fa"
            elif minutes < 43200:
                days = minutes // 1440
                last_seen_label = f"Attivo {days} giorn{'o' if days == 1 else 'i'} fa"
            else:
                months = minutes // 43200
                last_seen_label = f"Attivo {months} mes{'e' if months == 1 else 'i'} fa"
        
        # Carica recensioni del consulente
        reviews_raw = session.exec(
            select(Review).where(Review.consultant_user_id == user_id).order_by(Review.created_at.desc())
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
        
        return request.app.state.templates.TemplateResponse("user_profile.html", {
            "request": request,
            "user": user,
            "current_user": current_user,
            "category": category,
            "aree_interesse_list": aree_interesse_list,
            "subcategories": subcategories,
            "is_own_profile": current_user and current_user.id == user.id,
            "is_favorited": is_favorited,
            "categories": categories,
            "user_languages": user_languages,
            "user_other_language": user_other_language,
            "last_seen_label": last_seen_label,
            "reviews": reviews,
            "avg_rating": avg_rating,
            "consultant_has_payment": has_payment_method(user),
            "categorie_verificate": etichette_verificate(session, user),
            "categorie_verificate_ids": list(id_categorie_verificate(user)),
            "current_user_has_payment": has_payment_method(current_user) if current_user else False,
        })


@router.post("/api/favorites/toggle/{consultant_id}")
def toggle_favorite(request: Request, consultant_id: int):
    """Aggiunge o rimuove un consulente dai preferiti"""
    from app.routes.auth import verify_token
    current_user = verify_token(request)
    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)
    
    if current_user.id == consultant_id:
        return JSONResponse({"error": "Non puoi salvare te stesso"}, status_code=400)
    
    with get_session() as session:
        consultant = session.get(User, consultant_id)
        if not consultant:
            return JSONResponse({"error": "Utente non trovato"}, status_code=404)
        
        existing = session.exec(
            select(FavoriteConsultant).where(
                FavoriteConsultant.user_id == current_user.id,
                FavoriteConsultant.consultant_id == consultant_id
            )
        ).first()
        
        if existing:
            session.delete(existing)
            session.commit()
            return JSONResponse({"status": "removed", "message": "Rimosso dai preferiti"})
        else:
            fav = FavoriteConsultant(user_id=current_user.id, consultant_id=consultant_id)
            session.add(fav)
            session.commit()
            return JSONResponse({"status": "added", "message": "Aggiunto ai preferiti"})


@router.get("/api/favorites")
def get_favorites(request: Request):
    """Restituisce la lista dei consulenti preferiti dell'utente loggato"""
    from app.routes.auth import verify_token
    current_user = verify_token(request)
    if not current_user:
        return JSONResponse({"error": "Non autenticato"}, status_code=401)
    
    with get_session() as session:
        favorites = session.exec(
            select(FavoriteConsultant).where(
                FavoriteConsultant.user_id == current_user.id
            ).order_by(FavoriteConsultant.created_at.desc())
        ).all()
        
        consultants = []
        for fav in favorites:
            consultant = session.get(User, fav.consultant_id)
            if consultant:
                category = session.get(Category, consultant.category_id) if consultant.category_id else None
                consultants.append({
                    "id": consultant.id,
                    "nome": consultant.nome or "",
                    "cognome": consultant.cognome or "",
                    "professione": consultant.professione or "",
                    "profile_picture": consultant.profile_picture,
                    "prezzo_consulenza": consultant.prezzo_consulenza,
                    "category_name": category.name if category else None,
                    "category_icon": category.icon if category else None,
                    "is_verified": consultant.is_verified
                })
        
        return JSONResponse({"favorites": consultants})