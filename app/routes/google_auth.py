"""
Google OAuth2 Login Route
Gestisce il login/registrazione tramite account Google
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from sqlmodel import select

from app.database import get_session
from app.models import User
from app.utils.password import UNUSABLE_PASSWORD
from app.utils.lingue_ui import lingua_di
from app.utils.lingua_profilo import applica_lingua
from app.logger_config import logger

import jwt
from datetime import datetime, timedelta

router = APIRouter()

# Configurazione OAuth
oauth = OAuth()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    logger.info("✅ Google OAuth configurato correttamente")
else:
    logger.warning("⚠️ Google OAuth non configurato: GOOGLE_CLIENT_ID o GOOGLE_CLIENT_SECRET mancanti")


def callback_url(request: Request) -> str:
    """URL di ritorno da comunicare a Google.

    Deve coincidere carattere per carattere con uno degli "URI di reindirizzamento
    autorizzati" della console Google, altrimenti Google risponde
    `400 redirect_uri_mismatch`.

    Ricavarlo dalla richiesta non funziona in produzione: Render chiude l'HTTPS
    sul proprio proxy e inoltra all'app in HTTP, quindi `url_for` produceva
    `http://…/auth/google/callback` — un indirizzo che Google non permette
    nemmeno di registrare su un dominio pubblico. Si usa quindi BASE_URL, come
    già fa il resto dell'app per i link nelle email e i redirect di Stripe.
    """
    base = os.getenv("BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/auth/google/callback"

    # Senza BASE_URL (sviluppo locale) si ripiega sulla richiesta, correggendo lo
    # schema se un proxy dichiara che il client era su HTTPS.
    url = str(request.url_for("google_callback"))
    if request.headers.get("x-forwarded-proto", "").lower() == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


@router.get("/auth/google")
async def google_login(request: Request):
    """Redirect a Google per il login OAuth"""
    if not GOOGLE_CLIENT_ID:
        return RedirectResponse("/login", status_code=302)

    redirect_uri = callback_url(request)
    logger.info(f"🔑 Google OAuth: redirect_uri inviato a Google = {redirect_uri}")

    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/google/callback")
async def google_callback(request: Request):
    """Callback da Google dopo il consenso dell'utente"""
    try:
        token = await oauth.google.authorize_access_token(request)
        userinfo = token.get("userinfo")
        
        if not userinfo:
            logger.error("❌ Google OAuth: nessuna userinfo nel token")
            return RedirectResponse("/login", status_code=302)
        
        google_id = userinfo.get("sub")
        email = userinfo.get("email")
        nome = userinfo.get("given_name", "")
        cognome = userinfo.get("family_name", "")
        
        if not email or not google_id:
            logger.error("❌ Google OAuth: email o sub mancanti nella risposta")
            return RedirectResponse("/login", status_code=302)

        # Senza email_verified il collegamento per email qui sotto diventa un
        # takeover: basterebbe un account Google con l'indirizzo della vittima
        # non verificato per entrare nel suo account Ispiramy.
        if userinfo.get("email_verified") is False:
            logger.warning(f"⚠️ Google OAuth: email non verificata da Google ({email}), login rifiutato")
            return RedirectResponse("/login?error=email_non_verificata", status_code=302)

        logger.info(f"🔑 Google OAuth callback per: {email} (sub: {google_id})")
        
        with get_session() as session:
            # 1. Cerca utente per google_id
            user = session.exec(
                select(User).where(User.google_id == google_id)
            ).first()
            
            if user:
                # Utente già collegato con Google → login.
                # L'email l'ha verificata Google: un confirmed rimasto a 0
                # (account vecchio) non deve bloccare il prossimo accesso con password.
                if user.confirmed != 1:
                    _conferma_da_google(user)
                    session.add(user)
                    session.commit()
                    session.refresh(user)
                logger.info(f"✅ Google login per utente esistente: {user.email} (ID: {user.id})")
                _set_session(request, session, user)
                risposta = RedirectResponse("/profile", status_code=302)
                applica_lingua(request, risposta, session, user)
                return risposta
            
            # 2. Cerca utente per email (account già registrato con email/password)
            user = session.exec(
                select(User).where(User.email == email)
            ).first()
            
            if user:
                # Collega account Google all'utente esistente
                user.google_id = google_id
                _conferma_da_google(user)
                if not user.nome and nome:
                    user.nome = nome
                if not user.cognome and cognome:
                    user.cognome = cognome
                session.add(user)
                session.commit()
                session.refresh(user)
                
                logger.info(f"🔗 Account Google collegato a utente esistente: {user.email} (ID: {user.id})")
                _set_session(request, session, user)
                risposta = RedirectResponse("/profile", status_code=302)
                applica_lingua(request, risposta, session, user)
                return risposta
            
            # 3. Nuovo utente → crea account.
            # Nessuna password: l'accesso avviene via Google. Il campo resta
            # valorizzato con un valore impossibile da indovinare, cosi' non
            # esiste una password "vera" da rubare. Per accedere anche con
            # email e password l'utente usa "password dimenticata".
            password_hash = UNUSABLE_PASSWORD
            
            new_user = User(
                email=email,
                password_md5=password_hash,
                nome=nome or None,
                cognome=cognome or None,
                google_id=google_id,
                confirmed=1,  # Email già verificata da Google
                ui_language=lingua_di(request),
            )
            
            session.add(new_user)
            session.commit()
            session.refresh(new_user)
            
            logger.info(f"✅ Nuovo utente registrato via Google: {email} (ID: {new_user.id})")
            _set_session(request, session, new_user)
            risposta = RedirectResponse("/profile", status_code=302)
            applica_lingua(request, risposta, session, new_user)
            return risposta
    
    except Exception as e:
        # Caso tipico dopo il passaggio a BASE_URL: l'utente ha avviato il login
        # su un dominio diverso (es. www.ispiramy.com) da quello di BASE_URL
        # (ispiramy.com). Lo "state" OAuth sta nel cookie di sessione del primo
        # dominio, che non viene inviato al secondo, e authlib rifiuta il ritorno.
        if e.__class__.__name__ == "MismatchingStateError":
            logger.error(
                "❌ Google OAuth: state non corrispondente. Il login è partito da "
                f"{request.headers.get('referer', 'un dominio sconosciuto')} ma il "
                f"ritorno è su {request.url.netloc}: BASE_URL deve essere il dominio "
                "che gli utenti usano davvero."
            )
        else:
            logger.error(f"❌ Errore Google OAuth callback: {e}", exc_info=True)
        return RedirectResponse("/login", status_code=302)


def _conferma_da_google(user: User) -> None:
    """Google ha già verificato l'indirizzo."""
    if user.confirmed != 1:
        user.confirmed = 1
        user.confirmation_code = None
        user.confirmation_code_created_at = None


def _set_session(request: Request, session, user: User):
    """Imposta la sessione con JWT e dati utente"""
    JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
    token_data = {
        "user_id": user.id,
        "email": user.email,
        "exp": datetime.utcnow() + timedelta(days=7)
    }
    access_token = jwt.encode(token_data, JWT_SECRET, algorithm="HS256")
    
    request.session["access_token"] = access_token
    request.session["user_id"] = user.id
    request.session["user_email"] = user.email
    request.session["user_nome"] = user.nome or "User"
