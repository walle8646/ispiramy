"""Pagine statiche informative/legali (Chi Siamo, FAQ, Contatti, Privacy, Termini)."""
import os
import re
from html import escape as html_escape

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logger_config import logger
from app.routes.auth import verify_token
from app.utils.email import send_email
from app.utils.lingue_ui import COOKIE, GIORNI_MEMORIA, lingua_valida
from app.utils.rate_limit import enforce_rate_limit

router = APIRouter()

EMAIL_AMMINISTRAZIONE = os.getenv("ADMIN_EMAIL", "admin@ispiramy.com")
_EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return request.app.state.templates.TemplateResponse("about.html", {"request": request})


@router.get("/come-funziona", response_class=HTMLResponse)
async def come_funziona(request: Request):
    return request.app.state.templates.TemplateResponse("come_funziona.html", {"request": request})


@router.get("/faq", response_class=HTMLResponse)
async def faq(request: Request):
    return request.app.state.templates.TemplateResponse("faq.html", {"request": request})


def _contesto_contatto(request: Request, **extra):
    utente = verify_token(request)
    contesto = {
        "request": request,
        "inviato": request.query_params.get("inviato") == "1",
        "utente": utente,
        "nome_precompilato": "",
        "email_precompilata": "",
        "errore": None,
    }
    if utente:
        contesto["nome_precompilato"] = " ".join(
            parte for parte in (utente.nome or "", utente.cognome or "") if parte
        ).strip()
        contesto["email_precompilata"] = utente.email or ""
    contesto.update(extra)
    return contesto


@router.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    return request.app.state.templates.TemplateResponse(
        "contact.html", _contesto_contatto(request)
    )


@router.post("/contact", response_class=HTMLResponse)
async def invia_contatto(
    request: Request,
    nome: str = Form(""),
    email: str = Form(""),
    tipo: str = Form("lamentela"),
    messaggio: str = Form(""),
):
    """Una lamentela (o un altro messaggio) arriva a admin@ispiramy.com.

    Funziona sia per chi ha fatto accesso sia per chi visita soltanto.
    Non c'è un chatbot: l'AI già presente serve ai tag, alla verifica del
    profilo e alle contestazioni, non a un dialogo di assistenza.
    """
    nome = (nome or "").strip()
    email = (email or "").strip()
    messaggio = (messaggio or "").strip()
    tipo = tipo if tipo in ("lamentela", "domanda") else "lamentela"

    if len(nome) < 2 or not _EMAIL_OK.match(email) or len(messaggio) < 20:
        return request.app.state.templates.TemplateResponse(
            "contact.html",
            _contesto_contatto(
                request,
                errore="Servono nome, un'email valida e un messaggio di almeno 20 caratteri.",
                nome_precompilato=nome,
                email_precompilata=email,
                messaggio_precompilato=messaggio,
                tipo_scelto=tipo,
            ),
            status_code=400,
        )

    enforce_rate_limit(
        request, "contatto", limit=5, window_seconds=3600, extra_key=email,
        message="Hai inviato troppi messaggi. Riprova fra {attesa} secondi o scrivi a "
                + EMAIL_AMMINISTRAZIONE,
    )

    oggetto = "Lamentela da un utente" if tipo == "lamentela" else "Messaggio dal sito"
    corpo = (
        "<p>È arrivato un messaggio dal modulo Contattaci.</p>"
        "<p><strong>Tipo:</strong> " + html_escape(tipo) + "</p>"
        "<p><strong>Nome:</strong> " + html_escape(nome) + "</p>"
        "<p><strong>Email:</strong> " + html_escape(email) + "</p>"
        "<p><strong>Messaggio:</strong></p>"
        "<p>" + html_escape(messaggio).replace("\n", "<br>") + "</p>"
    )
    utente = verify_token(request)
    if utente:
        corpo += f"<p><strong>Account:</strong> id {utente.id}</p>"

    if not send_email(EMAIL_AMMINISTRAZIONE, f"[Ispiramy] {oggetto}", corpo):
        logger.error(f"❌ Modulo contatti: email non partita verso {EMAIL_AMMINISTRAZIONE}")
        return request.app.state.templates.TemplateResponse(
            "contact.html",
            _contesto_contatto(
                request,
                errore=f"Non siamo riusciti a inviare il messaggio. Scrivi direttamente a {EMAIL_AMMINISTRAZIONE}.",
                nome_precompilato=nome,
                email_precompilata=email,
                messaggio_precompilato=messaggio,
                tipo_scelto=tipo,
            ),
            status_code=502,
        )

    logger.info(f"📩 Contatto ({tipo}) inviato a {EMAIL_AMMINISTRAZIONE} da {email}")
    return RedirectResponse("/contact?inviato=1", status_code=303)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return request.app.state.templates.TemplateResponse("privacy.html", {"request": request})


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return request.app.state.templates.TemplateResponse("terms.html", {"request": request})


@router.get("/lingua/{codice}", include_in_schema=False)
async def cambia_lingua(codice: str, request: Request):
    # Cambia la lingua del sito e torna da dove si e' arrivati. La preferenza
    # sta in un cookie e non nel profilo: vale anche per chi non ha un
    # account, ed e' la prima cosa che tocca chi arriva da fuori.
    torna_a = request.headers.get("referer") or "/"

    # Solo indirizzi di casa nostra: il referer arriva da fuori, non si sa mai
    if "://" in torna_a:
        pezzo = torna_a.split("://", 1)[1]
        ospite_nostro = pezzo.split("/", 1)[0] == request.url.netloc
        percorso = "/" + pezzo.split("/", 1)[1] if "/" in pezzo else "/"
        torna_a = percorso if ospite_nostro else "/"

    risposta = RedirectResponse(torna_a, status_code=303)
    scelta = lingua_valida(codice)
    if scelta:
        risposta.set_cookie(COOKIE, scelta, max_age=GIORNI_MEMORIA * 86400,
                            samesite="lax", path="/")
    return risposta
