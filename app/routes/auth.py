from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from app.database import account_gia_esistente, get_session
from app.models import User
from sqlmodel import select
import time
from typing import Optional
from html import escape as html_escape
from urllib.parse import quote_plus
from app.logger_config import logger
from app.utils.email import generate_verification_code
from app.utils.lingue_ui import lingua_di, traduci
from app.utils.lingua_profilo import applica_lingua
from app.utils.password import hash_password, verify_password
from app.utils.rate_limit import clear_attempts, enforce_rate_limit
from app.utils.orari import now_italy_naive
import os
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from loguru import logger
import jwt
from datetime import datetime, timedelta
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

router = APIRouter()

# Cache last_seen per evitare UPDATE al DB ad ogni richiesta
# Aggiorna last_seen solo ogni 5 minuti per utente
_last_seen_cache: dict[int, float] = {}
_LAST_SEEN_INTERVAL = 300  # 5 minuti in secondi

def verify_token(request: Request) -> Optional[User]:
    """
    Verifica token JWT e restituisce utente autenticato
    """
    try:
        # ✅ Leggi token dalla sessione
        token = request.session.get("access_token")
        
        if not token:
            logger.debug(f"Session keys: {list(request.session.keys())}")
            
            # Fallback: controlla se c'è user_id nella sessione (sessione senza JWT)
            user_id = request.session.get("user_id")
            if user_id:
                with get_session() as session:
                    user = session.get(User, user_id)
                    if user:
                        _maybe_update_last_seen(session, user)
                        return user
            
            return None
        
        # Decodifica JWT
        JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            user_id = payload.get("user_id")
            
            if not user_id:
                logger.warning("⚠️ No user_id in token payload")
                return None
        
        except jwt.ExpiredSignatureError:
            logger.warning("⚠️ Token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"⚠️ Invalid token: {str(e)}")
            return None
        
        # ✅ Ottieni utente dal database
        with get_session() as session:
            user = session.get(User, user_id)
            
            if not user:
                logger.warning(f"⚠️ User {user_id} not found in database")
                return None
            
            _maybe_update_last_seen(session, user)
            
            return user
    
    except Exception as e:
        logger.error("Error verifying token: " + str(e).replace("{", "{{").replace("}", "}}"), exc_info=True)
        return None


def _maybe_update_last_seen(session, user: User):
    """Aggiorna last_seen solo se sono passati più di 5 minuti dall'ultimo aggiornamento"""
    now = time.time()
    last_update = _last_seen_cache.get(user.id, 0)
    if now - last_update > _LAST_SEEN_INTERVAL:
        user.last_seen = datetime.utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
        _last_seen_cache[user.id] = now

# Alias per compatibilità
def get_current_user(request: Request) -> Optional[User]:
    """Alias di verify_token"""
    return verify_token(request)
    if not user:
        raise HTTPException(status_code=401, detail="Non autenticato")
    return user


# Requisito minimo sulla password. Volutamente basso ma non nullo: prima non
# c'era alcun controllo e "1" era una password accettata.
MIN_PASSWORD_LENGTH = 8

# Validità del codice di verifica email: è quella scritta nell'email.
VALIDITA_CODICE_VERIFICA = timedelta(minutes=15)
# Il codice di reset è un altro: 10 minuti, scritti anche nell'email.
VALIDITA_CODICE_RESET = timedelta(minutes=10)


def _risposta_email_non_confermata(request: Request, email: str, html: bool = False):
    """Login rifiutato perché l'email non è ancora confermata.

    Il messaggio segue la lingua della pagina (il cookie), non una frase
    italiana fissa: con l'inglese selezionato prima si leggeva comunque
    «Conferma prima la tua email».
    """
    messaggio = traduci("accesso.conferma_prima", lingua_di(request))
    if html:
        return request.app.state.templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": messaggio,
                "richiede_conferma": True,
                "email_da_confermare": email,
            },
        )
    return JSONResponse(
        {
            "error": messaggio,
            "requires_verification": True,
            "email": email,
        },
        status_code=403,
    )


def _sblocca_se_account_precedente(session, user: User) -> None:
    """Dopo la password giusta, un account precedente entra anche con un codice in sospeso.

    Una password sbagliata non arriva qui, quindi non conferma niente.
    """
    if user.confirmed == 1:
        return
    if not account_gia_esistente(user.created_at, getattr(user, "google_id", None)):
        return
    user.confirmed = 1
    user.confirmation_code = None
    user.confirmation_code_created_at = None
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info(f"✅ Account precedente confermato al login: {user.email}")


