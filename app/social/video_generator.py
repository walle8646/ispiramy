"""
Generatore video verticali per i draft TikTok (vanno bene anche come Reels).

Dallo script del draft produce un MP4 1080x1920:
- lo script diviso in frasi (`script_segments` se il generatore di contenuti
  li ha prodotti, altrimenti diviso qui, frase per frase)
- una slide brandizzata per frase, con la frase scritta in grande: su TikTok
  molti guardano senza audio, e un video senza testo a schermo non si capisce
- la voce fuori campo di ElevenLabs, una registrazione per frase: ogni slide
  resta a schermo esattamente quanto dura la sua frase
- il montaggio con ffmpeg (H.264 + AAC, 30 fps) e un lento movimento di camera,
  perche' non sembri una presentazione ferma
L'MP4 finisce su S3 e l'URL in draft.media_urls, pronto per la pubblicazione.

Variabili d'ambiente: ELEVENLABS_API_KEY e ELEVENLABS_VOICE_ID obbligatorie,
ELEVENLABS_MODEL_ID facoltativa, FFMPEG_BINARY facoltativa (altrimenti ffmpeg
di sistema o quello del pacchetto imageio-ffmpeg).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageDraw
from sqlmodel import Session

from app.database import engine
from app.logger_config import logger
from app.models import SocialDraft
from app.social.image_generator import (
    GREEN,
    GREEN_BG,
    GREEN_DARK,
    GREEN_LIGHT,
    GREEN_MID,
    TEXT_DARK,
    WHITE,
    _carica_su_s3,
    _draw_wrapped,
    _font,
    _logo,
    _wrap_text,
)

# Formato verticale di TikTok e Reels
W, H = 1080, 1920
FPS = 30

# Movimento di camera: la slide si disegna su una tela piu' larga del
# fotogramma e l'inquadratura scorre di PAN pixel da un lato all'altro.
# Niente ingrandimento: con lo zoom i margini si mangiavano e a fine scena il
# testo usciva dall'inquadratura o finiva sotto i pulsanti di TikTok.
BORDO_X = 60
PAN = 40
LARGHEZZA_TELA = W + 2 * BORDO_X

# Zone coperte dall'interfaccia di TikTok (nome e didascalia in basso, pulsanti
# a destra): il testo che conta deve restarne fuori anche a camera spostata
MARGINE_ALTO = 180
MARGINE_BASSO = 440
MARGINE_SX = 90
MARGINE_DX = 170
LARGHEZZA_TESTO = W - MARGINE_SX - MARGINE_DX
X_TESTO = BORDO_X + MARGINE_SX  # dove comincia il testo sulla tela

PAUSA_TRA_FRASI = 0.30   # secondi di silenzio dopo ogni frase
CODA_FINALE = 0.80       # la CTA resta a schermo un attimo dopo l'ultima parola
DURATA_MINIMA = 3.5      # TikTok rifiuta i video sotto i 3 secondi
MAX_SEGMENTI = 10
FREQUENZA_AUDIO = 44100

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODELLO_PREDEFINITO = "eleven_multilingual_v2"


class VideoNonGenerato(Exception):
    """Il video non si puo' fare: il messaggio va mostrato a chi ha premuto il bottone."""


# --------------------------------------------------------------------------- script

_FINE_FRASE = re.compile(r"(?<=[.!?…])\s+")


def _normalizza(testo: str) -> str:
    return re.sub(r"[^a-z0-9àèéìòù]+", "", testo.lower())


