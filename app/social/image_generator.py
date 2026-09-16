"""
Generatore grafica caroselli per i social draft (Fase 2A della pipeline).

Da un SocialDraft Instagram (con hook + carousel_slides in extra_content) produce
le immagini del carosello 1080x1350 in stile brand Ispiramy:
- Copertina: hook grande + illustrazione AI (gpt-image-1, fallback flat design)
- Slide 1..N: testo impaginato su template verde con numerazione
Le immagini vengono caricate su S3 (bucket pubblico ispiramy-images) e gli URL
salvati in draft.media_urls, pronti per la pubblicazione.
"""
import io
import os
import json
import base64
from datetime import datetime
from pathlib import Path
from typing import Optional

import boto3
from PIL import Image, ImageDraw, ImageFont
from sqlmodel import Session, select

from app.database import engine
from app.models import SocialContent, SocialDraft
from app.logger_config import logger

# Canvas Instagram portrait 4:5
W, H = 1080, 1350

# Palette brand (verde bosco)
GREEN_DARK = (27, 94, 32)      # #1b5e20
GREEN = (46, 125, 50)          # #2e7d32
GREEN_MID = (67, 160, 71)      # #43a047
GREEN_LIGHT = (102, 187, 106)  # #66bb6a
GREEN_BG = (232, 245, 233)     # #e8f5e9
WHITE = (255, 255, 255)
TEXT_DARK = (27, 58, 36)       # #1b3a24

FONTS_DIR = Path(__file__).parent.parent / "static" / "fonts"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS_DIR / name), size)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Va a capo per parola per stare dentro max_width."""
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _draw_wrapped(draw, text, font, max_width, x, y, fill, line_spacing=1.25, align_center=False):
    """Disegna testo multilinea; ritorna la y finale."""
    lines = _wrap_text(draw, text, font, max_width)
    line_h = int(font.size * line_spacing)
    for line in lines:
        lx = x
        if align_center:
            lx = x + (max_width - draw.textlength(line, font=font)) / 2
        draw.text((lx, y), line, font=font, fill=fill)
        y += line_h
    return y


def _decorations(img: Image.Image):
    """Cerchi decorativi soft agli angoli (stesso stile del call-background)."""
    for cx, cy, r, color, alpha in [
        (80, 90, 150, GREEN_LIGHT, 26),
        (W - 60, H - 160, 210, GREEN, 22),
        (W - 110, 130, 90, GREEN_MID, 30),
        (60, H - 90, 110, GREEN_MID, 22),
    ]:
        overlay = Image.new("RGBA", (r * 2, r * 2), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.ellipse([0, 0, r * 2, r * 2], fill=color + (alpha,))
        img.paste(overlay, (cx - r, cy - r), overlay)


def _footer(draw: ImageDraw.ImageDraw, dark_bg: bool = False):
    """Footer con wordmark ispiramy.com."""
    font = _font("Poppins-SemiBold.ttf", 34)
    text = "ispiramy.com"
    tw = draw.textlength(text, font=font)
    color = WHITE if dark_bg else GREEN
    draw.text(((W - tw) / 2, H - 92), text, font=font, fill=color)


def _logo(size: int = 96) -> Optional[Image.Image]:
    """Logo PNG bianco (renderizzato da S3 per le email, riusato qui). Cache locale."""
    try:
        import urllib.request
        cache = Path("/tmp/ispiramy-logo-email.png")
        if not cache.exists():
            urllib.request.urlretrieve(
                "https://ispiramy-images.s3.eu-north-1.amazonaws.com/logo-email.png", cache
            )
        logo = Image.open(cache).convert("RGBA")
        return logo.resize((size, size), Image.LANCZOS)
    except Exception as e:
        logger.warning(f"Image generator: logo non disponibile: {e}")
        return None


def _ai_illustration(topic: str) -> Optional[Image.Image]:
    """Illustrazione di copertina via gpt-image-1. Ritorna None se fallisce (fallback flat)."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        prompt = (
            "Flat vector illustration, modern minimal style, forest green palette "
            "(#2e7d32, #66bb6a, #e8f5e9 background), no text, no words, no letters. "
            f"Subject: {topic}. Friendly, professional, clean composition, centered subject."
        )
        result = client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            quality="medium",
            n=1,
        )
        img_b64 = result.data[0].b64_json
        return Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("RGBA")
    except Exception as e:
        logger.warning(f"Image generator: illustrazione AI fallita ({e}), uso design flat.")
        return None


