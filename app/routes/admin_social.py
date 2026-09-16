"""
Routes admin per la gestione dei contenuti social (bozze, approvazione, pubblicazione).
"""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pydantic import BaseModel
from typing import Optional

import json

from app.database import engine
from app.models import SocialContent, SocialDraft
from app.routes.admin import require_admin
from app.logger_config import logger
from app.social.publisher import PLATFORM_REQUIRES_MEDIA, parse_media_urls
from app.social.tipi import (
    ETICHETTE as ETICHETTE_TIPO,
    PIATTAFORME,
    TIPI,
    piattaforme_per_tipo,
    tipi_ammessi,
    tipo_di,
)

router = APIRouter(prefix="/admin")

ITALY_TZ = ZoneInfo("Europe/Rome")

PLATFORM_LABELS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}


@router.get("/social", response_class=HTMLResponse)
async def admin_social(request: Request):
    """Dashboard social: bozze generate, approvazione e pubblicazione."""
    admin_user = require_admin(request)
    if not admin_user:
        return RedirectResponse("/login", status_code=302)

    with Session(engine) as session:
        contenuti = session.exec(
            select(SocialContent).order_by(SocialContent.created_at.desc()).limit(100)
        ).all()
        uscite = session.exec(
            select(SocialDraft).order_by(SocialDraft.id.desc()).limit(500)
        ).all()

    # Le uscite raggruppate sotto il loro contenuto, in ordine di programmazione
    per_contenuto = {}
    for uscita in uscite:
        per_contenuto.setdefault(uscita.content_id, []).append(uscita)
    for elenco in per_contenuto.values():
        elenco.sort(key=lambda u: (u.scheduled_at or datetime.max, u.id))

    titoli = {c.id: (c.source_title or c.caption_base or "").strip()[:70] for c in contenuti}
    testi = {c.id: _testi_del_contenuto(c) for c in contenuti}

    counts = {}
    for uscita in uscite:
        counts[uscita.status] = counts.get(uscita.status, 0) + 1
    conteggi_tipo = {}
    for contenuto in contenuti:
        conteggi_tipo[contenuto.content_kind] = conteggi_tipo.get(contenuto.content_kind, 0) + 1
    calendario = _voci_calendario(uscite, titoli)

    # Stato account collegati (best effort, non bloccare la pagina se l'API è giù)
    accounts = {}
    try:
        from app.social.publisher import get_connected_accounts
        # Chiamata HTTP sincrona: fuori dall'event loop, altrimenti l'apertura
        # della dashboard blocca tutto il sito finché Post for Me non risponde.
        accounts = await asyncio.to_thread(get_connected_accounts)
    except Exception as e:
        logger.warning(f"Admin social: impossibile leggere account Post for Me: {e}")

    return request.app.state.templates.TemplateResponse("admin/social.html", {
        "request": request,
        "user": admin_user,
        "current_user": admin_user,
        "contenuti": contenuti,
        "uscite": per_contenuto,
        "testi": testi,
        "titoli": titoli,
        "counts": counts,
        "calendario": calendario,
        "conteggi_tipo": conteggi_tipo,
        "etichette_tipo": ETICHETTE_TIPO,
        "elenco_tipi": TIPI,
        "piattaforme_per_tipo": piattaforme_per_tipo,
        "accounts": accounts,
        "platform_labels": PLATFORM_LABELS,
    })


def _testi_del_contenuto(contenuto: SocialContent) -> dict:
    """La versione del testo per ogni social, col testo base come ripiego."""
    try:
        testi = json.loads(contenuto.captions or "{}")
    except json.JSONDecodeError:
        testi = {}
    return {p: (testi.get(p) or contenuto.caption_base or "") for p in PIATTAFORME}