def _entra(request: Request, user: User) -> None:
    """Apre la sessione come il login: JWT e dati utente."""
    jwt_secret = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
    token_data = {
        "user_id": user.id,
        "email": user.email,
        "exp": datetime.utcnow() + timedelta(days=7),
    }
    request.session["access_token"] = jwt.encode(token_data, jwt_secret, algorithm="HS256")
    request.session["user_id"] = user.id
    request.session["user_email"] = user.email
    request.session["user_nome"] = user.nome or "User"


def _applica_codice(session, email: str, code: str):
    """Conferma l'email se il codice è quello giusto e non è scaduto.

    Ritorna (utente, errore). errore è None quando la conferma è andata a buon fine.
    """
    user = session.exec(select(User).where(User.email == email)).first()
    if not user:
        return None, {"chiave": "accesso.email_sconosciuta", "status": 404}
    if user.confirmed == 1:
        return user, {"chiave": "accesso.gia_confermata", "status": 400}
    if not user.confirmation_code or not secrets.compare_digest(
            user.confirmation_code, (code or "").strip()):
        logger.warning(f"❌ Invalid code for {email}")
        return user, {"chiave": "accesso.codice_errato", "status": 400}
    generato = user.confirmation_code_created_at
    if generato is None or now_italy_naive() - generato > VALIDITA_CODICE_VERIFICA:
        return user, {"chiave": "accesso.codice_scaduto", "status": 400, "expired": True}
    user.confirmed = 1
    user.confirmation_code = None
    user.confirmation_code_created_at = None
    session.add(user)
    session.commit()
    return user, None


def _url_conferma(email: str, code: str = "") -> str:
    base = os.getenv("BASE_URL", "http://localhost:8080").rstrip("/")
    url = f"{base}/conferma-email?email={quote_plus(email or '')}"
    if code:
        url += f"&code={quote_plus(code)}"
    return url


