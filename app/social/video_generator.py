"""
Generatore video verticali per i draft TikTok (vanno bene anche per Reels e Stories).

Dallo script del draft produce un MP4 1080x1920 di 15-30 secondi:
- lo script diviso in frasi (`script_segments` se il generatore di contenuti
  li ha prodotti, altrimenti diviso qui, frase per frase)
- una scena per frase: uno spezzone di repertorio (Pexels) con la frase scritta
  sopra, oppure una slide brandizzata quando la clip non si trova. La chiusura
  e' sempre una slide nostra, cosi' marchio e invito non li disegna nessun
  modello al posto nostro
- la voce fuori campo di ElevenLabs, una registrazione per frase: ogni scena
  resta a schermo esattamente quanto dura la sua frase
- musica di sottofondo, se configurata, tenuta sotto la voce
- montaggio con ffmpeg (H.264 + AAC, 30 fps), una scena alla volta per non
  esaurire la memoria del server

Variabili d'ambiente: ELEVENLABS_API_KEY e ELEVENLABS_VOICE_ID obbligatorie,
PEXELS_API_KEY per il repertorio, MUSICA_VIDEO (file o URL) per la musica,
ELEVENLABS_MODEL_ID e FFMPEG_BINARY facoltative.
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
from typing import Optional

import requests
from PIL import Image, ImageDraw
from sqlmodel import Session

from app.database import engine
from app.logger_config import logger
from app.models import SocialDraft
from app.social import repertorio
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

# Formato verticale di TikTok, Reels e Stories
W, H = 1080, 1920
FPS = 30

# Movimento di camera sulle slide: la slide si disegna su una tela piu' larga
# del fotogramma e l'inquadratura scorre di PAN pixel da un lato all'altro.
# Niente ingrandimento: con lo zoom i margini si mangiavano e a fine scena il
# testo usciva dall'inquadratura o finiva sotto i pulsanti di TikTok.
BORDO_X = 60
PAN = 40
LARGHEZZA_TELA = W + 2 * BORDO_X

# Zone coperte dall'interfaccia (nome e didascalia in basso, pulsanti a destra):
# il testo che conta deve restarne fuori anche a camera spostata
MARGINE_ALTO = 180
MARGINE_BASSO = 440
MARGINE_SX = 90
MARGINE_DX = 170
LARGHEZZA_TESTO = W - MARGINE_SX - MARGINE_DX
X_TESTO = BORDO_X + MARGINE_SX  # dove comincia il testo sulla tela delle slide

PAUSA_TRA_FRASI = 0.30   # secondi di silenzio dopo ogni frase
CODA_FINALE = 0.80       # la chiusura resta a schermo un attimo dopo l'ultima parola
DURATA_MINIMA = 3.5      # TikTok rifiuta i video sotto i 3 secondi
DURATA_CONSIGLIATA = 30  # oltre, il video non e' piu' da Reels/Stories
MAX_SEGMENTI = 8
FREQUENZA_AUDIO = 44100
VOLUME_MUSICA = 0.10

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
MODELLO_PREDEFINITO = "eleven_multilingual_v2"


class VideoNonGenerato(Exception):
    """Il video non si puo' fare: il messaggio va mostrato a chi ha premuto il bottone."""


# --------------------------------------------------------------------------- script

_FINE_FRASE = re.compile(r"(?<=[.!?…])\s+")


def _normalizza(testo: str) -> str:
    return re.sub(r"[^a-z0-9àèéìòù]+", "", testo.lower())