def segmenti_dello_script(extra: dict) -> list[str]:
    """Le frasi del video, una per slide, con l'hook in apertura."""
    segmenti = [
        s.strip() for s in (extra.get("script_segments") or [])
        if isinstance(s, str) and s.strip()
    ]
    if not segmenti:
        # I draft generati prima di script_segments hanno solo il blocco unico.
        # Una frase cortissima ("Ecco come.") da sola durerebbe mezzo secondo:
        # si accorpa alla successiva.
        pezzi = [p.strip() for p in _FINE_FRASE.split((extra.get("script") or "").strip()) if p.strip()]
        accumulo = ""
        for pezzo in pezzi:
            accumulo = f"{accumulo} {pezzo}".strip()
            if len(accumulo) >= 40:
                segmenti.append(accumulo)
                accumulo = ""
        if accumulo:
            if segmenti:
                segmenti[-1] = f"{segmenti[-1]} {accumulo}"
            else:
                segmenti.append(accumulo)

    hook = (extra.get("hook") or "").strip()
    if hook:
        gia_in_apertura = segmenti and (
            _normalizza(segmenti[0]).startswith(_normalizza(hook)[:30])
            or _normalizza(hook).startswith(_normalizza(segmenti[0])[:30])
        )
        if not gia_in_apertura:
            segmenti.insert(0, hook)

    if len(segmenti) > MAX_SEGMENTI:
        # Meglio un'ultima slide piu' lunga che tagliare la chiamata finale
        segmenti = segmenti[:MAX_SEGMENTI - 1] + [" ".join(segmenti[MAX_SEGMENTI - 1:])]
    return segmenti


# --------------------------------------------------------------------------- strumenti

def ffmpeg_exe() -> str:
    """Il binario di ffmpeg: esplicito, di sistema o quello di imageio-ffmpeg."""
    esplicito = os.getenv("FFMPEG_BINARY")
    if esplicito:
        return esplicito
    di_sistema = shutil.which("ffmpeg")
    if di_sistema:
        return di_sistema
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise VideoNonGenerato(f"ffmpeg non disponibile sul server ({e})") from e


