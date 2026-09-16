"""Le poche rotte che servono a far installare il sito come app sul telefono.

Il service worker deve stare in cima al sito e non dentro /static/: un file
servito da /static/sw.js puo' occuparsi solo di /static/, quindi non vedrebbe
mai la navigazione fra le pagine. Qui lo serviamo da /sw.js, che e' l'unico
modo per dargli come campo d'azione tutto il sito.
"""
import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

router = APIRouter()

STATICO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


@router.get("/sw.js", include_in_schema=False)
async def service_worker():
    return FileResponse(
        os.path.join(STATICO, "sw.js"),
        media_type="application/javascript",
        headers={
            # Senza questo il browser limita il worker alla cartella da cui arriva
            "Service-Worker-Allowed": "/",
            # Il worker decide cosa sta in cache: se finisse lui in cache, un
            # errore resterebbe sul telefono delle persone per giorni
            "Cache-Control": "no-cache",
        },
    )


def _indirizzo_del_sito(request: Request) -> str:
    """L'origine come la vede chi naviga, https compreso.

    Dietro il proxy di Render la richiesta arriva in http: senza guardare
    l'intestazione inoltrata scriveremmo un indirizzo che non corrisponde a
    quello dell'app installata, e il riconoscimento non funzionerebbe.
    """
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https":
        base = base.replace("http://", "https://", 1)
    return base


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest(request: Request):
    with open(os.path.join(STATICO, "manifest.webmanifest"), encoding="utf-8") as f:
        dati = json.load(f)

    # Dichiarando l'app come "applicazione collegata" di se stessa, Chrome ci
    # lascia chiedere se e' gia' installata su quel telefono (vedi
    # getInstalledRelatedApps in base.html). Senza, dal browser normale non
    # c'e' modo di saperlo e continueremmo a proporre di installarla.
    dati["related_applications"] = [{
        "platform": "webapp",
        "url": _indirizzo_del_sito(request) + "/manifest.webmanifest",
    }]
    dati["prefer_related_applications"] = False

    return JSONResponse(
        dati,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/senza-rete", response_class=HTMLResponse, include_in_schema=False)
async def senza_rete(request: Request):
    """Quello che si vede aprendo l'app senza campo."""
    return request.app.state.templates.TemplateResponse("senza_rete.html", {"request": request})