def _voci_calendario(drafts, titoli: Optional[dict] = None) -> list[dict]:
    """Le voci del calendario: cosa e' programmato e cosa e' gia' uscito.

    Tutti gli orari in ora italiana. scheduled_at lo e' gia' (arriva dal campo
    della pagina), published_at invece e' salvato in UTC: mostrarli insieme
    senza convertire farebbe sembrare che un post sia uscito due ore prima di
    quando era programmato.
    """
    voci = []
    for d in drafts:
        if d.status == "published" and d.published_at:
            quando = d.published_at.replace(tzinfo=timezone.utc).astimezone(ITALY_TZ).replace(tzinfo=None)
        elif d.scheduled_at:
            quando = d.scheduled_at
        else:
            continue
        voci.append({
            "id": d.id,
            "contenuto": d.content_id,
            "platform": d.platform,
            "status": d.status,
            "giorno": quando.strftime("%Y-%m-%d"),
            "ora": quando.strftime("%H:%M"),
            "titolo": ((titoli or {}).get(d.content_id) or d.source_title or d.caption or "").strip()[:70],
        })
    return sorted(voci, key=lambda v: (v["giorno"], v["ora"]))


class GenerateRequest(BaseModel):
    limit: int = 3


@router.post("/social/generate")
async def admin_social_generate(data: GenerateRequest, request: Request):
    """Genera nuove bozze dalle top domande community (chiama GPT, può richiedere qualche secondo)."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    try:
        from app.social.content_generator import genera_bozze, salva_contenuti
        limit = max(1, min(data.limit, 10))
        packages, errori = await asyncio.to_thread(genera_bozze, limit)

        if not packages:
            if errori:
                # Prima questo caso veniva scambiato per "non c'e' niente da fare"
                return JSONResponse(
                    {"ok": False, "message": "Generazione non riuscita. " + "; ".join(errori)[:300]},
                    status_code=502,
                )
            return {
                "ok": True,
                "message": "Nessuna domanda nuova da lavorare: tutte le domande "
                           "più seguite hanno già dei contenuti.",
            }

        created = await asyncio.to_thread(salva_contenuti, packages)
        messaggio = f"Creati {created} contenuti da {len(packages)} domande"
        if errori:
            messaggio += f" ({len(errori)} non riuscite: {'; '.join(errori)[:200]})"
        return {"ok": True, "message": messaggio}
    except Exception as e:
        logger.error(f"Admin social: errore generazione: {e}", exc_info=True)
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=500)


class ContenutoUpdate(BaseModel):
    caption_base: Optional[str] = None
    captions: Optional[dict] = None
    media_urls: Optional[str] = None


@router.post("/social/contents/{content_id}")
async def admin_social_update_content(content_id: int, data: ContenutoUpdate, request: Request):
    """Aggiorna il testo (base e per social) e il media del contenuto."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return JSONResponse({"ok": False, "message": "Contenuto non trovato"}, status_code=404)
        if data.caption_base is not None:
            contenuto.caption_base = data.caption_base[:5000]
        if data.captions is not None:
            testi = {p: (t or "")[:5000] for p, t in data.captions.items() if p in PIATTAFORME}
            contenuto.captions = json.dumps(testi, ensure_ascii=False)[:8000]
        if data.media_urls is not None:
            contenuto.media_urls = "\n".join(parse_media_urls(data.media_urls))[:3000] or None
        contenuto.updated_at = datetime.utcnow()
        session.add(contenuto)
        session.commit()
        media = contenuto.media_urls

    if data.media_urls is not None:
        # Il media cambiato vale per tutte le uscite non ancora partite
        from app.social.image_generator import scrivi_media
        scrivi_media(content_id, media or "")
    return {"ok": True, "message": "Contenuto aggiornato"}