def _cover(hook: str, topic: str, use_ai: bool = True, swipe: bool = True) -> Image.Image:
    """Slide di copertina: illustrazione (AI o flat) + hook grande.

    Con swipe=False l'immagine e' pensata per stare da sola (Facebook,
    LinkedIn): al posto di "Scorri" ci va la chiamata al sito, perche' non
    c'e' nessuna seconda slide da scorrere.
    """
    img = Image.new("RGB", (W, H), GREEN_BG)
    draw = ImageDraw.Draw(img)
    _decorations(img)

    illustration = _ai_illustration(topic) if use_ai else None
    y_text = 160
    if illustration:
        ill_size = 620
        illustration = illustration.resize((ill_size, ill_size), Image.LANCZOS)
        # maschera circolare per integrarla nel template
        mask = Image.new("L", (ill_size, ill_size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, ill_size, ill_size], fill=255)
        img.paste(illustration, ((W - ill_size) // 2, 130), mask)
        y_text = 130 + ill_size + 70
    else:
        # design flat: blocco verde pieno in alto con logo
        draw.rounded_rectangle([70, 130, W - 70, 620], radius=36, fill=GREEN)
        logo = _logo(150)
        if logo:
            img.paste(logo, ((W - 150) // 2, 250), logo)
        badge_font = _font("Poppins-SemiBold.ttf", 34)
        badge = "IL CONSIGLIO DI ISPIRAMY"
        bw = draw.textlength(badge, font=badge_font)
        draw.text(((W - bw) / 2, 460), badge, font=badge_font, fill=WHITE)
        y_text = 700

    hook_font = _font("Poppins-Bold.ttf", 74)
    # riduci il font finché il testo sta in 4 righe
    while len(_wrap_text(draw, hook, hook_font, W - 160)) > 4 and hook_font.size > 44:
        hook_font = _font("Poppins-Bold.ttf", hook_font.size - 6)
    _draw_wrapped(draw, hook, hook_font, W - 160, 80, y_text, TEXT_DARK, align_center=True)

    cta_font = _font("Poppins-SemiBold.ttf", 36)
    # » al posto di →: Poppins non ha il glifo freccia
    cta = "Scorri  »" if swipe else "Trova il tuo esperto  »"
    sw = draw.textlength(cta, font=cta_font)
    draw.rounded_rectangle([(W - sw) / 2 - 34, H - 210, (W + sw) / 2 + 34, H - 130], radius=40, fill=GREEN)
    draw.text(((W - sw) / 2, H - 196), cta, font=cta_font, fill=WHITE)

    _footer(draw)
    return img


def _slide(text: str, index: int, total: int, is_last: bool) -> Image.Image:
    """Slide interna: numero grande + testo. L'ultima è la CTA su fondo verde."""
    if is_last:
        img = Image.new("RGB", (W, H), GREEN)
        draw = ImageDraw.Draw(img)
        logo = _logo(140)
        if logo:
            img.paste(logo, ((W - 140) // 2, 200), logo)
        else:
            # fallback: wordmark testuale se il logo non è raggiungibile
            wm_font = _font("Poppins-Bold.ttf", 56)
            wm = "Ispiramy"
            draw.text(((W - draw.textlength(wm, font=wm_font)) / 2, 230), wm, font=wm_font, fill=WHITE)
        font = _font("Poppins-Bold.ttf", 62)
        while len(_wrap_text(draw, text, font, W - 200)) > 6 and font.size > 40:
            font = _font("Poppins-Bold.ttf", font.size - 4)
        _draw_wrapped(draw, text, font, W - 200, 100, 430, WHITE, align_center=True)
        btn_font = _font("Poppins-SemiBold.ttf", 40)
        btn = "Trova il tuo esperto su ispiramy.com"
        while draw.textlength(btn, font=btn_font) > W - 260 and btn_font.size > 28:
            btn_font = _font("Poppins-SemiBold.ttf", btn_font.size - 2)
        bw = draw.textlength(btn, font=btn_font)
        draw.rounded_rectangle([(W - bw) / 2 - 40, H - 330, (W + bw) / 2 + 40, H - 220], radius=54, fill=WHITE)
        draw.text(((W - bw) / 2, H - 305), btn, font=btn_font, fill=GREEN_DARK)
        return img

    img = Image.new("RGB", (W, H), GREEN_BG)
    draw = ImageDraw.Draw(img)
    _decorations(img)

    num_font = _font("Poppins-Bold.ttf", 140)
    draw.text((90, 110), f"{index}", font=num_font, fill=GREEN_MID)
    draw.line([100, 300, 240, 300], fill=GREEN_MID, width=8)

    text_font = _font("Poppins-SemiBold.ttf", 56)
    while len(_wrap_text(draw, text, text_font, W - 180)) > 8 and text_font.size > 38:
        text_font = _font("Poppins-SemiBold.ttf", text_font.size - 4)
    # centra verticalmente il blocco di testo nello spazio sotto il numero
    n_lines = len(_wrap_text(draw, text, text_font, W - 180))
    block_h = n_lines * int(text_font.size * 1.25)
    y_start = max(400, (H - block_h) // 2 - 60)
    _draw_wrapped(draw, text, text_font, W - 180, 90, y_start, TEXT_DARK)

    prog_font = _font("Poppins-Regular.ttf", 32)
    draw.text((90, H - 100), f"{index}/{total}", font=prog_font, fill=GREEN)
    _footer(draw)
    return img


def _carica_su_s3(dati: bytes, key: str, content_type: str) -> str:
    """Carica un file sul bucket pubblico e ritorna l'URL da mettere nel post."""
    bucket = os.getenv("S3_BUCKET_NAME", "ispiramy-images")
    region = os.getenv("AWS_REGION", "eu-north-1")
    client = boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=region,
    )
    client.put_object(
        Bucket=bucket, Key=key, Body=dati,
        ContentType=content_type, CacheControl="public, max-age=604800",
    )
    # In locale (MinIO) l'URL AWS non risolve: S3_PUBLIC_BASE_URL permette di
    # puntare all'endpoint raggiungibile dal browser (es. http://localhost:9000/ispiramy-images)
    public_base = os.getenv("S3_PUBLIC_BASE_URL")
    if public_base:
        return f"{public_base.rstrip('/')}/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def _upload_immagine(img: Image.Image, key: str) -> str:
    """Carica l'immagine su S3 in JPEG e ritorna l'URL pubblico.

    JPEG e non PNG: l'API di pubblicazione di Instagram accetta solo immagini
    JPEG, e con i PNG il container viene rifiutato con un 400 secco ("Request
    failed with status code 400"), senza dire perche'.
    """
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92, optimize=True, progressive=False)
    return _carica_su_s3(buf.getvalue(), key, "image/jpeg")


def _testo_in_evidenza(contenuto: SocialContent) -> str:
    """Il testo da mettere in grande sull'immagine.

    L'hook se c'e', altrimenti la prima slide, altrimenti il titolo della
    domanda, altrimenti la prima frase del testo senza hashtag e senza link.
    """
    try:
        extra = json.loads(contenuto.extra_content or "{}")
    except json.JSONDecodeError:
        extra = {}

    hook = (extra.get("hook") or "").strip()
    if hook:
        return hook
    slide = [s for s in (extra.get("carousel_slides") or []) if isinstance(s, str) and s.strip()]
    if slide:
        return slide[0].strip()
    if (contenuto.source_title or "").strip():
        return contenuto.source_title.strip()

    testo = " ".join(
        p for p in (contenuto.caption_base or "").replace("\n", " ").split()
        if not p.startswith("#") and not p.startswith("http") and p != "👉"
    )
    for fine in (". ", "! ", "? "):
        if fine in testo:
            testo = testo.split(fine)[0] + fine.strip()
            break
    return testo[:120].strip()


def scrivi_media(content_id: int, urls) -> None:
    """Salva il media sul contenuto e lo passa alle uscite non ancora partite.

    Le uscite gia' pubblicate tengono il media con cui sono uscite: e' la loro
    storia, e sovrascriverlo direbbe il falso su cosa e' stato pubblicato.
    """
    testo = "\n".join(urls) if isinstance(urls, (list, tuple)) else urls
    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return
        contenuto.media_urls = testo
        contenuto.updated_at = datetime.utcnow()
        session.add(contenuto)
        for uscita in session.exec(select(SocialDraft).where(SocialDraft.content_id == content_id)).all():
            if uscita.status in ("published", "publishing"):
                continue
            uscita.media_urls = testo
            uscita.updated_at = datetime.utcnow()
            session.add(uscita)
        session.commit()


def generate_image_for_content(content_id: int, use_ai_cover: bool = True) -> dict:
    """Genera una singola immagine brand per il contenuto.

    Senza slide da scorrere il post e' un testo con un'immagine a corredo:
    niente carosello, e al posto di "Scorri" la chiamata al sito.
    """
    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return {"ok": False, "message": "Contenuto non trovato"}
        hook = _testo_in_evidenza(contenuto)
        titolo = contenuto.source_title

    if not hook:
        return {"ok": False, "message": "Questo contenuto non ha un testo da mettere sull'immagine"}

    logger.info(f"🖼️ Genero immagine singola per il contenuto {content_id}...")
    img = _cover(hook, titolo or hook, use_ai=use_ai_cover, swipe=False)
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    url = _upload_immagine(img, f"social/content-{content_id}/{stamp}-post.jpg")

    scrivi_media(content_id, url)
    logger.info(f"✅ Immagine contenuto {content_id} su S3")
    return {"ok": True, "message": "Immagine generata", "urls": [url]}


def generate_media_for_content(content_id: int, use_ai_cover: bool = True, stile: Optional[str] = None) -> dict:
    """Genera il media del contenuto nello stile richiesto.

    Tre strade distinte: immagini (carosello se ci sono le slide, altrimenti
    una sola), video fatto con le nostre slide, video con le clip di
    repertorio. Il media e' uno solo e vale per tutte le uscite: lo stesso
    verticale va su TikTok, sui Reels di Instagram e su quelli di Facebook.
    """
    from app.social.tipi import IMMAGINI, TIPI, VIDEO_COMPLETO, VIDEO_SLIDE

    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return {"ok": False, "message": "Contenuto non trovato"}
        scelto = stile if stile in TIPI else (contenuto.content_kind or IMMAGINI)
        try:
            extra = json.loads(contenuto.extra_content or "{}")
        except json.JSONDecodeError:
            extra = {}
        ha_slide = bool([s for s in (extra.get("carousel_slides") or []) if isinstance(s, str) and s.strip()])

    if scelto in (VIDEO_COMPLETO, VIDEO_SLIDE):
        from app.social.video_generator import generate_video_for_content
        esito = generate_video_for_content(content_id, usa_repertorio=(scelto == VIDEO_COMPLETO))
    else:
        esito = (generate_carousel_for_content(content_id, use_ai_cover=use_ai_cover) if ha_slide
                 else generate_image_for_content(content_id, use_ai_cover=use_ai_cover))
        scelto = IMMAGINI

    if esito.get("ok"):
        _segna_tipo(content_id, scelto)
    return esito


def _segna_tipo(content_id: int, tipo: str) -> None:
    """Ricorda con che stile e' stato generato il media, sul contenuto e sulle uscite."""
    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return
        contenuto.content_kind = tipo
        contenuto.updated_at = datetime.utcnow()
        session.add(contenuto)
        for uscita in session.exec(select(SocialDraft).where(SocialDraft.content_id == content_id)).all():
            uscita.content_kind = tipo
            session.add(uscita)
        session.commit()


def generate_media_for_draft(draft_id: int, use_ai_cover: bool = True, stile: Optional[str] = None) -> dict:
    """Come sopra, partendo da un'uscita: genera il media del suo contenuto."""
    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return {"ok": False, "message": "Uscita non trovata"}
        content_id = draft.content_id
    if not content_id:
        return {"ok": False, "message": "Questa uscita non è collegata a nessun contenuto"}
    return generate_media_for_content(content_id, use_ai_cover=use_ai_cover, stile=stile)


def generate_carousel_for_content(content_id: int, use_ai_cover: bool = True) -> dict:
    """Genera il carosello del contenuto e compila media_urls. Ritorna {ok, message}."""
    with Session(engine) as session:
        contenuto = session.get(SocialContent, content_id)
        if not contenuto:
            return {"ok": False, "message": "Contenuto non trovato"}
        try:
            extra = json.loads(contenuto.extra_content or "{}")
        except json.JSONDecodeError:
            extra = {}
        titolo = contenuto.source_title

    hook = (extra.get("hook") or "").strip()
    slides = [s for s in (extra.get("carousel_slides") or []) if isinstance(s, str) and s.strip()]
    if not hook and not slides:
        return {"ok": False, "message": "Questo contenuto non ha hook né slide da mettere nel carosello"}
    if not hook:
        hook = slides.pop(0)

    logger.info(f"🖼️ Genero carosello per il contenuto {content_id} ({len(slides) + 1} slide)...")
    images = [_cover(hook, titolo or hook, use_ai=use_ai_cover)]
    total = len(slides)
    for i, slide_text in enumerate(slides, start=1):
        images.append(_slide(slide_text, i, total, is_last=(i == total)))

    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    urls = [
        _upload_immagine(img, f"social/content-{content_id}/{stamp}-slide-{n}.jpg")
        for n, img in enumerate(images)
    ]

    scrivi_media(content_id, urls)
    logger.info(f"✅ Carosello contenuto {content_id}: {len(urls)} immagini su S3")
    return {"ok": True, "message": f"Generate {len(urls)} immagini", "urls": urls}
