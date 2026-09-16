"""
Publisher: pubblica i SocialDraft approvati via Post for Me (postforme.dev).

Idempotenza: ogni draft viene inviato con external_id univoco ("ispiramy-draft-{id}").
Prima di creare un post si verifica che non esista già su Post for Me — lezione
imparata: Facebook può rispondere con errore ("reduce the amount of data") anche
quando il post è uscito, quindi MAI ritentare alla cieca.
"""
import os
import re
import json
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.database import engine
from app.models import SocialDraft
from app.logger_config import logger

API_BASE = "https://api.postforme.dev/v1"

# Requisiti media per piattaforma
PLATFORM_REQUIRES_MEDIA = {"instagram", "tiktok"}


def _api(method: str, path: str, body: Optional[dict] = None) -> dict:
    api_key = os.getenv("POSTFORME_API_KEY")
    if not api_key:
        raise RuntimeError("POSTFORME_API_KEY non configurata")
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body else None,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_connected_accounts() -> dict[str, dict]:
    """Ritorna {platform: account} per gli account collegati su Post for Me."""
    accounts = {}
    for a in _api("GET", "/social-accounts").get("data", []):
        if a.get("status") == "connected":
            accounts[a["platform"]] = a
    return accounts


# Separatori ammessi fra gli URL dei media. L'ultimo caso, "(?=https?://)",
# recupera le bozze in cui gli URL sono finiti incollati in un'unica stringa:
# succedeva a ogni salvataggio dalla dashboard, perche' il campo era un <input>
# a riga singola che eliminava gli a-capo.
_MEDIA_SPLIT = re.compile(r"[\s,;]+|(?=https?://)")


def parse_media_urls(raw: Optional[str]) -> list[str]:
    """Estrae gli URL dei media da un testo libero, nell'ordine in cui compaiono."""
    if not raw:
        return []
    return [u for u in (p.strip() for p in _MEDIA_SPLIT.split(raw)) if u]


# Formati che Instagram rifiuta per le immagini: accetta solo JPEG, e il
# rifiuto arriva come un 400 senza spiegazione. Meglio bloccarlo qui, dove si
# puo' dire cosa fare, che farselo dire dalla piattaforma.
_ESTENSIONI_RIFIUTATE_INSTAGRAM = (".png", ".webp", ".gif", ".bmp", ".tiff", ".heic", ".avif")


def _formati_non_accettati(platform: str, media: list[str]) -> list[str]:
    """Gli URL che la piattaforma rifiutera' di sicuro per il formato."""
    if platform != "instagram":
        return []
    cattivi = []
    for url in media:
        percorso = urllib.parse.urlparse(url).path.lower()
        if percorso.endswith(_ESTENSIONI_RIFIUTATE_INSTAGRAM):
            cattivi.append(percorso.rsplit("/", 1)[-1])
    return cattivi


def _video_generato_da_noi(url: str) -> bool:
    """I video prodotti da video_generator: voce sintetica, quindi contenuto AI.

    I piu' vecchi stanno sotto social/draft-N, quelli nuovi sotto
    social/content-N, da quando il media appartiene al contenuto e non alla
    singola uscita.
    """
    percorso = urllib.parse.urlparse(url).path
    nostro = "/social/draft-" in percorso or "/social/content-" in percorso
    return nostro and percorso.endswith("-video.mp4")


ESTENSIONI_VIDEO = (".mp4", ".mov", ".webm")


def _sono_video(media: list[str]) -> bool:
    """True se il post porta un video (e non foto o caroselli)."""
    return bool(media) and all(
        urllib.parse.urlparse(u).path.lower().endswith(ESTENSIONI_VIDEO) for u in media
    )


