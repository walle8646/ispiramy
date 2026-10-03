"""Verifica del consulente per categoria, non per tutto l'account.

``is_verified`` resta il lasciapassare per comparire in ricerca e ricevere
prenotazioni. Il badge che vede chi prenota vale solo per le categorie
segnate in ``User.verified_category_ids`` (JSON):

- ``None``: profili verificati prima di questa distinzione. Il badge vale
  per le categorie che hanno dichiarato, non per ogni categoria del sito.
- una lista, anche vuota: elenco esplicito. Lo scrive l'admin, oppure la
  prima verifica automatica del profilo sulle categorie dichiarate allora.
"""
import json
from typing import Iterable


def categorie_dichiarate(user) -> list[int]:
    """Categoria principale e sottocategorie scelte nel profilo, senza doppioni."""
    ids: list[int] = []
    if user.category_id:
        ids.append(int(user.category_id))
    raw = user.selected_subcategories
    if not raw:
        return ids
    try:
        dati = json.loads(raw)
    except (TypeError, ValueError):
        return ids
    if not isinstance(dati, list):
        return ids
    for voce in dati:
        try:
            numero = int(voce)
        except (TypeError, ValueError):
            continue
        if numero not in ids:
            ids.append(numero)
    return ids


def id_categorie_verificate(user) -> set[int]:
    raw = getattr(user, "verified_category_ids", None)
    if raw is None:
        if getattr(user, "is_verified", False):
            return set(categorie_dichiarate(user))
        return set()
    try:
        dati = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(dati, list):
        return set()
    ids = set()
    for voce in dati:
        try:
            ids.add(int(voce))
        except (TypeError, ValueError):
            continue
    return ids


def scrivi_categorie_verificate(user, category_ids: Iterable[int]) -> None:
    puliti: list[int] = []
    for voce in category_ids:
        try:
            numero = int(voce)
        except (TypeError, ValueError):
            continue
        if numero > 0 and numero not in puliti:
            puliti.append(numero)
    user.verified_category_ids = json.dumps(puliti)


def etichette_verificate(session, user) -> list[dict]:
    """Nome e icona delle categorie su cui il badge e' visibile."""
    from sqlmodel import select

    from app.models import Category

    ids = id_categorie_verificate(user)
    if not ids:
        return []
    trovate = list(session.exec(select(Category).where(Category.id.in_(list(ids)))).all())
    ordine = {cid: i for i, cid in enumerate(categorie_dichiarate(user))}
    trovate.sort(key=lambda c: (ordine.get(c.id, 10_000), c.name or ""))
    return [{"id": c.id, "name": c.name, "icon": c.icon or ""} for c in trovate]


def registra_verifica_iniziale(user) -> None:
    """Alla prima verifica il badge vale solo per le categorie dichiarate.

    Un elenco già scritto (dall'admin o da una verifica precedente) non si tocca:
    aggiungere una categoria dopo non la verifica da solo.
    """
    if user.verified_category_ids is not None:
        return
    scrivi_categorie_verificate(user, categorie_dichiarate(user))
