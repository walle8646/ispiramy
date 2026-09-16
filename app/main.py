import os
import secrets
import base64
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response, PlainTextResponse
from app.database import create_db_and_tables
from app.routes import home, auth, consultants, user_profile, messages, community, public_profile, availability, booking, consultation, stripe_webhook, stripe_connect, notifications, review, dispute, admin, admin_social, paypal_payment, google_auth, pages, pwa
from app.logger_config import logger
from app.scheduler import start_scheduler, shutdown_scheduler
from app.utils.template_helpers import get_all_categories
from app.utils_user import get_display_name, get_default_avatar
from app.utils.rate_limit import RateLimitExceeded

app = FastAPI(title="Ispiramy", version="1.0.0")


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Risponde al 429 sia con `detail` (standard FastAPI) sia con `error`,
    che è il campo letto dalle pagine del sito."""
    return JSONResponse(
        {"error": exc.detail, "detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


# Middleware per aggiungere categorie globalmente ai template
class CategoriesMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Carica categorie e le rende disponibili nel request.state
        request.state.categories = get_all_categories()
        # Rendi disponibile il token CSRF nel request.state per i template
        request.state.csrf_token = request.session.get("csrf_token", "")
        response = await call_next(request)
        return response


# CSRF Protection Middleware
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT_PATHS = {"/api/stripe/webhook", "/api/stripe/connect-webhook", "/webhook/stripe", "/webhook/stripe/connect"}

class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Genera token CSRF se non presente nella sessione
        if "csrf_token" not in request.session:
            request.session["csrf_token"] = secrets.token_hex(32)

        # Verifica CSRF solo per metodi che modificano dati
        if request.method not in CSRF_SAFE_METHODS:
            # Escludi webhook Stripe (hanno la propria firma)
            if request.url.path not in CSRF_EXEMPT_PATHS:
                token_from_session = request.session.get("csrf_token", "")
                # Accetta token da header (fetch/AJAX) o da form field (HTML form)
                token_from_request = request.headers.get("X-CSRF-Token", "")
                if not token_from_request:
                    # Fallback: cerca nei form data
                    content_type = request.headers.get("content-type", "")
                    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                        from starlette.datastructures import UploadFile
                        body = await request.body()
                        # Ripristina il body per i middleware successivi
                        async def receive():
                            return {"type": "http.request", "body": body}
                        request._receive = receive
                        from urllib.parse import parse_qs
                        if "application/x-www-form-urlencoded" in content_type:
                            form_data = parse_qs(body.decode("utf-8"))
                            token_from_request = form_data.get("csrf_token", [""])[0]

                if not token_from_session or not token_from_request or not secrets.compare_digest(token_from_request, token_from_session):
                    return JSONResponse(
                        {"error": "Token CSRF non valido. Ricarica la pagina e riprova."},
                        status_code=403
                    )

        response = await call_next(request)
        return response


# Basic Auth per proteggere lo staging.
# Attivo SOLO se STAGING_PASSWORD è impostata (quindi: ON su staging, OFF su prod e locale).
STAGING_PASSWORD = os.getenv("STAGING_PASSWORD", "")
STAGING_USER = os.getenv("STAGING_USER", "ispiramy")
# Path esenti (webhook esterni non possono fare Basic Auth)
STAGING_AUTH_EXEMPT = ("/webhook/", "/api/stripe/webhook", "/api/stripe/connect-webhook")


class StagingAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if STAGING_PASSWORD:
            path = request.url.path
            if not any(path.startswith(p) for p in STAGING_AUTH_EXEMPT):
                auth = request.headers.get("Authorization", "")
                authorized = False
                if auth.startswith("Basic "):
                    try:
                        decoded = base64.b64decode(auth[6:]).decode("utf-8")
                        user, _, pwd = decoded.partition(":")
                        if secrets.compare_digest(user, STAGING_USER) and secrets.compare_digest(pwd, STAGING_PASSWORD):
                            authorized = True
                    except Exception:
                        authorized = False
                if not authorized:
                    return PlainTextResponse(
                        "Area riservata (staging Ispiramy)",
                        status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Ispiramy Staging"'},
                    )
        return await call_next(request)


# Middleware (Starlette esegue in ordine inverso: ultimo aggiunto = più esterno)
# Ordine di esecuzione: StagingAuth → CategoriesMiddleware → CSRFMiddleware → SessionMiddleware → App

# 1. CategoriesMiddleware — più interno, serve le categorie ai template
app.add_middleware(CategoriesMiddleware)

# 2. CSRF middleware — valida token su POST/PUT/DELETE
app.add_middleware(CSRFMiddleware)

# 3. Session middleware — gestisce le sessioni
# In produzione (HTTPS) usiamo SameSite=None + Secure così il cookie di sessione
# sopravvive al redirect di ritorno da Stripe Checkout (origin esterna): senza questo,
# con SameSite=Lax alcuni browser (es. Safari) scartano il cookie e l'utente risulta
# sloggato dopo aver prenotato. In locale (HTTP) restiamo su Lax perché Secure
# impedirebbe del tutto il salvataggio del cookie su connessione non cifrata.
_cookie_https = os.getenv("BASE_URL", "").startswith("https")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "ispiramy-super-secret-key-change-in-production-2024"),
    max_age=86400,
    same_site="none" if _cookie_https else "lax",
    https_only=_cookie_https,
)

# 4. Staging Basic Auth — più esterno: blocca tutto prima di ogni altra logica
app.add_middleware(StagingAuthMiddleware)

# Templates
templates = Jinja2Templates(directory="app/templates")
# Aggiungi filtro personalizzato per nomi utenti
templates.env.filters['display_name'] = get_display_name
templates.env.filters['default_avatar'] = get_default_avatar

# Anno corrente globale (per footer copyright sempre aggiornato)
from datetime import datetime as _dt
templates.env.globals['current_year'] = _dt.now().year


def statico(percorso: str) -> str:
    """URL di un file statico con la sua data di modifica in coda.

    StaticFiles non manda un max-age, quindi i browser applicano una scadenza
    "a sentimento" che per un file vecchio dura giorni: dopo un rilascio si
    continuava a usare il JavaScript e il CSS vecchi (con effetti difficili da
    capire, tipo un errore su un'operazione in realtà riuscita). Il numero di
    versione scritto a mano nei template invece ci si dimenticava di cambiarlo.
    """
    relativo = percorso.lstrip("/")
    if relativo.startswith("static/"):
        relativo = relativo[len("static/"):]
    try:
        versione = int((Path("app/static") / relativo).stat().st_mtime)
    except OSError:
        return f"/static/{relativo}"
    return f"/static/{relativo}?v={versione}"


templates.env.globals['statico'] = statico

# Le regole di preavviso servono anche al JavaScript delle pagine: un solo
# valore, definito in app/utils/orari.py.
from app.utils.orari import ORE_LIMITE_ANNULLAMENTO, ORE_PREAVVISO_PRENOTAZIONE
templates.env.globals['ore_preavviso'] = ORE_PREAVVISO_PRENOTAZIONE
templates.env.globals['ore_limite_annullamento'] = ORE_LIMITE_ANNULLAMENTO

# Filtro per parsing JSON (usato per le immagini community)
import json as _json
def _parse_json(value):
    """Parse una stringa JSON in un oggetto Python. Ritorna lista vuota se invalido."""
    if not value:
        return []
    try:
        result = _json.loads(value)
        return result if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []
templates.env.filters['parse_json'] = _parse_json

app.state.templates = templates

# Crea directory uploads
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
(UPLOAD_DIR / "profile_pictures").mkdir(exist_ok=True)

# Monta static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# Include routes
app.include_router(home.router, tags=["home"])
app.include_router(auth.router, tags=["auth"])
app.include_router(user_profile.router, tags=["profile"])
app.include_router(public_profile.router, tags=["public_profile"])
app.include_router(consultants.router, tags=["consultants"])
app.include_router(messages.router, tags=["messages"])  # ✅ Aggiungi questo
app.include_router(community.router)
app.include_router(availability.router, tags=["availability"])
app.include_router(booking.router, tags=["booking"])
app.include_router(consultation.router, tags=["consultation"])
app.include_router(stripe_webhook.router, tags=["webhooks"])
app.include_router(stripe_connect.router, tags=["stripe_connect"])
app.include_router(notifications.router, tags=["notifications"])
app.include_router(review.router, tags=["reviews"])
app.include_router(dispute.router, tags=["disputes"])
app.include_router(admin.router, tags=["admin"])
app.include_router(admin_social.router, tags=["admin-social"])
app.include_router(paypal_payment.router, tags=["paypal"])
app.include_router(google_auth.router, tags=["google_auth"])
app.include_router(pages.router, tags=["pages"])
app.include_router(pwa.router, tags=["webapp"])


# Test S3 credentials
def test_s3_credentials():
    """Testa se le credenziali S3 sono valide"""
    try:
        import boto3
        from botocore.exceptions import ClientError
        import sys
        
        aws_access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        aws_s3_bucket_name = os.getenv("AWS_S3_BUCKET_NAME")
        aws_s3_region = os.getenv("AWS_S3_REGION", "eu-south-1")
        
        if not all([aws_access_key_id, aws_secret_access_key, aws_s3_bucket_name]):
            msg = "⚠️ [S3] AWS credentials non configurate completamente"
            print(msg, file=sys.stderr)
            logger.warning(msg)
            return False
        
        msg = "\n🔐 [S3] Testing AWS credentials..."
        print(msg, file=sys.stderr)
        logger.info(msg)
        sys.stderr.flush()
        
        s3_client = boto3.client(
            's3',
            region_name=aws_s3_region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key
        )
        
        # Test: head_bucket verifica se il bucket è accessibile
        s3_client.head_bucket(Bucket=aws_s3_bucket_name)
        msg = f"✅ [S3] AWS credentials VALID - Bucket '{aws_s3_bucket_name}' is accessible"
        print(msg, file=sys.stderr)
        logger.info(msg)
        msg2 = f"   Region: {aws_s3_region}"
        print(msg2, file=sys.stderr)
        logger.info(msg2)
        
        # Lista i primi file nel bucket
        try:
            response = s3_client.list_objects_v2(Bucket=aws_s3_bucket_name, MaxKeys=5)
            if 'Contents' in response:
                file_count = response.get('KeyCount', 0)
                msg3 = f"   Files in bucket: {file_count}"
                print(msg3, file=sys.stderr)
                logger.info(msg3)
                if file_count > 0:
                    logger.info(f"   Sample files:")
                    for obj in response['Contents'][:3]:
                        logger.info(f"     - {obj['Key']}")
            else:
                msg4 = f"   Bucket is empty"
                print(msg4, file=sys.stderr)
                logger.info(msg4)
        except Exception as e:
            msg5 = f"   (Could not list files: {str(e)})"
            print(msg5, file=sys.stderr)
            logger.info(msg5)
        
        sys.stderr.flush()
        return True
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchBucket':
            msg = f"❌ [S3] ERROR - Bucket '{aws_s3_bucket_name}' does not exist"
            print(msg, file=sys.stderr)
            logger.error(msg)
        elif error_code == 'InvalidAccessKeyId':
            msg = f"❌ [S3] ERROR - Invalid AWS Access Key ID"
            print(msg, file=sys.stderr)
            logger.error(msg)
        elif error_code == 'SignatureDoesNotMatch':
            msg = f"❌ [S3] ERROR - Invalid AWS Secret Access Key"
            print(msg, file=sys.stderr)
            logger.error(msg)
        else:
            msg = f"❌ [S3] ERROR - {error_code}: {str(e)}"
            print(msg, file=sys.stderr)
            logger.error(msg)
        return False
    except Exception as e:
        msg = f"❌ [S3] ERROR - {str(e)}"
        print(msg, file=sys.stderr)
        logger.error(msg)
        return False


# Database init
@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    test_s3_credentials()  # Test S3 credentials early
    start_scheduler()  # Avvia lo scheduler per le notifiche programmate
    logger.info("✅ Ispiramy started successfully")


@app.on_event("shutdown")
def on_shutdown():
    shutdown_scheduler()  # Ferma lo scheduler in modo pulito
    logger.info("👋 Ispiramy shutting down")


# Esecuzione locale
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=True)