def _configurazione_piattaforma(platform: str, media: list[str]) -> Optional[dict]:
    """Le impostazioni specifiche della piattaforma da mandare a Post for Me.

    TikTok chiede due dichiarazioni:
    - contenuto generato con l'AI: i nostri video hanno la voce di ElevenLabs,
      quindi si dichiarano; un video caricato a mano (girato da una persona) no;
    - promozione del proprio marchio: ogni post chiude con l'invito a cercare
      un esperto su Ispiramy, e un contenuto promozionale non dichiarato
      TikTok puo' rimuoverlo.
    """
    if platform == "tiktok":
        return {
            "privacy_status": "public",
            "allow_comment": True,
            "is_ai_generated": any(_video_generato_da_noi(u) for u in media),
            "disclose_your_brand": True,
        }

    # I nostri video sono verticali 9:16 da 15-30 secondi: sono Reel. Senza
    # dirlo, la collocazione la sceglie Post for Me con un valore non
    # documentato, e un verticale finito nel diario normale rende molto meno.
    # Le foto e i caroselli restano nel diario.
    if platform in ("facebook", "instagram") and _sono_video(media):
        configurazione = {"placement": "reels"}
        if platform == "instagram":
            # Il Reel si vede anche nel feed del profilo, non solo nella scheda Reels
            configurazione["share_to_feed"] = True
        return configurazione
    return None


def _external_id(draft: SocialDraft) -> str:
    """ID stabile per un tentativo di pubblicazione.

    Resta lo stesso finche' il tentativo e' in corso (e' cio' che impedisce i
    doppioni se Post for Me risponde con errore a post gia' creato). Cambia solo
    quando un amministratore ritenta esplicitamente un post fallito sul social.
    Il tentativo 0 mantiene il formato originale, cosi' i post gia' inviati
    restano riconoscibili.
    """
    attempt = getattr(draft, "publish_attempt", 0) or 0
    if attempt:
        return f"ispiramy-draft-{draft.id}-r{attempt}"
    return f"ispiramy-draft-{draft.id}"


def _find_existing_post(external_id: str) -> Optional[dict]:
    """Cerca su Post for Me un post già creato con questo external_id (idempotenza)."""
    try:
        q = urllib.parse.urlencode({"external_id": external_id})
        data = _api("GET", f"/social-posts?{q}").get("data", [])
        return data[0] if data else None
    except Exception as e:
        logger.warning(f"Social publisher: check idempotenza fallito per {external_id}: {e}")
        return None  # in dubbio, il chiamante deciderà (non ritenta se il draft è già 'publishing')


def publish_draft(draft_id: int) -> dict:
    """Pubblica (o programma) un singolo draft. Ritorna {ok, message}."""
    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return {"ok": False, "message": "Draft non trovato"}
        if draft.status not in ("approved", "publishing", "failed"):
            return {"ok": False, "message": f"Stato '{draft.status}' non pubblicabile (serve 'approved')"}

        # Vincoli piattaforma
        media = parse_media_urls(draft.media_urls)
        if draft.platform in PLATFORM_REQUIRES_MEDIA and not media:
            return {"ok": False, "message": f"{draft.platform} richiede almeno un media (aggiungi URL immagine/video)"}
        non_adatti = _formati_non_accettati(draft.platform, media)
        if non_adatti:
            return {"ok": False, "message": (
                f"Instagram accetta solo immagini JPEG: {', '.join(non_adatti)}. "
                "Rigenera la grafica e riprova."
            )}

        # Idempotenza: se già inviato (o risulta su Post for Me), non ricreare
        ext_id = _external_id(draft)
        existing = None
        if draft.postforme_post_id:
            existing = {"id": draft.postforme_post_id}
        else:
            existing = _find_existing_post(ext_id)
        if existing:
            draft.postforme_post_id = existing["id"]
            draft.status = "publishing"
            draft.updated_at = datetime.utcnow()
            session.add(draft)
            session.commit()
            check_publishing_results()
            return {"ok": True, "message": "Post già inviato in precedenza: aggiornato lo stato invece di duplicare"}

        accounts = get_connected_accounts()
        account = accounts.get(draft.platform)
        if not account:
            return {"ok": False, "message": f"Nessun account '{draft.platform}' collegato su Post for Me"}

        payload = {
            "caption": draft.caption,
            "social_accounts": [account["id"]],
            "external_id": ext_id,
        }
        if media:
            payload["media"] = [{"url": u} for u in media]
        configurazione = _configurazione_piattaforma(draft.platform, media)
        if configurazione:
            payload["platform_configurations"] = {draft.platform: configurazione}

        try:
            result = _api("POST", "/social-posts", payload)
        except Exception as e:
            draft.error = f"Errore invio a Post for Me: {e}"[:2000]
            draft.status = "failed"
            draft.updated_at = datetime.utcnow()
            session.add(draft)
            session.commit()
            logger.error(f"Social publisher: invio draft {draft.id} fallito: {e}")
            return {"ok": False, "message": draft.error}

        draft.postforme_post_id = result.get("id")
        draft.status = "publishing"
        draft.error = None
        draft.updated_at = datetime.utcnow()
        session.add(draft)
        session.commit()
        logger.info(f"📤 Social publisher: draft {draft.id} ({draft.platform}) inviato -> {draft.postforme_post_id}")
        return {"ok": True, "message": f"Inviato a {draft.platform} (post {draft.postforme_post_id})"}


