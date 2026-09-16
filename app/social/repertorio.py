"""
Clip di repertorio da Pexels per i video social.

Per ogni frase dello script cerca uno spezzone verticale che la illustri, lo
scarica e lo consegna al montatore. Senza PEXELS_API_KEY il modulo si tira
indietro e il video usa le slide brandizzate: meglio un video piu' semplice
che nessun video.

Licenza Pexels: uso commerciale e modifiche permessi, attribuzione non
obbligatoria; le linee guida dell'API la chiedono "dove possibile", percio'
`generate_video_for_draft` riporta i nomi degli autori nel messaggio, da
mettere nella caption.
"""
import os
from pathlib import Path
from typing import Optional

import requests

from app.logger_config import logger

RICERCA = "https://api.pexels.com/v1/videos/search"
# Si scelgono i file piu' leggeri che bastino: le clip 4K di Pexels arrivano a
# centinaia di MB e su Render non c'e' ne' banda ne' memoria da sprecare
LARGHEZZA_MINIMA = 720
PESO_MASSIMO = 60 * 1024 * 1024


def configurato() -> bool:
    """False se manca la chiave: il chiamante ripiega sulle slide."""
    return bool(os.getenv("PEXELS_API_KEY"))


def _file_migliore(video: dict, orizzontale: bool = False) -> Optional[dict]:
    """Il file piu' leggero fra quelli del verso giusto e abbastanza grandi."""
    candidati = []
    for file in video.get("video_files") or []:
        larghezza, altezza = file.get("width") or 0, file.get("height") or 0
        if not file.get("link") or larghezza < LARGHEZZA_MINIMA:
            continue
        if orizzontale:
            if larghezza <= altezza:
                continue  # verticale: ritagliato in 16:9 resterebbe una fessura
        elif altezza <= larghezza:
            continue  # orizzontale: ritagliato in 9:16 perderebbe troppo
        candidati.append((larghezza * altezza, file))
    if not candidati:
        return None
    return min(candidati, key=lambda c: c[0])[1]


def _scarica(url: str, destinazione: Path) -> bool:
    try:
        with requests.get(url, stream=True, timeout=120) as risposta:
            if risposta.status_code != 200:
                logger.warning(f"Repertorio: scaricamento fallito ({risposta.status_code})")
                return False
            scritti = 0
            with open(destinazione, "wb") as uscita:
                for pezzo in risposta.iter_content(chunk_size=1 << 16):
                    scritti += len(pezzo)
                    if scritti > PESO_MASSIMO:
                        logger.warning("Repertorio: clip troppo pesante, scartata")
                        return False
                    uscita.write(pezzo)
        return scritti > 0
    except requests.RequestException as e:
        logger.warning(f"Repertorio: scaricamento non riuscito ({e})")
        return False


def cerca_clip(parole: str, durata_minima: float, cartella: Path, nome: str,
               orientamento: str = "portrait") -> Optional[dict]:
    """Cerca una clip per queste parole chiave.

    L'orientamento e' verticale per i social e orizzontale per il video della
    homepage, che vive dentro un riquadro 16:9.

    Ritorna {"percorso", "autore", "id"} oppure None: un ritorno vuoto non e'
    un errore, vuol dire solo che quella scena si fa con la slide.
    """
    parole = (parole or "").strip()
    if not configurato() or not parole:
        return None
    try:
        risposta = requests.get(
            RICERCA,
            headers={"Authorization": os.getenv("PEXELS_API_KEY")},
            params={"query": parole, "orientation": orientamento, "size": "medium", "per_page": 15},
            timeout=30,
        )
    except requests.RequestException as e:
        logger.warning(f"Repertorio: ricerca '{parole}' non riuscita ({e})")
        return None
    if risposta.status_code != 200:
        logger.warning(f"Repertorio: ricerca '{parole}' rifiutata ({risposta.status_code})")
        return None

    video = [
        v for v in (risposta.json().get("videos") or [])
        if (v.get("duration") or 0) >= max(2.0, durata_minima)
    ]
    for scelto in video[:5]:
        file = _file_migliore(scelto, orizzontale=(orientamento == "landscape"))
        if not file:
            continue
        percorso = cartella / f"{nome}.mp4"
        if _scarica(file["link"], percorso):
            autore = (scelto.get("user") or {}).get("name") or "Pexels"
            logger.info(f"🎞️ Repertorio '{parole}': clip {scelto.get('id')} di {autore}")
            return {"percorso": percorso, "autore": autore, "id": scelto.get("id")}
    logger.info(f"Repertorio: nessuna clip adatta per '{parole}', si usa la slide")
    return None