def segmenti_dello_script(extra: dict) -> list[str]:
    """Le frasi del video, una per scena, con l'hook in apertura."""
    segmenti = [
        s.strip() for s in (extra.get("script_segments") or [])
        if isinstance(s, str) and s.strip()
    ]
    if not segmenti:
        # Le bozze Instagram non hanno uno script: hanno le slide del carosello,
        # che sono gia' frasi brevi e in ordine. Meglio quelle che l'hook da solo.
        segmenti = [
            s.strip() for s in (extra.get("carousel_slides") or [])
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
        # Meglio un'ultima scena piu' lunga che tagliare la chiamata finale
        segmenti = segmenti[:MAX_SEGMENTI - 1] + [" ".join(segmenti[MAX_SEGMENTI - 1:])]
    return segmenti


def parole_per_scena(extra: dict, segmenti: list[str]) -> list[str]:
    """Le parole chiave con cui cercare la clip di ogni scena, allineate alle frasi."""
    parole = [p.strip() if isinstance(p, str) else "" for p in (extra.get("scene_keywords") or [])]
    originali = [s.strip() for s in (extra.get("script_segments") or []) if isinstance(s, str) and s.strip()]
    if len(parole) == len(segmenti):
        return parole
    if originali and len(parole) == len(originali) and len(segmenti) == len(originali) + 1:
        return [""] + parole  # l'hook e' stato aggiunto in testa alle frasi
    return (parole + [""] * len(segmenti))[:len(segmenti)]


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


def _musica(cartella: Path) -> Optional[Path]:
    """La musica di sottofondo, se configurata (file locale o URL)."""
    indicata = os.getenv("MUSICA_VIDEO")
    if not indicata:
        predefinita = Path(__file__).parent.parent / "static" / "audio" / "musica-social.mp3"
        return predefinita if predefinita.exists() else None
    if indicata.startswith("http"):
        try:
            risposta = requests.get(indicata, timeout=60)
            if risposta.status_code != 200 or not risposta.content:
                logger.warning(f"Musica non scaricata ({risposta.status_code}): video senza sottofondo")
                return None
            percorso = cartella / "musica.mp3"
            percorso.write_bytes(risposta.content)
            return percorso
        except requests.RequestException as e:
            logger.warning(f"Musica non scaricata ({e}): video senza sottofondo")
            return None
    percorso = Path(indicata)
    if not percorso.exists():
        logger.warning(f"MUSICA_VIDEO punta a un file inesistente ({indicata}): video senza sottofondo")
        return None
    return percorso


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
    schermo la sua scena, quindi voce e immagini non si sfasano mai.
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


# --------------------------------------------------------------------------- grafica

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


def _testo_centrato(draw, testo, nome_font, dimensione, minimo, max_righe, colore, alto, basso, x=X_TESTO) -> int:
    """Testo centrato fra `alto` e `basso`, rimpicciolito finche' ci sta."""
    font = _font(nome_font, dimensione)
    while len(_wrap_text(draw, testo, font, LARGHEZZA_TESTO)) > max_righe and font.size > minimo:
        font = _font(nome_font, font.size - 4)
    righe = len(_wrap_text(draw, testo, font, LARGHEZZA_TESTO))
    altezza = righe * int(font.size * 1.25)
    y = alto + max(0, (basso - alto - altezza) // 2)
    return _draw_wrapped(draw, testo, font, LARGHEZZA_TESTO, x, y, colore, align_center=True)


def _pillola(draw, testo, font, y, sfondo, colore, x_testo=X_TESTO) -> int:
    larghezza = draw.textlength(testo, font=font)
    x = x_testo + (LARGHEZZA_TESTO - larghezza) / 2
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
    """Una scena disegnata da noi, sulla tela larga. L'ultima e' la chiamata al sito."""
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
        y_barra = MARGINE_ALTO + 40
        draw.rounded_rectangle([X_TESTO, y_barra, X_TESTO + LARGHEZZA_TESTO, y_barra + 14], radius=7, fill=WHITE)
        pieno = int(LARGHEZZA_TESTO * indice / totale)
        draw.rounded_rectangle([X_TESTO, y_barra, X_TESTO + pieno, y_barra + 14], radius=7, fill=GREEN_MID)
        _testo_centrato(draw, testo, "Poppins-SemiBold.ttf", 76, 48, 8, TEXT_DARK, area_alta, area_bassa)

    _firma(draw, GREEN)
    return img


def _sovrapposizione(testo: str, indice: int, totale: int) -> Image.Image:
    """Quello che scriviamo sopra alla clip di repertorio: marchio, barra e frase.

    Il testo sta in un riquadro scuro semitrasparente: sopra un video qualunque
    il bianco da solo diventa illeggibile appena la scena schiarisce.
    """
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x_testo = MARGINE_SX

    _pillola(draw, "IL CONSIGLIO DI ISPIRAMY", _font("Poppins-SemiBold.ttf", 34),
             MARGINE_ALTO, GREEN + (235,), WHITE, x_testo=x_testo)

    y_barra = MARGINE_ALTO + 110
    draw.rounded_rectangle([x_testo, y_barra, x_testo + LARGHEZZA_TESTO, y_barra + 12],
                           radius=6, fill=(255, 255, 255, 110))
    draw.rounded_rectangle([x_testo, y_barra, x_testo + int(LARGHEZZA_TESTO * indice / totale), y_barra + 12],
                           radius=6, fill=GREEN_MID + (255,))

    font = _font("Poppins-Bold.ttf", 76)
    while len(_wrap_text(draw, testo, font, LARGHEZZA_TESTO - 60)) > 5 and font.size > 46:
        font = _font("Poppins-Bold.ttf", font.size - 4)
    righe = _wrap_text(draw, testo, font, LARGHEZZA_TESTO - 60)
    altezza_testo = len(righe) * int(font.size * 1.25)

    basso = H - MARGINE_BASSO
    alto = basso - altezza_testo - 80
    draw.rounded_rectangle([x_testo - 30, alto, x_testo + LARGHEZZA_TESTO + 30, basso],
                           radius=36, fill=(12, 32, 18, 205))
    _draw_wrapped(draw, testo, font, LARGHEZZA_TESTO - 60, x_testo + 30, alto + 40, WHITE + (255,), align_center=True)

    firma = _font("Poppins-SemiBold.ttf", 38)
    larghezza = draw.textlength("ispiramy.com", font=firma)
    draw.text((x_testo + (LARGHEZZA_TESTO - larghezza) / 2, basso + 26), "ispiramy.com",
              font=firma, fill=WHITE + (230,))
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


def _comando_scena(ffmpeg: str, scena: dict, fotogrammi: int, avanzamento: str, uscita: Path) -> list[str]:
    """Il comando ffmpeg di una scena: clip di repertorio o slide disegnata."""
    if scena["tipo"] == "repertorio":
        # La clip si ripete se e' piu' corta della frase; sopra ci va la nostra
        # grafica, che e' un'immagine sola e viene ripetuta su ogni fotogramma
        filtro = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"fps={FPS},eq=brightness=-0.05:saturation=0.95[sfondo];"
            f"[sfondo][1:v]overlay=0:0,setsar=1,format=yuv420p[video]"
        )
        return [ffmpeg, "-v", "error", "-y",
                "-stream_loop", "-1", "-i", str(scena["clip"]),
                "-i", str(scena["immagine"]),
                "-filter_complex", filtro,
                "-map", "[video]", "-an", "-frames:v", str(fotogrammi),
                *CODIFICA_VIDEO, str(uscita)]

    # Slide: l'immagine si decodifica una volta sola e si ripete nel filtro.
    # Con -loop 1 sull'ingresso ffmpeg la ridecodificava a ogni fotogramma, piu'
    # in fretta di quanto il codificatore li consumasse, e i fotogrammi grezzi
    # (7 MB l'uno) si accumulavano in coda: da 391 a 164 MB per scena.
    return [ffmpeg, "-v", "error", "-y",
            "-framerate", str(FPS), "-i", str(scena["immagine"]),
            "-vf", (f"loop=loop=-1:size=1:start=0,"
                    f"crop={W}:{H}:x='{BORDO_X - PAN}+{2 * PAN}*{avanzamento}':y=0,"
                    f"setsar=1,format=yuv420p"),
            "-frames:v", str(fotogrammi),
            *CODIFICA_VIDEO, str(uscita)]


def _monta(ffmpeg: str, scene: list[dict], durate: list[float], audio: Path, uscita: Path,
           musica: Optional[Path] = None) -> None:
    """Una scena alla volta, poi le scene in fila e la voce sopra.

    Prima tutte le scene entravano in un unico comando ffmpeg: restavano aperte
    e decodificate insieme e il montaggio arrivava a 3 GB di memoria. Su Render
    (512 MB) il processo veniva ucciso e il video non usciva mai.
    """
    cartella = uscita.parent
    clip = []
    fine = 0.0
    fotogrammi_fatti = 0
    for i, (scena, durata) in enumerate(zip(scene, durate)):
        # Fotogrammi contati sul totale progressivo: gli arrotondamenti delle
        # singole scene non si sommano, e il video resta allineato alla voce
        fine += durata
        fotogrammi = max(1, round(fine * FPS) - fotogrammi_fatti)
        fotogrammi_fatti += fotogrammi
        secondi = fotogrammi / FPS

        # Sulle slide l'inquadratura scorre attorno al centro della tela,
        # alternando il verso a ogni scena
        avanzamento = f"min(1,t/{secondi:.3f})" if i % 2 == 0 else f"(1-min(1,t/{secondi:.3f}))"
        pezzo = cartella / f"pezzo-{i + 1}.mp4"
        _esegui(_comando_scena(ffmpeg, scena, fotogrammi, avanzamento, pezzo), f"Montaggio della scena {i + 1}")
        clip.append(pezzo)

    elenco = cartella / "scene.txt"
    elenco.write_text("".join(f"file '{p.as_posix()}'\n" for p in clip), encoding="utf-8")

    argomenti = [ffmpeg, "-v", "error", "-y",
                 "-f", "concat", "-safe", "0", "-i", str(elenco),
                 "-i", str(audio)]
    if musica:
        durata_totale = fotogrammi_fatti / FPS
        argomenti += ["-stream_loop", "-1", "-i", str(musica),
                      "-filter_complex",
                      (f"[2:a]volume={VOLUME_MUSICA},afade=t=out:st={max(0.0, durata_totale - 1.2):.2f}:d=1.2[sottofondo];"
                       f"[1:a][sottofondo]amix=inputs=2:duration=first:normalize=0[audio]"),
                      "-map", "0:v", "-map", "[audio]"]
    else:
        argomenti += ["-map", "0:v", "-map", "1:a"]
    argomenti += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", str(FREQUENZA_AUDIO),
                  # Niente -shortest: voce e scene sono gia' lunghe uguali per
                  # costruzione, e -shortest con l'AAC tagliava gli ultimi fotogrammi
                  "-movflags", "+faststart", str(uscita)]
    _esegui(argomenti, "Unione delle scene")