def _password_troppo_debole(password: str, lingua: str = "it") -> Optional[str]:
    """Ritorna il motivo del rifiuto, oppure None se la password va bene."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return traduci("password.corta", lingua).replace("{n}", str(MIN_PASSWORD_LENGTH))
    if password.isdigit():
        return traduci("password.solo_numeri", lingua)
    return None


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...)
):
    """Login con form HTML"""
    # Forza bruta sulle password: massimo 10 tentativi in 5 minuti
    enforce_rate_limit(request, "login", limit=10, window_seconds=300, extra_key=email)
    with get_session() as session:
        statement = select(User).where(User.email == email)
        user = session.exec(statement).first()
        
        if not user:
            return request.app.state.templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Email o password non corretti"}
            )
        
        password_ok, needs_rehash = verify_password(password, user.password_md5)

        if not password_ok:
            return request.app.state.templates.TemplateResponse(
                "login.html",
                # Non distinguere fra email inesistente e password errata:
                # il messaggio diverso permetteva di enumerare gli account.
                {"request": request, "error": "Email o password non corretti"}
            )

        # Migrazione trasparente da MD5 a bcrypt al primo login riuscito
        if needs_rehash:
            user.password_md5 = hash_password(password)
            session.add(user)
            session.commit()
            session.refresh(user)
            logger.info(f"🔐 Password migrata a bcrypt per {user.email}")

        _sblocca_se_account_precedente(session, user)
        if user.confirmed != 1:
            return _risposta_email_non_confermata(request, email, html=True)
        
        clear_attempts(request, "login", email)

        request.session["user_id"] = user.id
        request.session["user_email"] = user.email
        request.session["user_nome"] = user.nome or "User"

        logger.info(f"✅ User logged in: {user.email}")

        risposta = RedirectResponse(url="/profile", status_code=303)
        applica_lingua(request, risposta, session, user)
        return risposta

@router.post("/api/login")
async def api_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...)
):
    """API endpoint per login (AJAX)"""
    enforce_rate_limit(request, "login", limit=10, window_seconds=300, extra_key=email)
    try:
        with get_session() as session:
            user = session.exec(
                select(User).where(User.email == email)
            ).first()
            
            if not user:
                return JSONResponse(
                    {"error": "Email o password non corretti"},
                    status_code=401
                )
            
            # Verifica password (bcrypt, con fallback sul vecchio MD5)
            password_ok, needs_rehash = verify_password(password, user.password_md5)
            if not password_ok:
                return JSONResponse(
                    {"error": "Email o password non corretti"},
                    status_code=401
                )

            # Migrazione trasparente da MD5 a bcrypt al primo login riuscito
            if needs_rehash:
                user.password_md5 = hash_password(password)
                session.add(user)
                session.commit()
                session.refresh(user)
                logger.info(f"🔐 Password migrata a bcrypt per {user.email}")

            # L'email deve essere confermata: il form /login lo controllava gia',
            # questo endpoint no, quindi la verifica via codice era aggirabile
            # semplicemente usando il login AJAX. Gli account precedenti, anche
            # con un codice rimasto in sospeso, non vengono più fermati.
            _sblocca_se_account_precedente(session, user)
            if user.confirmed != 1:
                return _risposta_email_non_confermata(request, user.email)
            
            # ✅ Genera token JWT
            JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
            token_data = {
                "user_id": user.id,
                "email": user.email,
                "exp": datetime.utcnow() + timedelta(days=7)
            }
            access_token = jwt.encode(token_data, JWT_SECRET, algorithm="HS256")
            clear_attempts(request, "login", email)
            
            # ✅ SALVA TOKEN IN SESSION (IMPORTANTE!)
            request.session["access_token"] = access_token
            request.session["user_id"] = user.id
            request.session["user_email"] = user.email
            request.session["user_nome"] = user.nome
            
            logger.info(f"✅ User logged in: {user.nome} ({user.email})")
            logger.debug("Sessione inizializzata per user %s", user.id)
            
            risposta = JSONResponse({
                "success": True,
                "message": "Login effettuato con successo",
                "user": {
                    "id": user.id,
                    "nome": user.nome,
                    "email": user.email
                }
            }, status_code=200)
            applica_lingua(request, risposta, session, user)
            return risposta
    
    except Exception as e:
        logger.error("Login error: " + str(e).replace("{", "{{").replace("}", "}}"), exc_info=True)
        return JSONResponse(
            {"error": "Errore durante il login"},
            status_code=500
        )

@router.post("/api/register")
async def api_register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    nome: str = Form(...),
    cognome: str = Form(None)
):
    """API Registrazione con invio email di conferma"""
    # Creazione account a raffica (e invio email a spese nostre)
    enforce_rate_limit(request, "register", limit=5, window_seconds=3600)

    motivo = _password_troppo_debole(password, lingua_di(request))
    if motivo:
        return JSONResponse({"error": motivo}, status_code=400)
    try:
        with get_session() as session:
            existing = session.exec(select(User).where(User.email == email)).first()
            
            if existing:
                if existing.confirmed == 1:
                    logger.warning(f"❌ Registration attempt with existing email: {email}")
                    return JSONResponse({
                        "error": "Email già registrata",
                        "message": "Questa email è già registrata. Hai già un account?",
                        "redirect_url": "/login",
                        "show_login_link": True
                    }, status_code=400)
                else:
                    code = generate_verification_code()
                    existing.confirmation_code = code
                    existing.confirmation_code_created_at = now_italy_naive()
                    session.add(existing)
                    session.commit()
                    
                    # ✅ Ora funziona con 3 parametri
                    send_verification_email(email, code, existing.nome or "User", lingua_di(request))
                    
                    logger.info(f"♻️ Resent confirmation code to: {email}")
                    
                    return JSONResponse({
                        "message": "Codice di verifica inviato nuovamente!",
                        "email": email,
                        "requires_verification": True
                    }, status_code=200)
            
            password_hash = hash_password(password)
            code = generate_verification_code()
            
            new_user = User(
                email=email,
                password_md5=password_hash,
                nome=nome,
                cognome=cognome,
                confirmed=0,
                confirmation_code=code,
                confirmation_code_created_at=now_italy_naive(),
                ui_language=lingua_di(request),
            )
            
            session.add(new_user)
            session.commit()
            session.refresh(new_user)
            
            # ✅ Ora funziona con 3 parametri
            email_sent = send_verification_email(email, code, nome, lingua_di(request))
            
            if not email_sent:
                logger.warning(f"⚠️ User registered but email failed: {email}")
            
            logger.info(f"✅ New user registered: {email} (ID: {new_user.id}) - codice inviato via email")
            
            return JSONResponse({
                "message": "Registrazione completata! Controlla la tua email per il codice di verifica.",
                "email": email,
                "requires_verification": True
            }, status_code=201)
    
    except Exception as e:
        logger.error(f"Error during registration: {e}", exc_info=True)
        return JSONResponse(
            {"error": "Errore durante la registrazione. Riprova."},
            status_code=500
        )

@router.post("/api/verify-email")
async def verify_email(
    request: Request,
    email: str = Form(...),
    code: str = Form(...)
):
    """Verifica codice email"""
    # Il codice e' di 6 cifre: senza limite si enumera in poche ore
    enforce_rate_limit(request, "verify_email", limit=10, window_seconds=900, extra_key=email)
    lingua = lingua_di(request)
    try:
        with get_session() as session:
            user, esito = _applica_codice(session, email, code)
            if esito:
                corpo = {"error": traduci(esito["chiave"], lingua)}
                if esito.get("expired"):
                    corpo["expired"] = True
                return JSONResponse(corpo, status_code=esito["status"])

            _entra(request, user)
            logger.info(f"✅ Email verified for: {email}")
            return JSONResponse({
                "message": traduci("accesso.email_verificata", lingua),
                "redirect_url": "/profile",
            }, status_code=200)

    except Exception as e:
        logger.error(f"Error during verification: {e}", exc_info=True)
        return JSONResponse({"error": "Errore durante la verifica"}, status_code=500)

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Pagina di login"""
    return request.app.state.templates.TemplateResponse(
        "login.html",
        {"request": request}
    )


