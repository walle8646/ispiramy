"""Pagine statiche informative/legali (Chi Siamo, FAQ, Contatti, Privacy, Termini)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.utils.lingue_ui import COOKIE, GIORNI_MEMORIA, lingua_valida

router = APIRouter()


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return request.app.state.templates.TemplateResponse("about.html", {"request": request})


@router.get("/come-funziona", response_class=HTMLResponse)
async def come_funziona(request: Request):
    return request.app.state.templates.TemplateResponse("come_funziona.html", {"request": request})


@router.get("/faq", response_class=HTMLResponse)
async def faq(request: Request):
    return request.app.state.templates.TemplateResponse("faq.html", {"request": request})


@router.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    return request.app.state.templates.TemplateResponse("contact.html", {"request": request})


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