@router.post("/social/contents/{content_id}/kind")
async def admin_social_content_kind(content_id: int, request: Request):
    """Cambia il tipo del contenuto (immagini, post video, video completo)."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    stile = (corpo or {}).get("stile")
    if stile not in TIPI:
        return JSONResponse({"ok": False, "message": "Tipo di contenuto non valido"}, status_code=400)

    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return JSONResponse({"ok": False, "message": "Contenuto non trovato"}, status_code=404)

        ammesse = piattaforme_per_tipo(stile)
        uscite = session.exec(select(SocialDraft).where(SocialDraft.content_id == content_id)).all()
        incompatibili = sorted({u.platform for u in uscite if u.platform not in ammesse})
        if incompatibili:
            # Cambiare tipo con un'uscita che non potrebbe piu' partire vorrebbe
            # dire farla fallire piu' tardi, in silenzio
            nomi = ", ".join(PLATFORM_LABELS.get(p, p) for p in incompatibili)
            return JSONResponse({
                "ok": False,
                "message": f"«{ETICHETTE_TIPO[stile]}» non si pubblica su {nomi}: togli prima quelle uscite",
            }, status_code=400)

        contenuto.content_kind = stile
        contenuto.updated_at = datetime.utcnow()
        session.add(contenuto)
        for uscita in uscite:
            uscita.content_kind = stile
            session.add(uscita)
        session.commit()
    logger.info(f"Admin social: contenuto {content_id} spostato in '{stile}'")
    return {"ok": True, "message": f"Spostato in «{ETICHETTE_TIPO[stile]}»"}


@router.post("/social/contents/{content_id}/generate-media")
async def admin_social_content_generate_media(content_id: int, request: Request):
    """Genera il media del contenuto: vale per tutte le sue uscite."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    stile = (corpo or {}).get("stile")
    if stile is not None and stile not in TIPI:
        return JSONResponse({"ok": False, "message": "Tipo di contenuto non valido"}, status_code=400)

    try:
        from app.social.image_generator import generate_media_for_content
        result = await asyncio.to_thread(generate_media_for_content, content_id, True, stile)
        return JSONResponse(result, status_code=200 if result["ok"] else 400)
    except Exception as e:
        logger.error(f"Admin social: errore generazione media contenuto {content_id}: {e}", exc_info=True)
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=500)


class NuovaUscita(BaseModel):
    platform: str
    scheduled_at: Optional[str] = None  # "YYYY-MM-DDTHH:MM" ora italiana


@router.post("/social/contents/{content_id}/publications")
async def admin_social_add_publication(content_id: int, data: NuovaUscita, request: Request):
    """Aggiunge un'uscita del contenuto su un social, con il suo orario.

    È il punto di tutta la struttura: lo stesso video esce su TikTok, sui Reels
    di Instagram e su quelli di Facebook, a orari diversi, senza rigenerarlo.
    """
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return JSONResponse({"ok": False, "message": "Contenuto non trovato"}, status_code=404)
        if data.platform not in piattaforme_per_tipo(contenuto.content_kind):
            return JSONResponse({
                "ok": False,
                "message": f"«{ETICHETTE_TIPO[contenuto.content_kind]}» non si pubblica su "
                           f"{PLATFORM_LABELS.get(data.platform, data.platform)}",
            }, status_code=400)

        quando = None
        if (data.scheduled_at or "").strip():
            try:
                quando = datetime.fromisoformat(data.scheduled_at.strip())
            except ValueError:
                return JSONResponse({"ok": False, "message": "Formato data non valido"}, status_code=400)

        testi = _testi_del_contenuto(contenuto)
        uscita = SocialDraft(
            content_id=content_id,
            platform=data.platform,
            caption=(testi.get(data.platform) or contenuto.caption_base or "")[:5000],
            media_urls=contenuto.media_urls,
            content_kind=contenuto.content_kind,
            source_question_id=contenuto.source_question_id,
            source_title=contenuto.source_title,
            extra_content=contenuto.extra_content,
            scheduled_at=quando,
        )
        session.add(uscita)
        session.commit()
        session.refresh(uscita)
        nuovo_id = uscita.id

    quando_testo = f" per il {quando.strftime('%d/%m alle %H:%M')}" if quando else ""
    return {
        "ok": True,
        "id": nuovo_id,
        "message": f"Uscita su {PLATFORM_LABELS.get(data.platform, data.platform)} aggiunta{quando_testo}",
    }