def _esegui(argomenti: list[str], cosa: str) -> None:
    esito = subprocess.run(
        argomenti, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    if esito.returncode != 0:
        if esito.returncode < 0 or esito.returncode == 137:
            # Ucciso dal sistema, non fallito da se': sui server piccoli succede
            # quando finisce la memoria, e ffmpeg non fa in tempo a scrivere nulla
            raise VideoNonGenerato(f"{cosa} interrotto dal sistema (probabilmente memoria esaurita sul server)")
        coda = (esito.stderr or "").strip().splitlines()[-3:]
        raise VideoNonGenerato(f"{cosa} non riuscito: {' | '.join(coda)[:300]}")


def _config_elevenlabs() -> tuple[str, str, str]:
    chiave = os.getenv("ELEVENLABS_API_KEY")
    voce = os.getenv("ELEVENLABS_VOICE_ID")
    mancanti = [nome for nome, valore in (("ELEVENLABS_API_KEY", chiave), ("ELEVENLABS_VOICE_ID", voce)) if not valore]
    if mancanti:
        raise VideoNonGenerato(
            "Voce non configurata: manca " + " e ".join(mancanti) + " nelle variabili d'ambiente"
        )
    return chiave, voce, os.getenv("ELEVENLABS_MODEL_ID") or MODELLO_PREDEFINITO


def voce_elevenlabs(testo: str) -> bytes:
    """Registra una frase con la voce scelta. Ritorna l'MP3."""
    chiave, voce, modello = _config_elevenlabs()
    risposta = requests.post(
        ELEVENLABS_URL.format(voice_id=voce),
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": chiave, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        json={
            "text": testo,
            "model_id": modello,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=90,
    )
    if risposta.status_code != 200:
        dettaglio = risposta.text[:200]
        try:
            corpo = risposta.json().get("detail")
            if isinstance(corpo, dict):
                dettaglio = corpo.get("message") or corpo.get("status") or dettaglio
            elif isinstance(corpo, str):
                dettaglio = corpo
        except ValueError:
            pass
        raise VideoNonGenerato(f"ElevenLabs ha rifiutato la richiesta ({risposta.status_code}): {dettaglio}")
    if not risposta.content:
        raise VideoNonGenerato("ElevenLabs ha risposto senza audio")
    return risposta.content


# --------------------------------------------------------------------------- audio

def _in_wav(ffmpeg: str, sorgente: Path, destinazione: Path) -> None:
    """Qualunque audio in WAV mono 16 bit: cosi' la durata si legge al campione."""
    _esegui(
        [ffmpeg, "-v", "error", "-y", "-i", str(sorgente),
         "-ac", "1", "-ar", str(FREQUENZA_AUDIO), "-sample_fmt", "s16", str(destinazione)],
        "Conversione dell'audio",
    )


def _unisci_voci(wavs: list[Path], uscita: Path) -> list[float]:
    """Mette in fila le frasi con una pausa dopo ciascuna.

    Ritorna quanto dura ogni pezzo (frase + pausa): e' anche quanto resta a
    schermo la sua slide, quindi voce e immagini non si sfasano mai.
    """
    registrazioni = []
    for percorso in wavs:
        with wave.open(str(percorso), "rb") as w:
            registrazioni.append(w.readframes(w.getnframes()))

    pause = [PAUSA_TRA_FRASI] * (len(registrazioni) - 1) + [CODA_FINALE]
    durata_voce = sum(len(r) / 2 / FREQUENZA_AUDIO for r in registrazioni)
    durata_totale = durata_voce + sum(pause)
    if durata_totale < DURATA_MINIMA:
        pause[-1] += DURATA_MINIMA - durata_totale

    durate = []
    with wave.open(str(uscita), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(FREQUENZA_AUDIO)
        for registrazione, pausa in zip(registrazioni, pause):
            silenzio = b"\x00\x00" * int(FREQUENZA_AUDIO * pausa)
            out.writeframes(registrazione + silenzio)
            durate.append((len(registrazione) + len(silenzio)) / 2 / FREQUENZA_AUDIO)
    return durate


# --------------------------------------------------------------------------- slide

def _decorazioni(img: Image.Image) -> None:
    """Cerchi morbidi agli angoli, come nei caroselli."""
    for cx, cy, r, colore, alpha in [
        (80, 110, 190, GREEN_LIGHT, 26),
        (LARGHEZZA_TELA - 40, H - 260, 280, GREEN, 22),
        (LARGHEZZA_TELA - 120, 190, 110, GREEN_MID, 30),
        (70, H - 150, 150, GREEN_MID, 22),
    ]:
        cerchio = Image.new("RGBA", (r * 2, r * 2), (0, 0, 0, 0))
        ImageDraw.Draw(cerchio).ellipse([0, 0, r * 2, r * 2], fill=colore + (alpha,))
        img.paste(cerchio, (cx - r, cy - r), cerchio)


def _testo_centrato(draw, testo, nome_font, dimensione, minimo, max_righe, colore, alto, basso) -> int:
    """Testo centrato fra `alto` e `basso`, rimpicciolito finche' ci sta."""
    font = _font(nome_font, dimensione)
    while len(_wrap_text(draw, testo, font, LARGHEZZA_TESTO)) > max_righe and font.size > minimo:
        font = _font(nome_font, font.size - 4)
    righe = len(_wrap_text(draw, testo, font, LARGHEZZA_TESTO))
    altezza = righe * int(font.size * 1.25)
    y = alto + max(0, (basso - alto - altezza) // 2)
    return _draw_wrapped(draw, testo, font, LARGHEZZA_TESTO, X_TESTO, y, colore, align_center=True)


def _pillola(draw, testo, font, y, sfondo, colore) -> int:
    larghezza = draw.textlength(testo, font=font)
    x = X_TESTO + (LARGHEZZA_TESTO - larghezza) / 2
    draw.rounded_rectangle(
        [x - 36, y, x + larghezza + 36, y + font.size + 44], radius=(font.size + 44) // 2, fill=sfondo,
    )
    draw.text((x, y + 18), testo, font=font, fill=colore)
    return y + font.size + 44


def _firma(draw, colore) -> None:
    font = _font("Poppins-SemiBold.ttf", 40)
    testo = "ispiramy.com"
    x = X_TESTO + (LARGHEZZA_TESTO - draw.textlength(testo, font=font)) / 2
    draw.text((x, H - MARGINE_BASSO - 80), testo, font=font, fill=colore)


def _slide(testo: str, indice: int, totale: int) -> Image.Image:
    """Una scena del video, sulla tela larga. La prima e' l'hook, l'ultima la chiamata al sito."""
    area_alta = MARGINE_ALTO + 170
    area_bassa = H - MARGINE_BASSO - 140

    if indice == totale and totale > 1:
        img = Image.new("RGB", (LARGHEZZA_TELA, H), GREEN)
        draw = ImageDraw.Draw(img)
        logo = _logo(150)
        if logo:
            img.paste(logo, (BORDO_X + (W - 150) // 2, MARGINE_ALTO + 20), logo)
        fine = _testo_centrato(draw, testo, "Poppins-Bold.ttf", 78, 48, 7, WHITE, area_alta, area_bassa - 160)
        _pillola(draw, "Trova il tuo esperto  »", _font("Poppins-SemiBold.ttf", 44), fine + 60, WHITE, GREEN_DARK)
        _firma(draw, WHITE)
        return img

    img = Image.new("RGB", (LARGHEZZA_TELA, H), GREEN_BG)
    _decorazioni(img)
    draw = ImageDraw.Draw(img)

    if indice == 1:
        _pillola(draw, "IL CONSIGLIO DI ISPIRAMY", _font("Poppins-SemiBold.ttf", 34), MARGINE_ALTO + 20, GREEN, WHITE)
        _testo_centrato(draw, testo, "Poppins-Bold.ttf", 92, 56, 6, TEXT_DARK, area_alta, area_bassa)
    else:
        # Barra di avanzamento: chi guarda capisce quanto manca
        y_barra = MARGINE_ALTO + 40
        draw.rounded_rectangle([X_TESTO, y_barra, X_TESTO + LARGHEZZA_TESTO, y_barra + 14], radius=7, fill=WHITE)
        pieno = int(LARGHEZZA_TESTO * indice / totale)
        draw.rounded_rectangle([X_TESTO, y_barra, X_TESTO + pieno, y_barra + 14], radius=7, fill=GREEN_MID)
        _testo_centrato(draw, testo, "Poppins-SemiBold.ttf", 76, 48, 8, TEXT_DARK, area_alta, area_bassa)

    _firma(draw, GREEN)
    return img


# --------------------------------------------------------------------------- montaggio

# Stessi parametri per ogni scena: e' cio' che permette di unirle senza ricodificare.
# Un thread e lookahead corto tengono bassa la memoria del codificatore: Render
# Starter ha 512 MB in tutto e l'app ne occupa gia' circa 130.
CODIFICA_VIDEO = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
    "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
    "-threads", "1", "-x264-params", "rc-lookahead=5:sync-lookahead=0",
]


def _monta(ffmpeg: str, slide: list[Path], durate: list[float], audio: Path, uscita: Path) -> None:
    """Una scena alla volta, poi le scene in fila e la voce sopra.

    Prima tutte le scene entravano in un unico comando ffmpeg: restavano aperte
    e decodificate insieme e il montaggio arrivava a 3 GB di memoria. Su Render
    (512 MB) il processo veniva ucciso e il video non usciva mai.

    Ogni scena poi decodifica la sua immagine una volta sola e la ripete dentro
    il filtro `loop`. Con `-loop 1` sull'ingresso ffmpeg ridecodificava il PNG a
    ogni fotogramma, piu' in fretta di quanto il codificatore li consumasse, e i
    fotogrammi grezzi (7 MB l'uno) si accumulavano in coda. Misurato su Linux
    con lo stesso ffmpeg di Render: da 391 a 164 MB per scena.
    """
    cartella = uscita.parent
    clip = []
    fine = 0.0
    fotogrammi_fatti = 0
    for i, (percorso, durata) in enumerate(zip(slide, durate)):
        # Fotogrammi contati sul totale progressivo: gli arrotondamenti delle
        # singole scene non si sommano, e il video resta allineato alla voce
        fine += durata
        fotogrammi = max(1, round(fine * FPS) - fotogrammi_fatti)
        fotogrammi_fatti += fotogrammi
        secondi = fotogrammi / FPS

        # L'inquadratura scorre di 2*PAN pixel attorno al centro della tela,
        # alternando il verso a ogni scena
        avanzamento = f"min(1,t/{secondi:.3f})" if i % 2 == 0 else f"(1-min(1,t/{secondi:.3f}))"
        pezzo = cartella / f"scena-{i + 1}.mp4"
        _esegui(
            [ffmpeg, "-v", "error", "-y",
             "-framerate", str(FPS), "-i", str(percorso),
             "-vf", (f"loop=loop=-1:size=1:start=0,"
                     f"crop={W}:{H}:x='{BORDO_X - PAN}+{2 * PAN}*{avanzamento}':y=0,"
                     f"setsar=1,format=yuv420p"),
             "-frames:v", str(fotogrammi),
             *CODIFICA_VIDEO, str(pezzo)],
            f"Montaggio della scena {i + 1}",
        )
        clip.append(pezzo)

    elenco = cartella / "scene.txt"
    elenco.write_text("".join(f"file '{p.as_posix()}'\n" for p in clip), encoding="utf-8")
    _esegui(
        [ffmpeg, "-v", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(elenco), "-i", str(audio),
         "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "128k", "-ar", str(FREQUENZA_AUDIO),
         # Niente -shortest: voce e scene sono gia' lunghe uguali per
         # costruzione, e -shortest con l'AAC tagliava gli ultimi fotogrammi
         "-movflags", "+faststart", str(uscita)],
        "Unione delle scene",
    )


def _produci(draft_id: int, segmenti: list[str], ffmpeg: str) -> tuple[str, float]:
    with tempfile.TemporaryDirectory(prefix=f"video-draft-{draft_id}-") as cartella:
        base = Path(cartella)

        wavs = []
        for i, testo in enumerate(segmenti, start=1):
            registrazione = base / f"voce-{i}.mp3"
            registrazione.write_bytes(voce_elevenlabs(testo))
            wav = base / f"voce-{i}.wav"
            _in_wav(ffmpeg, registrazione, wav)
            wavs.append(wav)
        audio = base / "voce.wav"
        durate = _unisci_voci(wavs, audio)

        slide = []
        for i, testo in enumerate(segmenti, start=1):
            percorso = base / f"scena-{i}.png"
            _slide(testo, i, len(segmenti)).save(percorso, compress_level=1)
            slide.append(percorso)

        video = base / "video.mp4"
        _monta(ffmpeg, slide, durate, audio, video)

        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        url = _carica_su_s3(video.read_bytes(), f"social/draft-{draft_id}/{stamp}-video.mp4", "video/mp4")
        return url, sum(durate)


def generate_video_for_draft(draft_id: int) -> dict:
    """Genera il video del draft e compila media_urls. Ritorna {ok, message}."""
    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return {"ok": False, "message": "Draft non trovato"}
        if draft.status in ("published", "publishing"):
            return {"ok": False, "message": "Draft già pubblicato"}
        try:
            extra = json.loads(draft.extra_content or "{}")
        except json.JSONDecodeError:
            extra = {}
    segmenti = segmenti_dello_script(extra)
    if not segmenti:
        return {"ok": False, "message": "Questo draft non ha uno script da trasformare in video"}

    logger.info(f"🎬 Genero video per draft {draft_id} ({len(segmenti)} scene)...")
    try:
        # Prima i controlli che costano zero: niente slide se poi manca la voce
        _config_elevenlabs()
        ffmpeg = ffmpeg_exe()
        url, durata = _produci(draft_id, segmenti, ffmpeg)
    except VideoNonGenerato as e:
        logger.warning(f"Video draft {draft_id} non generato: {e}")
        return {"ok": False, "message": str(e)}

    # La generazione dura minuti: il draft si rilegge adesso, non si tiene
    # aperta una sessione sul database per tutto quel tempo
    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return {"ok": False, "message": "Il draft è stato eliminato durante la generazione"}
        if draft.status in ("published", "publishing"):
            return {"ok": False, "message": "Il draft è stato pubblicato durante la generazione"}
        draft.media_urls = url
        draft.updated_at = datetime.utcnow()
        session.add(draft)
        session.commit()

    logger.info(f"✅ Video draft {draft_id}: {durata:.1f}s su S3")
    return {"ok": True, "message": f"Video generato: {len(segmenti)} scene, {durata:.0f} secondi", "urls": [url]}
