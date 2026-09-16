"""I tre tipi di contenuto social, con i nomi usati ovunque.

Il tipo decide tre cose insieme: come si genera il media, in quale scheda
dell'admin compare la bozza e cosa si pubblica. Tenerli qui evita che le
stringhe si sparpaglino fra route, template e generatori.
"""

IMMAGINI = "immagini"            # foto singola o carosello
VIDEO_SLIDE = "video_slide"      # video fatto con le nostre slide
VIDEO_COMPLETO = "video_completo"  # video con le clip di repertorio

TIPI = (IMMAGINI, VIDEO_SLIDE, VIDEO_COMPLETO)

ETICHETTE = {
    IMMAGINI: "Post con immagini",
    VIDEO_SLIDE: "Post video",
    VIDEO_COMPLETO: "Video completo",
}

# TikTok accetta solo video: una bozza nata li' parte gia' come video completo.
# Le altre piattaforme partono dalle immagini, ma possono diventare video.
PREDEFINITO_PER_PIATTAFORMA = {"tiktok": VIDEO_COMPLETO}

# Su TikTok un post di sole immagini non si pubblica
TIPI_AMMESSI_PER_PIATTAFORMA = {"tiktok": (VIDEO_SLIDE, VIDEO_COMPLETO)}


def tipo_di(draft) -> str:
    """Il tipo di una bozza: quello salvato, oppure quello di partenza."""
    tipo = getattr(draft, "content_kind", None)
    if tipo in TIPI:
        return tipo
    return PREDEFINITO_PER_PIATTAFORMA.get(draft.platform, IMMAGINI)


def tipi_ammessi(platform: str) -> tuple:
    """I tipi che ha senso generare per questa piattaforma."""
    return TIPI_AMMESSI_PER_PIATTAFORMA.get(platform, TIPI)