class DraftUpdateRequest(BaseModel):
    caption: Optional[str] = None
    media_urls: Optional[str] = None
    scheduled_at: Optional[str] = None  # "YYYY-MM-DDTHH:MM" ora italiana, "" per azzerare


@router.post("/social/drafts/{draft_id}")
async def admin_social_update_draft(draft_id: int, data: DraftUpdateRequest, request: Request):
    """Aggiorna testo, media o programmazione di un draft."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return JSONResponse({"ok": False, "message": "Draft non trovato"}, status_code=404)
        if draft.status in ("published", "publishing"):
            return JSONResponse({"ok": False, "message": "Draft già pubblicato/in pubblicazione"}, status_code=400)

        if data.caption is not None:
            draft.caption = data.caption[:5000]
        if data.media_urls is not None:
            # Un URL per riga, qualunque cosa arrivi: ripara anche le bozze in
            # cui gli URL erano finiti incollati da un vecchio salvataggio.
            urls = parse_media_urls(data.media_urls)
            draft.media_urls = "\n".join(urls)[:3000] or None
        if data.scheduled_at is not None:
            if data.scheduled_at.strip():
                try:
                    draft.scheduled_at = datetime.fromisoformat(data.scheduled_at.strip())
                except ValueError:
                    return JSONResponse({"ok": False, "message": "Formato data non valido"}, status_code=400)
            else:
                draft.scheduled_at = None
        draft.updated_at = datetime.utcnow()
        session.add(draft)
        session.commit()
    return {"ok": True, "message": "Draft aggiornato"}


@router.post("/social/drafts/{draft_id}/status")
async def admin_social_draft_status(draft_id: int, request: Request):
    """Cambia stato del draft: body {"action": "approve"|"reject"|"back_to_draft"}."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    body = await request.json()
    action = body.get("action")
    transitions = {
        "approve": ("draft", "failed"),   # da questi stati si può approvare
        "reject": ("draft", "approved"),
        "back_to_draft": ("approved", "rejected", "failed"),
    }
    new_status = {"approve": "approved", "reject": "rejected", "back_to_draft": "draft"}.get(action)
    if not new_status:
        return JSONResponse({"ok": False, "message": "Azione non valida"}, status_code=400)

    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return JSONResponse({"ok": False, "message": "Draft non trovato"}, status_code=404)
        if draft.status not in transitions[action]:
            return JSONResponse({"ok": False, "message": f"Transizione non permessa da '{draft.status}'"}, status_code=400)

        # Approvare un post che non potrà mai partire serve solo a farlo
        # fallire più tardi, magari in silenzio all'ora programmata.
        if (action == "approve" and draft.platform in PLATFORM_REQUIRES_MEDIA
                and not parse_media_urls(draft.media_urls)):
            servono = "un video" if draft.platform == "tiktok" else "le immagini"
            return JSONResponse({
                "ok": False,
                "message": f"Per {PLATFORM_LABELS.get(draft.platform, draft.platform)} servono {servono} prima di approvare",
            }, status_code=400)

        # Uscendo da 'failed' dopo che il post era arrivato al social, il
        # prossimo invio deve essere un post NUOVO: si incrementa il tentativo
        # (che cambia l'external_id) e si dimentica il post precedente.
        # Senza questo il publisher lo riconosceva come "già inviato" e lo
        # riportava in 'failed' con l'errore di prima, per sempre.
        if draft.status == "failed" and draft.postforme_post_id:
            draft.publish_attempt = (draft.publish_attempt or 0) + 1
            draft.postforme_post_id = None
            draft.published_url = None
            logger.info(f"🔁 Social: draft {draft.id} riaperto per il tentativo {draft.publish_attempt}")

        draft.status = new_status
        if action == "approve":
            draft.error = None
        draft.updated_at = datetime.utcnow()
        session.add(draft)
        session.commit()
    return {"ok": True, "message": f"Stato: {new_status}"}