def _testo_errore(risultato: dict) -> str:
    """Il motivo del fallimento, il piu' completo possibile.

    Post for Me a volte restituisce solo "Request failed with status code 400":
    il perche' vero (formato dell'immagine, proporzioni, account) sta nel resto
    del risultato. Tenere solo il campo `error` lasciava in dashboard un
    messaggio che non dice niente e non permette di correggere il post.
    """
    errore = risultato.get("error")
    testo = errore if isinstance(errore, str) else json.dumps(errore, ensure_ascii=False)
    if len(testo) < 200:
        resto = {
            k: v for k, v in risultato.items()
            if k != "error" and v not in (None, "", [], {})
        }
        if resto:
            testo = f"{testo} — dettagli: {json.dumps(resto, ensure_ascii=False)}"
    return testo[:2000]


def check_publishing_results() -> None:
    """Aggiorna lo stato dei draft in 'publishing' leggendo i risultati da Post for Me."""
    with Session(engine) as session:
        publishing = session.exec(
            select(SocialDraft).where(SocialDraft.status == "publishing")
        ).all()
        for draft in publishing:
            if not draft.postforme_post_id:
                continue
            try:
                q = urllib.parse.urlencode({"post_id": draft.postforme_post_id})
                results = _api("GET", f"/social-post-results?{q}").get("data", [])
            except Exception as e:
                logger.warning(f"Social publisher: lettura risultato draft {draft.id} fallita: {e}")
                continue
            if not results:
                continue  # ancora in elaborazione
            r = results[0]
            if r.get("success"):
                details = r.get("details") or {}
                draft.status = "published"
                draft.published_url = details.get("url") or details.get("post_url")
                draft.published_at = datetime.utcnow()
                draft.error = None
                logger.info(f"✅ Social publisher: draft {draft.id} pubblicato su {draft.platform}")
            else:
                draft.status = "failed"
                draft.error = _testo_errore(r)
                logger.error(f"❌ Social publisher: draft {draft.id} fallito: {draft.error}")
            draft.updated_at = datetime.utcnow()
            session.add(draft)
        session.commit()


def _segna_fallito(draft_id: int, messaggio: str) -> None:
    """Porta in 'failed' un post programmato che non puo' partire.

    Senza questo, un post con un requisito mancante (es. Instagram senza
    immagini, account non collegato) restava 'approved' per sempre: il job lo
    ritentava ogni 5 minuti e in dashboard non compariva alcun errore.
    """
    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft or draft.status != "approved":
            return
        draft.status = "failed"
        draft.error = f"Programmazione non eseguita: {messaggio}"[:2000]
        draft.updated_at = datetime.utcnow()
        session.add(draft)
        session.commit()
    logger.warning(f"Social publisher: draft {draft_id} programmato non pubblicabile: {messaggio}")


def process_social_queue() -> None:
    """Job schedulato: pubblica i draft approvati la cui ora è arrivata e aggiorna gli esiti.

    NB: i draft 'failed' NON vengono ritentati automaticamente (rischio duplicati,
    vedi falsi negativi di Facebook) — si ritenta manualmente dalla dashboard admin.
    """
    from zoneinfo import ZoneInfo
    now_italy = datetime.now(ZoneInfo("Europe/Rome")).replace(tzinfo=None)

    try:
        with Session(engine) as session:
            due = session.exec(
                select(SocialDraft)
                .where(SocialDraft.status == "approved")
                .where(SocialDraft.scheduled_at != None)  # noqa: E711
                .where(SocialDraft.scheduled_at <= now_italy)
            ).all()
            due_ids = [d.id for d in due]
        for draft_id in due_ids:
            esito = publish_draft(draft_id)
            if not esito.get("ok"):
                _segna_fallito(draft_id, esito.get("message", "Pubblicazione non riuscita"))
        check_publishing_results()
    except Exception as e:
        logger.error(f"Social publisher: errore nel job process_social_queue: {e}", exc_info=True)
