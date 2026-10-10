from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from app.database import get_session
from app.models import User, Category, Review
from app.routes.auth import verify_token
from sqlmodel import select, func
from app.logger_config import logger

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Homepage con consulenti in evidenza"""
    
    # Controlla se utente è loggato
    current_user = verify_token(request)
    
    # Categorie disponibili tramite middleware
    categories = getattr(request.state, 'categories', [])
    
    with get_session() as session:
        try:
            # Carica consulenti featured
            featured_consultants_query = (
                select(User)
                .where(User.consulenze_vendute > 0)
                .order_by(User.consulenze_vendute.desc())
                .limit(4)
            )
            # Chi ha fatto accesso non si vede fra i consulenti in evidenza
            if current_user:
                featured_consultants_query = featured_consultants_query.where(User.id != current_user.id)
            featured_users = session.exec(featured_consultants_query).all()
            categorie = {c.id: c for c in session.exec(select(Category)).all()}
            voti = {}
            if featured_users:
                righe_voti = session.exec(
                    select(
                        Review.consultant_user_id,
                        func.count(Review.id),
                        func.avg(Review.rating_helpful),
                        func.avg(Review.rating_prepared),
                        func.avg(Review.rating_communication),
                    )
                    .where(Review.consultant_user_id.in_([u.id for u in featured_users]))
                    .group_by(Review.consultant_user_id)
                ).all()
                for riga in righe_voti:
                    voti[riga[0]] = (round((riga[2] + riga[3] + riga[4]) / 3, 1), riga[1])

            consultants = []
            for user in featured_users:
                media, quante = voti.get(user.id, (None, 0))
                consultants.append({
                    "user": user,
                    "category": categorie.get(user.category_id) if user.category_id else None,
                    "avg_rating": media,
                    "review_count": quante,
                })
            
            # Le ultime domande della community: sul telefono, a chi ha fatto
            # l'accesso, la home mostra le sue cose invece della vetrina, e
            # queste sono il "che c'e' di nuovo". Si caricano col resto della
            # pagina: un secondo giro di rete farebbe comparire il blocco dopo.
            domande_recenti = []
            if current_user:
                from app.models import CommunityQuestion

                righe = session.exec(
                    select(CommunityQuestion)
                    .where(CommunityQuestion.validation == True)  # noqa: E712
                    .order_by(CommunityQuestion.created_at.desc())
                    .limit(3)
                ).all()
                for domanda in righe:
                    categoria = categorie.get(domanda.category_id) if domanda.category_id else None
                    domande_recenti.append({
                        "id": domanda.id,
                        "titolo": domanda.title,
                        "categoria": categoria.name if categoria else None,
                        "risposte": domanda.views or 0,
                    })

            logger.info(f"Home page loaded with {len(consultants)} featured consultants")
            
            return request.app.state.templates.TemplateResponse(
                "home.html",
                {
                    "request": request,
                    "categories": categories,
                    "consultants": consultants,
                    "user": current_user,
                    "current_user": current_user,
                    "domande_recenti": domande_recenti,
                    "hero_img_url": "https://i.imgur.com/YourImage.png"
                }
            )
        except Exception as e:
            logger.error(f"Error loading home page: {e}", exc_info=True)
            
            return request.app.state.templates.TemplateResponse(
                "home.html",
                {
                    "request": request,
                    "categories": [],
                    "consultants": [],
                    "user": current_user,
                    "current_user": current_user,
                    "error": "Errore nel caricamento della pagina"
                }
            )