@router.post("/social/drafts/{draft_id}/generate-media")
async def admin_social_generate_media(draft_id: int, request: Request):
    """Genera la grafica brand (Pillow + illustrazione AI) e compila media_urls.

    Carosello su Instagram, immagine singola sulle altre piattaforme.
    """
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    # Lo stile arriva dal bottone premuto; senza, si usa quello della bozza
    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    stile = (corpo or {}).get("stile")
    if stile is not None and stile not in TIPI:
        return JSONResponse({"ok": False, "message": "Tipo di contenuto non valido"}, status_code=400)

    try:
        from app.social.image_generator import generate_media_for_draft
        result = await asyncio.to_thread(generate_media_for_draft, draft_id, True, stile)
        return JSONResponse(result, status_code=200 if result["ok"] else 400)
    except Exception as e:
        logger.error(f"Admin social: errore generazione grafica draft {draft_id}: {e}", exc_info=True)
        return JSONResponse({"ok": False, "message": str(e)[:300]}, status_code=500)


@router.post("/social/drafts/{draft_id}/kind")
async def admin_social_set_kind(draft_id: int, request: Request):
    """Sposta la bozza in un'altra scheda, senza rigenerare il media.

    Il tipo e' una classificazione: un video gia' fatto con le slide deve poter
    stare fra i post video senza rifarlo daccapo (e senza ripagare la voce).
    """
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    stile = (corpo or {}).get("stile")
    if stile not in TIPI:
        return JSONResponse({"ok": False, "message": "Tipo di contenuto non valido"}, status_code=400)

    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return JSONResponse({"ok": False, "message": "Draft non trovato"}, status_code=404)
        if stile not in tipi_ammessi(draft.platform):
            return JSONResponse({
                "ok": False,
                "message": f"{ETICHETTE_TIPO[stile]} non è pubblicabile su {PLATFORM_LABELS.get(draft.platform, draft.platform)}",
            }, status_code=400)
        draft.content_kind = stile
        draft.updated_at = datetime.utcnow()
        session.add(draft)
        session.commit()
    logger.info(f"Admin social: draft {draft_id} spostato in '{stile}'")
    return {"ok": True, "message": f"Spostato in «{ETICHETTE_TIPO[stile]}»"}


@router.post("/social/drafts/{draft_id}/publish")
async def admin_social_publish_now(draft_id: int, request: Request):
    """Pubblica subito un draft approvato (o ritenta un failed dopo verifica idempotente)."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    from app.social.publisher import publish_draft
    result = await asyncio.to_thread(publish_draft, draft_id)
    status_code = 200 if result["ok"] else 400
    return JSONResponse(result, status_code=status_code)


@router.post("/social/refresh-results")
async def admin_social_refresh_results(request: Request):
    """Forza l'aggiornamento degli esiti dei post in pubblicazione."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    from app.social.publisher import check_publishing_results
    await asyncio.to_thread(check_publishing_results)
    return {"ok": True, "message": "Esiti aggiornati"}


@router.delete("/social/drafts/{draft_id}")
async def admin_social_delete_draft(draft_id: int, request: Request):
    """Elimina un draft (solo se non pubblicato)."""
    admin_user = require_admin(request)
    if not admin_user:
        return JSONResponse({"ok": False, "message": "Non autorizzato"}, status_code=403)

    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return JSONResponse({"ok": False, "message": "Draft non trovato"}, status_code=404)
        if draft.status in ("published", "publishing"):
            return JSONResponse({"ok": False, "message": "Non eliminabile: già pubblicato/in pubblicazione"}, status_code=400)
        session.delete(draft)
        session.commit()
    return {"ok": True, "message": "Draft eliminato"}