def _produci(draft_id: int, segmenti: list[str], parole: list[str], ffmpeg: str,
             usa_repertorio: bool = True) -> tuple[str, float, list[str]]:
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

        scene, crediti = [], []
        ultima = len(segmenti)
        for i, testo in enumerate(segmenti, start=1):
            # La chiusura resta sempre nostra: marchio e invito non li disegna
            # nessuno al posto nostro
            trovata = None
            if i != ultima and usa_repertorio:
                trovata = repertorio.cerca_clip(parole[i - 1], durate[i - 1], base, f"clip-{i}")
            if trovata:
                immagine = base / f"testo-{i}.png"
                _sovrapposizione(testo, i, ultima).save(immagine)
                scene.append({"tipo": "repertorio", "clip": trovata["percorso"], "immagine": immagine})
                crediti.append(trovata["autore"])
            else:
                immagine = base / f"scena-{i}.png"
                _slide(testo, i, ultima).save(immagine, compress_level=1)
                scene.append({"tipo": "slide", "clip": None, "immagine": immagine})

        video = base / "video.mp4"
        _monta(ffmpeg, scene, durate, audio, video, _musica(base))

        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        url = _carica_su_s3(video.read_bytes(), f"social/draft-{draft_id}/{stamp}-video.mp4", "video/mp4")
        return url, sum(durate), crediti


