"""Le rotte con cui un dispositivo si iscrive (o si disiscrive) alle push."""
from fastapi import APIRouter, HTTPException, Request

from app.routes.auth import get_current_user
from app.utils import notifiche_push

router = APIRouter()


def _utente(request: Request):
    utente = get_current_user(request)
    if not utente:
        raise HTTPException(status_code=401, detail="Non autenticato")
    return utente


@router.get("/api/push/stato")
async def stato(request: Request):
    """Cosa deve sapere la pagina per mostrare l'interruttore giusto."""
    utente = _utente(request)
    return {
        "attivabile": notifiche_push.configurato(),
        "chiave_pubblica": notifiche_push.chiave_pubblica(),
        "dispositivi": notifiche_push.dispositivi(utente.id),
    }


@router.post("/api/push/iscrizione")
async def iscrivi(request: Request):
    utente = _utente(request)
    if not notifiche_push.configurato():
        raise HTTPException(status_code=503, detail="Notifiche push non configurate")

    dati = await request.json()
    salvata = notifiche_push.registra(
        utente.id,
        dati.get("iscrizione") or dati,
        request.headers.get("user-agent"),
    )
    if not salvata:
        raise HTTPException(status_code=400, detail="Iscrizione incompleta")
    return {"ok": True, "dispositivi": notifiche_push.dispositivi(utente.id)}


@router.delete("/api/push/iscrizione")
async def disiscrivi(request: Request):
    utente = _utente(request)
    dati = await request.json()
    notifiche_push.cancella(dati.get("endpoint"))
    return {"ok": True, "dispositivi": notifiche_push.dispositivi(utente.id)}


@router.post("/api/push/prova")
async def prova(request: Request):
    """Manda una notifica a se stessi: serve a capire se tutto funziona."""
    utente = _utente(request)
    inviate = notifiche_push.invia(
        utente.id,
        "Ispiramy",
        "Le notifiche funzionano: ti avviseremo qui.",
        "/profile",
        tag="prova",
    )
    return {"ok": inviate > 0, "dispositivi": inviate}
