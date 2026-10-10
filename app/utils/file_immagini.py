"""Riconosce un'immagine dai byte, non da quello che dice il browser.

Il nome del file e il Content-Type arrivano dal client: un HTML chiamato
foto.jpg con tipo image/jpeg veniva salvato e, in chat, anche servito con
il tipo dichiarato. Qui si guarda solo l'inizio del file.
"""
from typing import Optional, Tuple

# (firma, estensione, tipo) — il webp è un caso a parte, sotto.
_FIRME = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)

# Tipo da mandare al browser per gli allegati che non sono immagini.
# Non si usa mai il Content-Type del client: un .pdf dichiarato text/html
# verrebbe aperto come pagina.
MIME_ALLEGATI = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "csv": "text/csv",
    "zip": "application/zip",
}

ESTENSIONI_IMMAGINE = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}


def tipo_immagine(contenuto: bytes) -> Optional[Tuple[str, str]]:
    """(estensione, mime) se i byte sono jpeg, png, gif o webp, altrimenti None."""
    if not contenuto:
        return None
    if contenuto.startswith(b"RIFF") and len(contenuto) >= 12 and contenuto[8:12] == b"WEBP":
        return ("webp", "image/webp")
    for firma, estensione, mime in _FIRME:
        if contenuto.startswith(firma):
            return (estensione, mime)
    return None