def _materiale_sufficiente(segmenti: list[str], parole: list[str], usa_repertorio: bool) -> bool:
    """Un video di una scena sola non e' un video: serve altro materiale."""
    if len(segmenti) < 3:
        return False
    return not usa_repertorio or any(p for p in parole)


def _completa_materiale(draft_id: int, extra: dict) -> dict:
    """Fa scrivere al modello frasi e scene mancanti, e le salva sulla bozza.

    Cosi' si paga una volta sola: la prossima generazione dello stesso draft
    trova gia' tutto. Se il modello non e' disponibile si va avanti con quello
    che c'e': meglio un video piu' semplice che nessun video.
    """
    from app.social.content_generator import completa_script_video

    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if not draft:
            return extra
        titolo, caption = draft.source_title, draft.caption
    try:
        aggiunta = completa_script_video(titolo, caption, extra.get("carousel_slides"))
    except Exception as e:
        logger.warning(f"Video draft {draft_id}: script non completato ({e}), si usa il materiale esistente")
        return extra

    nuovo = dict(extra)
    nuovo.update(aggiunta)
    with Session(engine) as session:
        draft = session.get(SocialDraft, draft_id)
        if draft:
            draft.extra_content = json.dumps(nuovo, ensure_ascii=False)[:8000]
            draft.updated_at = datetime.utcnow()
            session.add(draft)
            session.commit()
    logger.info(f"🖊️ Video draft {draft_id}: script ricavato dal post ({len(aggiunta['script_segments'])} scene)")
    return nuovo


