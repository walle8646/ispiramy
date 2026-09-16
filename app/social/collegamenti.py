"""I link al sito da mettere nei testi social.

Costruiti da BASE_URL, cosi' passando da staging a produzione cambia una
variabile d'ambiente e non i contenuti gia' scritti. Ogni link porta
l'indicazione del social da cui arriva: senza, nelle statistiche del sito tutto
il traffico social si confonde in un mucchio solo.
"""
import os
from urllib.parse import urlencode

PREDEFINITO = "https://ispiramy.com"


def base() -> str:
    """L'indirizzo del sito, senza barra finale."""
    return (os.getenv("BASE_URL") or PREDEFINITO).rstrip("/")


def _con_provenienza(percorso: str, piattaforma: str = None) -> str:
    indirizzo = f"{base()}{percorso}"
    if not piattaforma:
        return indirizzo
    return f"{indirizzo}?{urlencode({'utm_source': piattaforma, 'utm_medium': 'social'})}"


def link_consulenti(piattaforma: str = None) -> str:
    """Dove mandare chi vuole risolvere il problema di cui parla il post."""
    return _con_provenienza("/consultants", piattaforma)


def link_community(piattaforma: str = None) -> str:
    """Dove mandare chi vuole fare la sua domanda."""
    return _con_provenienza("/community", piattaforma)


def aggiungi_link(testo: str, piattaforma: str = None, invito: str = "Trova il tuo esperto") -> str:
    """Mette in fondo al testo l'invito con il link, se non c'e' gia'.

    Su Instagram e TikTok il link non e' cliccabile, ma scriverlo serve lo
    stesso: chi lo vuole lo cerca, ed e' la stessa riga che su Facebook e
    LinkedIn diventa un collegamento vero.
    """
    testo = (testo or "").strip()
    indirizzo = link_consulenti(piattaforma)
    if base() in testo:
        return testo
    riga = f"👉 {invito}: {indirizzo}"
    return f"{testo}\n\n{riga}".strip()