@router.get("/conferma-email", response_class=HTMLResponse)
async def pagina_conferma_email(request: Request, email: str = "", code: str = ""):
    """Pagina per inserire il codice. Con email e codice nel link, conferma e fa entrare.

    Il link con il codice è il pulsante dell'email. Senza codice si mostra solo il modulo:
    aprirlo non conferma nessuno.
    """
    lingua = lingua_di(request)
    email = (email or "").strip()
    code = (code or "").strip()
    errore = None
    if email and code:
        enforce_rate_limit(request, "verify_email", limit=10, window_seconds=900, extra_key=email)
        with get_session() as session:
            user, esito = _applica_codice(session, email, code)
            if esito is None and user is not None:
                _entra(request, user)
                clear_attempts(request, "login", email)
                logger.info(f"✅ Email verified via link for: {email}")
                risposta = RedirectResponse(url="/profile", status_code=303)
                applica_lingua(request, risposta, session, user)
                return risposta
            if esito:
                errore = traduci(esito["chiave"], lingua)
    return request.app.state.templates.TemplateResponse(
        "conferma_email.html",
        {
            "request": request,
            "email_da_confermare": email,
            "codice_iniziale": code,
            "error": errore,
        },
    )

@router.post("/api/resend-verification")
async def resend_verification(
    request: Request,
    email: str = Form(...)
):
    """Reinvia codice di verifica"""
    lingua = lingua_di(request)
    # Tre email ogni 15 minuti: il pulsante sul login non deve poter inondare la casella.
    enforce_rate_limit(
        request, "resend_verification", limit=3, window_seconds=900, extra_key=email,
        message=traduci("accesso.reinvia_attesa", lingua),
    )
    try:
        with get_session() as session:
            user = session.exec(select(User).where(User.email == email)).first()
            
            if not user:
                return JSONResponse({"error": traduci("accesso.email_sconosciuta", lingua)}, status_code=404)
            
            if user.confirmed == 1:
                return JSONResponse({"error": traduci("accesso.gia_confermata", lingua)}, status_code=400)
            
            code = generate_verification_code()
            user.confirmation_code = code
            user.confirmation_code_created_at = now_italy_naive()
            session.add(user)
            session.commit()
            
            send_verification_email(email, code, user.nome or "User", lingua)
            
            logger.info(f"♻️ Resent code to {email}")
            
            return JSONResponse({"message": traduci("accesso.reinvia_ok", lingua)}, status_code=200)
    
    except Exception as e:
        logger.error(f"Error resending code: {e}", exc_info=True)
        return JSONResponse({"error": traduci("accesso.reinvia_errore", lingua)}, status_code=500)