def generate_video_for_draft(draft_id: int, usa_repertorio: bool = True) -> dict:
    """Genera il video del draft e compila media_urls. Ritorna {ok, message}.

    Con usa_repertorio=False ogni scena e' una nostra slide: e' il "post video",
    piu' sobrio e senza dipendere da Pexels.
    """
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
    parole = parole_per_scena(extra, segmenti)
    if not _materiale_sufficiente(segmenti, parole, usa_repertorio):
        # Un post nato per le immagini non ha ne' script ne' scene: li si scrive
        # adesso, partendo dal testo che c'e' gia'
        extra = _completa_materiale(draft_id, extra)
        segmenti = segmenti_dello_script(extra)
        parole = parole_per_scena(extra, segmenti)
    if not segmenti:
        return {"ok": False, "message": "Questo draft non ha un testo da trasformare in video"}

    logger.info(f"🎬 Genero video per draft {draft_id} ({len(segmenti)} scene)...")
    try:
        # Prima i controlli che costano zero: niente scene se poi manca la voce
        _config_elevenlabs()
        ffmpeg = ffmpeg_exe()
        url, durata, crediti = _produci(draft_id, segmenti, parole, ffmpeg, usa_repertorio)
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

    messaggio = f"Video generato: {len(segmenti)} scene, {durata:.0f} secondi"
    if crediti:
        # Pexels non obbliga a citare, le linee guida dell'API lo chiedono dove
        # possibile: i nomi arrivano qui per finire nella caption
        messaggio += f". Riprese di {', '.join(dict.fromkeys(crediti))} (Pexels)"
    if durata > DURATA_CONSIGLIATA:
        messaggio += f". Attenzione: oltre i {DURATA_CONSIGLIATA} secondi, per Reels e Stories conviene più corto"
    logger.info(f"✅ Video draft {draft_id}: {durata:.1f}s su S3")
    return {"ok": True, "message": messaggio, "urls": [url]}
