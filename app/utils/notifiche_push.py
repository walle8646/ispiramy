"""Notifiche push: quelle che arrivano sul telefono anche col sito chiuso.

Come funziona, in breve: il browser della persona chiede al proprio servizio
(Google per Chrome, Apple per Safari) un indirizzo di consegna, e ce lo passa.
Noi lo salviamo e, quando c'e' qualcosa da dire, ci recapitiamo un messaggio
cifrato firmato con la nostra coppia di chiavi VAPID. Il telefono lo riceve
anche se il sito non e' aperto: lo raccoglie il service worker.

Due cose che vale la pena sapere:

- l'indirizzo di consegna scade e cambia da solo. Quando il servizio risponde
  404 o 410 vuol dire "questo non esiste piu'": la riga va cancellata, o
  continueremmo a bussare a vuoto per sempre;
- l'invio parte in un thread separato. Un servizio lento non deve rallentare
  la richiesta della persona che ha appena prenotato una consulenza.
"""
import json
import os
import threading
from datetime import datetime
from urllib.parse import urlparse

from sqlmodel import Session, select

from app.database import engine
from app.logger_config import logger
from app.models import PushSubscription

# Le push valgono qualche secondo in piu' di attesa, non minuti
ATTESA = 10


def chiave_pubblica() -> str:
    """La chiave che il browser usa per iscriversi. Puo' stare in chiaro."""
    return os.getenv("VAPID_PUBLIC_KEY", "")


def _chiave_privata() -> str:
    return os.getenv("VAPID_PRIVATE_KEY", "")


def _mittente() -> str:
    """Chi contattare se il servizio di consegna ha problemi con i nostri invii.

    pywebpush vuole un mailto: o un https:. Se nelle variabili c'è solo
    l'indirizzo email, si aggiunge mailto:.
    """
    soggetto = (os.getenv("VAPID_SUBJECT") or "mailto:admin@ispiramy.com").strip()
    if soggetto.startswith("mailto:") or soggetto.startswith("https:"):
        return soggetto
    if "@" in soggetto and "://" not in soggetto:
        return "mailto:" + soggetto
    return soggetto


def configurato() -> bool:
    """Senza le chiavi le push non esistono: il sito funziona lo stesso."""
    return bool(chiave_pubblica() and _chiave_privata())


def registra(user_id: int, iscrizione: dict, dispositivo: str = None) -> bool:
    """Salva (o aggiorna) l'indirizzo di consegna di un dispositivo."""
    endpoint = (iscrizione or {}).get("endpoint")
    chiavi = (iscrizione or {}).get("keys") or {}
    if not endpoint or not chiavi.get("p256dh") or not chiavi.get("auth"):
        return False

    with Session(engine) as session:
        esistente = session.exec(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        ).first()
        if esistente:
            # Stesso dispositivo, magari di un altro account: l'indirizzo e' suo
            esistente.user_id = user_id
            esistente.p256dh = chiavi["p256dh"]
            esistente.auth = chiavi["auth"]
            esistente.dispositivo = (dispositivo or "")[:300] or esistente.dispositivo
            session.add(esistente)
        else:
            session.add(PushSubscription(
                user_id=user_id,
                endpoint=endpoint[:800],
                p256dh=chiavi["p256dh"][:200],
                auth=chiavi["auth"][:100],
                dispositivo=(dispositivo or "")[:300] or None,
            ))
        session.commit()
    return True


def cancella(endpoint: str) -> bool:
    """Toglie un dispositivo: lo chiede la persona, o lo dice il servizio."""
    if not endpoint:
        return False
    with Session(engine) as session:
        riga = session.exec(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint)
        ).first()
        if not riga:
            return False
        session.delete(riga)
        session.commit()
    return True


def dispositivi(user_id: int) -> int:
    """Quanti dispositivi riceverebbero una notifica adesso."""
    with Session(engine) as session:
        return len(session.exec(
            select(PushSubscription).where(PushSubscription.user_id == user_id)
        ).all())


def _host_consegna(endpoint: str) -> str:
    """Solo l'host: l'indirizzo completo è un segreto di consegna."""
    try:
        return urlparse(endpoint or "").netloc or "sconosciuto"
    except ValueError:
        return "sconosciuto"


def _consegna(riga_id: int, endpoint: str, p256dh: str, auth: str, carico: str, user_id: int = None) -> None:
    from pywebpush import WebPushException, webpush

    host = _host_consegna(endpoint)
    try:
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=carico,
            vapid_private_key=_chiave_privata(),
            vapid_claims={"sub": _mittente()},
            timeout=ATTESA,
        )
    except WebPushException as errore:
        stato = getattr(errore.response, "status_code", None)
        if stato in (404, 410):
            # Il dispositivo non esiste piu': smettiamo di bussare
            cancella(endpoint)
            logger.warning(
                f"PUSH FALLITA: iscrizione scaduta status={stato} utente={user_id} host={host}"
            )
        else:
            logger.error(
                f"PUSH FALLITA: status={stato} utente={user_id} host={host} "
                f"errore={type(errore).__name__}"
            )
        return
    except Exception as errore:  # rete, DNS, chiavi sbagliate
        logger.error(
            f"PUSH FALLITA: utente={user_id} host={host} errore={type(errore).__name__}"
        )
        return

    with Session(engine) as session:
        riga = session.get(PushSubscription, riga_id)
        if riga:
            riga.last_used_at = datetime.utcnow()
            session.add(riga)
            session.commit()


def invia(user_id: int, titolo: str, testo: str, url: str = "/", tag: str = None) -> int:
    """Manda una notifica a tutti i dispositivi della persona.

    Torna a quanti dispositivi e' stata affidata: l'esito vero arriva dopo, in
    un thread, perche' chi ha fatto l'azione non deve aspettare i server di
    Google o Apple.
    """
    if not configurato():
        mancano = []
        if not chiave_pubblica():
            mancano.append("VAPID_PUBLIC_KEY")
        if not _chiave_privata():
            mancano.append("VAPID_PRIVATE_KEY")
        logger.error(
            "PUSH NON INVIATA: chiavi mancanti (" + ", ".join(mancano) + f") utente={user_id}"
        )
        return 0

    with Session(engine) as session:
        righe = session.exec(
            select(PushSubscription).where(PushSubscription.user_id == user_id)
        ).all()
        destinatari = [(r.id, r.endpoint, r.p256dh, r.auth) for r in righe]

    if not destinatari:
        logger.warning(f"PUSH NON INVIATA: nessun dispositivo iscritto utente={user_id}")
        return 0

    carico = json.dumps({
        "titolo": titolo,
        "testo": testo,
        "url": url or "/",
        # Stesso tag = la notifica nuova sostituisce la vecchia invece di
        # accumulare dieci righe uguali nella tendina del telefono
        "tag": tag or "ispiramy",
    })

    for riga_id, endpoint, p256dh, auth in destinatari:
        threading.Thread(
            target=_consegna,
            args=(riga_id, endpoint, p256dh, auth, carico, user_id),
            daemon=True,
        ).start()

    return len(destinatari)