@router.get("/logout")
async def logout(request: Request):
    """Logout utente"""
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# ========== RESET PASSWORD ROUTES ==========

@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    """Pagina reset password"""
    return request.app.state.templates.TemplateResponse(
        "reset_password.html",
        {"request": request}
    )

@router.post("/api/request-password-reset")
async def request_password_reset(
    request: Request,
    email: str = Form(...)
):
    """API: richiedi reset password (invia codice via email)"""
    enforce_rate_limit(request, "reset_request", limit=3, window_seconds=900, extra_key=email)
    try:
        with get_session() as session:
            user = session.exec(select(User).where(User.email == email)).first()
            
            if not user:
                # Risposta identica al caso "email esistente": rispondere 404
                # permetteva di scoprire quali indirizzi sono registrati.
                logger.info("🔐 Reset password richiesto per un'email non registrata")
                return JSONResponse({
                    "success": True,
                    "message": "Se l'email è registrata, riceverai un codice"
                }, status_code=200)

            # Genera codice reset (6 cifre)
            reset_code = generate_verification_code()  # Usa stessa funzione di verifica
            
            # Salva codice in sessione
            request.session['reset_code'] = reset_code
            request.session['reset_email'] = email
            request.session['reset_timestamp'] = int(time.time())
            
            # Invia email con codice
            send_reset_password_email(email, user.nome or "Utente", reset_code, lingua_di(request))
            
            logger.info(f"🔐 Reset password requested for: {email} - codice inviato via email")
            
            return JSONResponse({
                "success": True,
                "message": "Se l'email è registrata, riceverai un codice"
            }, status_code=200)
    
    except Exception as e:
        logger.error(f"Error requesting password reset: {e}", exc_info=True)
        return JSONResponse({"error": "Errore. Riprova."}, status_code=500)

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """Pagina di registrazione"""
    current_user = verify_token(request)
    
    if current_user:
        return RedirectResponse("/profile", status_code=302)
    
    return request.app.state.templates.TemplateResponse(
        "register.html",
        {"request": request}
    )


@router.post("/api/reset-password")
async def reset_password(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    new_password: str = Form(...)
):
    """API: reset password con codice"""
    enforce_rate_limit(request, "reset_submit", limit=10, window_seconds=900, extra_key=email)

    motivo = _password_troppo_debole(new_password, lingua_di(request))
    if motivo:
        return JSONResponse({"error": motivo}, status_code=400)
    try:
        # Verifica codice in sessione
        if 'reset_code' not in request.session or 'reset_email' not in request.session:
            return JSONResponse({"error": "Codice non trovato. Richiedi un nuovo reset."}, status_code=400)
        
        # Verifica email
        if request.session['reset_email'] != email:
            return JSONResponse({"error": "Email non corrisponde"}, status_code=400)
        
        # Verifica codice
        if request.session['reset_code'] != code:
            logger.warning(f"❌ Invalid reset code for {email}")
            return JSONResponse({"error": "Codice non valido"}, status_code=400)
        
        reset_time = request.session.get('reset_timestamp', 0)
        if int(time.time()) - reset_time > VALIDITA_CODICE_RESET.total_seconds():
            return JSONResponse({"error": "Codice scaduto. Richiedi un nuovo reset."}, status_code=400)
        
        # Hash nuova password
        new_password_hash = hash_password(new_password)
        
        # Aggiorna password nel database
        with get_session() as session:
            user = session.exec(select(User).where(User.email == email)).first()
            
            if not user:
                return JSONResponse({"error": "Utente non trovato"}, status_code=404)
            
            user.password_md5 = new_password_hash
            # Il codice è arrivato su questa casella: l'email è dimostrata,
            # anche se l'account era ancora in attesa della conferma.
            user.confirmed = 1
            user.confirmation_code = None
            user.confirmation_code_created_at = None
            session.add(user)
            session.commit()
            
            logger.info(f"✅ Password reset successful for: {email}")
        
        # Pulisci sessione
        request.session.pop('reset_code', None)
        request.session.pop('reset_email', None)
        request.session.pop('reset_timestamp', None)
        
        return JSONResponse({
            "success": True,
            "message": "Password reimpostata con successo!"
        }, status_code=200)
    
    except Exception as e:
        logger.error(f"Error resetting password: {e}", exc_info=True)
        return JSONResponse({"error": "Errore. Riprova."}, status_code=500)

