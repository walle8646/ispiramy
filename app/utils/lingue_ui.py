"""La lingua dell'interfaccia.

Il sito e' nato in italiano e la maggior parte delle pagine lo e' ancora:
qui c'e' l'ossatura per tradurle una per volta, senza dover riscrivere tutto
in un colpo solo. Le parole tradotte stanno in app/traduzioni/<lingua>.json,
e una chiave che manca ricade sull'italiano invece di sparire dalla pagina.

Come si sceglie la lingua, in ordine:
1. quella che la persona ha scelto col selettore (resta in un cookie);
2. quella del browser, se e' fra quelle che sappiamo parlare;
3. italiano.
"""
import json
import os
from functools import lru_cache
from typing import Optional

PREDEFINITA = "it"
COOKIE = "ispiramy_lingua"
GIORNI_MEMORIA = 365

# Le lingue in cui l'interfaccia esiste davvero. Non e' l'elenco delle lingue
# parlate dai consulenti (quello sta in consultants.py): tradurre una pagina
# e' un'altra cosa dal trovare qualcuno che parli spagnolo.
LINGUE_UI = [
    ("it", "Italiano", "🇮🇹"),
    ("en", "English", "🇬🇧"),
]
CODICI_UI = [codice for codice, _, _ in LINGUE_UI]

CARTELLA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "traduzioni")


@lru_cache(maxsize=8)
def _catalogo(lingua: str) -> dict:
    percorso = os.path.join(CARTELLA, f"{lingua}.json")
    try:
        with open(percorso, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def lingua_valida(codice: Optional[str]) -> Optional[str]:
    return codice if codice in CODICI_UI else None


def lingua_del_browser(intestazione: str) -> Optional[str]:
    """La prima lingua gradita dal browser fra quelle che sappiamo parlare.

    L'intestazione e' tipo "en-GB,en;q=0.9,it;q=0.8": basta il pezzo prima
    del trattino, e l'ordine e' gia' quello di preferenza.
    """
    for pezzo in (intestazione or "").split(","):
        codice = pezzo.split(";")[0].strip().lower().split("-")[0]
        if codice in CODICI_UI:
            return codice
    return None


def lingua_di(request) -> str:
    """La lingua da usare per questa richiesta."""
    scelta = lingua_valida(request.cookies.get(COOKIE))
    if scelta:
        return scelta
    return lingua_del_browser(request.headers.get("accept-language", "")) or PREDEFINITA


def traduci(chiave: str, lingua: str) -> str:
    """La parola nella lingua chiesta.

    Se manca si ripiega sull'italiano, e se manca anche quello si mostra la
    chiave: una pagina con una scritta strana si nota e si corregge, una con
    un buco al posto di un pulsante no.
    """
    parola = _catalogo(lingua).get(chiave)
    if parola:
        return parola
    return _catalogo(PREDEFINITA).get(chiave, chiave)


# Le categorie stanno nel database, non nei template: il nome italiano e' la
# chiave, perche' e' quello che il database ha davvero (ed e' unico). Una
# categoria aggiunta dall'amministrazione e non ancora tradotta resta in
# italiano: meglio una parola italiana in mezzo all'inglese che una categoria
# che sparisce dai filtri.
@lru_cache(maxsize=1)
def _categorie() -> dict:
    percorso = os.path.join(CARTELLA, "categorie.json")
    try:
        with open(percorso, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def nome_categoria(nome: Optional[str], lingua: str) -> str:
    """Il nome della categoria nella lingua chiesta.

    In italiano si usa sempre quello del database: e' li' che si cambia.
    """
    if not nome or lingua == PREDEFINITA:
        return nome or ""
    return _categorie().get(nome.strip(), {}).get(lingua) or nome