# ========== FUNZIONE EMAIL RESET PASSWORD ==========

def corpo_email_reset(nome: str, reset_code: str, lingua: str = "it") -> str:
    """HTML del codice di reset. I minuti sono gli stessi del controllo sul server."""
    from app.utils.notification_email import _branded_email
    minuti = int(VALIDITA_CODICE_RESET.total_seconds() // 60)
    code_box = (
        '<div style="font-size:34px;font-weight:800;letter-spacing:8px;color:#2e7d32;'
        'background:#e8f5e9;border-radius:10px;padding:22px;text-align:center;margin:24px 0;">'
        f'{html_escape(reset_code)}</div>'
    )
    if lingua == "en":
        body = (
            f"<p>Hi <strong>{html_escape(nome or '')}</strong>,</p>"
            "<p>You asked to reset your Ispiramy password. Here is the code:</p>"
            + code_box +
            '<div style="background:#fff8e1;border-left:4px solid #ffb300;padding:14px 18px;'
            'border-radius:6px;color:#795548;margin:20px 0;">The code is valid for '
            f'<strong>{minuti} minutes</strong>. If you did not ask for this, ignore this email.</div>'
        )
        titolo = "Reset your password"
    else:
        body = (
            f"<p>Ciao <strong>{html_escape(nome or '')}</strong>,</p>"
            "<p>Hai richiesto di reimpostare la tua password su Ispiramy. Ecco il codice di verifica:</p>"
            + code_box +
            '<div style="background:#fff8e1;border-left:4px solid #ffb300;padding:14px 18px;'
            'border-radius:6px;color:#795548;margin:20px 0;">⚠️ Il codice è valido '
            f'<strong>{minuti} minuti</strong>. Se non hai richiesto il reset, ignora questa email.</div>'
        )
        titolo = "Reset Password"
    return _branded_email("🔑", titolo, "#43a047", "#2e7d32", body), titolo


def send_reset_password_email(email: str, nome: str, reset_code: str, lingua: str = "it") -> bool:
    """Invia email brandizzata con codice reset password."""
    from app.utils.email_backend import is_backend_available, mail_send
    from sendgrid.helpers.mail import Mail
    html_content, titolo = corpo_email_reset(nome, reset_code, lingua)
    if not is_backend_available():
        logger.error("Nessun backend email disponibile per il reset password")
        return False
    api_key = os.getenv('SENDGRID_API_KEY') or os.getenv('SMTP_PASSWORD')
    from_email = os.getenv('FROM_EMAIL', 'noreply@ispiramy.com')
    try:
        message = Mail(from_email=from_email, to_emails=email,
                       subject=f"{titolo} - Ispiramy", html_content=html_content)
        mail_send(api_key, message)
        logger.info(f"Reset password email sent to {email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send reset email to {email}: {e}")
        return False


def corpo_email_verifica(nome: str, code: str, lingua: str = "it", email: str = "") -> tuple:
    """HTML e oggetto dell'email di conferma. I minuti sono quelli del server.

    Il pulsante è un link che conferma da solo. Sotto c'è la pagina in cui si
    può scrivere il codice a mano.
    """
    from app.utils.notification_email import _branded_email
    minuti = int(VALIDITA_CODICE_VERIFICA.total_seconds() // 60)
    code_box = (
        '<div style="font-size:34px;font-weight:800;letter-spacing:8px;color:#2e7d32;'
        'background:#e8f5e9;border-radius:10px;padding:22px;text-align:center;margin:24px 0;">'
        f'{html_escape(code)}</div>'
    )
    link_clic = html_escape(_url_conferma(email, code))
    link_pagina = html_escape(_url_conferma(email))
    if lingua == "en":
        body = (
            f"<p>Hi <strong>{html_escape(nome or '')}</strong>,</p>"
            "<p>Here is your Ispiramy verification code:</p>"
            + code_box +
            "<p>Use the button to confirm in one click, or enter the code on "
            f'<a href="{link_pagina}">the confirmation page</a>. '
            f"The code is valid for <strong>{minuti} minutes</strong>.</p>"
        )
        return _branded_email(
            "✨", "Confirm your email", "#43a047", "#2e7d32", body,
            "Confirm email", link_clic, "en",
        ), "Ispiramy verification code"
    body = (
        f"<p>Ciao <strong>{html_escape(nome or '')}</strong>,</p>"
        "<p>Grazie per esserti registrato su Ispiramy! Ecco il tuo codice di verifica:</p>"
        + code_box +
        "<p>Conferma con un clic sul pulsante, oppure inserisci il codice nella "
        f'<a href="{link_pagina}">pagina di conferma</a>. '
        f"Il codice è valido <strong>{minuti} minuti</strong>.</p>"
    )
    return _branded_email(
        "✨", "Conferma la tua email", "#43a047", "#2e7d32", body,
        "Conferma email", link_clic, "it",
    ), "Codice di Verifica Ispiramy"


def send_verification_email(to_email: str, code: str, nome: str = "User", lingua: str = "it") -> bool:
    """
    Invia email di verifica usando SendGrid API (HTTP).
    
    Args:
        to_email: Email destinatario
        code: Codice verifica 6 cifre
        nome: Nome utente
    
    Returns:
        bool: True se email inviata con successo
    """
    logger.info("=" * 80)
    logger.info("📧 INVIO EMAIL VIA SENDGRID API")
    logger.info("=" * 80)
    
    try:
        # ========== STEP 1: Carica API Key ==========
        api_key = os.getenv('SENDGRID_API_KEY') or os.getenv('SMTP_PASSWORD')
        from_email = os.getenv('FROM_EMAIL', 'noreply@ispiramy.com')
        
        logger.info("📋 STEP 1: Configurazione caricata")
        logger.info(f"   ├─ API_KEY: {'✅ SET' if api_key else '❌ NOT SET'}")
        logger.info(f"   ├─ FROM_EMAIL: {from_email}")
        logger.info(f"   ├─ TO_EMAIL: {to_email}")
        logger.info(f"   └─ NOME: {nome}")
        
        from app.utils.email_backend import is_backend_available
        if not is_backend_available():
            logger.error("Nessun backend email disponibile (RESEND_API_KEY / SENDGRID_API_KEY mancanti)")
            return False
        
        # ========== STEP 2: Costruisci messaggio HTML ==========
        logger.info("📝 STEP 2: Costruzione messaggio...")
        
        html_content, oggetto = corpo_email_verifica(nome, code, lingua, to_email)

        # ========== STEP 3: Crea messaggio SendGrid ==========
        message = Mail(
            from_email=from_email,
            to_emails=to_email,
            subject=oggetto,
            html_content=html_content
        )
        
        logger.info("✅ STEP 2: Messaggio costruito")
        logger.info(f"   ├─ Subject: Codice di Verifica Ispiramy")
        logger.info(f"   ├─ From: {from_email}")
        logger.info(f"   ├─ To: {to_email}")
        logger.info(f"   └─ Codice: {code}")
        
        # ========== STEP 4: Invia via API ==========
        logger.info("📤 STEP 3: Invio via SendGrid API...")
        
        try:
            from app.utils.email_backend import mail_send
            response = mail_send(api_key, message)
            
            logger.info(f"✅ STEP 3: Email inviata!")
            logger.info(f"   ├─ Status Code: {response.status_code}")
            logger.info(f"   ├─ Headers: {dict(response.headers)}")
            logger.info(f"   └─ Body: {response.body}")
            
            logger.info("=" * 80)
            logger.info("🎉 EMAIL INVIATA CON SUCCESSO!")
            logger.info("=" * 80)
            
            return True
        
        except Exception as e:
            logger.error(f"❌ STEP 3: Errore SendGrid API: {type(e).__name__}")
            logger.error(f"   Messaggio: {str(e)}")
            
            # Log dettagli errore SendGrid
            if hasattr(e, 'body'):
                logger.error(f"   Body: {e.body}")
            if hasattr(e, 'to_dict'):
                logger.error(f"   Details: {e.to_dict}")
            
            return False
    
    except Exception as e:
        logger.error("=" * 80)
        logger.error(f"❌ ERRORE FATALE INVIO EMAIL")
        logger.error(f"   Tipo: {type(e).__name__}")
        logger.error(f"   Messaggio: {e}")
        logger.error("=" * 80)
        import traceback
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        return False
