"""Genera il video della homepage.

    python scripts/genera_video_homepage.py            # serve l'app in locale
    python scripts/genera_video_homepage.py --sito https://ispiramy.com

Il video precedente era fatto da un'intelligenza artificiale: mostrava
interfacce finte con scritte storpiate ("Aree di comptenza", "Lingue parlata")
e chiudeva col vecchio marchio Helpy. Durava 64 secondi e pesava 47 MB, che su
una homepage aperta da telefono sono un problema per chi guarda.

Qui le schermate dell'app sono vere: le riprende un browser senza finestra dal
sito stesso, quindi le scritte sono corrette per costruzione e restano
corrette anche quando l'interfaccia cambia. Le scene con le persone vengono da
Pexels (uso commerciale permesso), la voce da ElevenLabs.

Il video parte muto e con l'audio disattivato: il testo a schermo deve bastare
da solo, la voce e' un di piu' per chi tocca "Attiva audio".
"""
import argparse
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

L, A = 1280, 720
FPS = 30
CODIFICA = [
    "-c:v", "libx264", "-preset", "medium", "-crf", "23",
    "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
]
FREQUENZA_AUDIO = 44100
PAUSA_TRA_FRASI = 0.45
CODA_FINALE = 1.2
# Le schermate si riprendono piu' larghe del video: il margine serve al
# movimento lento (un fermo immagine per cinque secondi sembra un errore)
L_SCATTO, A_SCATTO = 1500, 844
ZOOM_FINALE = 1.08

VERDE = (67, 160, 71)
VERDE_SCURO = (27, 94, 32)
VERDE_CHIARO = (102, 187, 106)
BIANCO = (255, 255, 255)

FONT_BOLD = RADICE / "app" / "static" / "fonts" / "Poppins-Bold.ttf"
FONT_SEMI = RADICE / "app" / "static" / "fonts" / "Poppins-SemiBold.ttf"
FONT_REG = RADICE / "app" / "static" / "fonts" / "Poppins-Regular.ttf"

SCENE = [
    {
        "voce": "Hai un problema concreto, e nessuno intorno a te sa davvero come aiutarti.",
        "titolo": "Hai un problema concreto",
        "sotto": "E nessuno intorno a te sa da dove cominciare",
        "fonte": {"tipo": "repertorio", "parole": "worried man laptop home office"},
    },
    {
        "voce": "Su Ispiramy trovi chi lo ha già risolto: esperti veri, divisi per categoria.",
        "titolo": "Trovi chi l'ha già risolto",
        "sotto": "Esperti verificati, categoria per categoria",
        "fonte": {"tipo": "schermata", "percorso": "/consultants"},
    },
    {
        "voce": "Guardi esperienza, prezzo e recensioni, e scegli la persona giusta per te.",
        "titolo": "Scegli con calma",
        "sotto": "Esperienza, prezzo e recensioni, in chiaro",
        "fonte": {"tipo": "schermata", "percorso": "/user/1"},
    },
    {
        "voce": "Prenoti quando ti serve e parlate in videochiamata, senza installare niente.",
        "titolo": "Parlate in videochiamata",
        "sotto": "Dentro Ispiramy, senza installare niente",
        "fonte": {"tipo": "repertorio", "parole": "business video conference call desk office"},
    },
    {
        "voce": "Ispiramy: l'esperienza di chi c'è già passato, a portata di click.",
        "titolo": "Ispiramy",
        "sotto": "L'esperienza di chi c'è già passato,\na portata di click",
        "fonte": {"tipo": "marchio"},
    },
]


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _browser() -> str:
    """Il browser che scatta le schermate: va bene quello che c'e'."""
    candidati = [
        os.getenv("BROWSER_SCATTI"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ]
    for c in candidati:
        if c and Path(c).exists():
            return c
    raise SystemExit("Serve Chrome o Edge per riprendere le schermate del sito")


def _scatta(sito: str, percorso: str, destinazione: Path) -> None:
    """Una schermata vera del sito, presa da un browser senza finestra."""
    with __import__("tempfile").TemporaryDirectory() as profilo:
        subprocess.run([
            _browser(),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={profilo}",
            f"--window-size={L_SCATTO},{A_SCATTO}",
            "--virtual-time-budget=6000",
            f"--screenshot={destinazione}",
            sito.rstrip("/") + percorso,
        ], check=True, capture_output=True)
    if not destinazione.exists():
        raise SystemExit(f"Schermata non riuscita: {percorso}")


def _a_capo(testo: str, font: ImageFont.FreeTypeFont, larghezza: int) -> list[str]:
    righe = []
    for paragrafo in testo.split("\n"):
        riga = ""
        for parola in paragrafo.split():
            prova = f"{riga} {parola}".strip()
            if font.getbbox(prova)[2] <= larghezza or not riga:
                riga = prova
            else:
                righe.append(riga)
                riga = parola
        righe.append(riga)
    return righe


def _sovrimpressione(scena: dict, destinazione: Path) -> None:
    """La fascia col testo, trasparente, da posare sopra il filmato.

    Sta in basso e su una fascia scura: sopra un video qualsiasi il testo
    bianco "nudo" diventa illeggibile appena passa una parete chiara.
    """
    tela = Image.new("RGBA", (L, A), (0, 0, 0, 0))
    disegno = ImageDraw.Draw(tela)

    titolo_font = ImageFont.truetype(str(FONT_BOLD), 54)
    sotto_font = ImageFont.truetype(str(FONT_REG), 30)
    righe_titolo = _a_capo(scena["titolo"], titolo_font, L - 200)
    righe_sotto = _a_capo(scena["sotto"], sotto_font, L - 200)

    altezza_testo = len(righe_titolo) * 66 + len(righe_sotto) * 40 + 40
    cima = A - altezza_testo - 70

    # Sfumatura verso il basso: netta dove c'e' il testo, invisibile sopra
    for y in range(cima - 90, A):
        q = min(1.0, max(0.0, (y - (cima - 90)) / 160))
        disegno.line([(0, y), (L, y)], fill=(12, 30, 14, int(200 * q)))

    y = cima
    for riga in righe_titolo:
        disegno.text((100, y), riga, font=titolo_font, fill=BIANCO + (255,))
        y += 66
    y += 8
    for riga in righe_sotto:
        disegno.text((100, y), riga, font=sotto_font, fill=(214, 236, 216, 255))
        y += 40

    # Barretta verde a sinistra: lega il testo al marchio senza scriverlo
    disegno.rounded_rectangle([72, cima + 8, 80, y - 14], radius=4, fill=VERDE_CHIARO + (255,))
    tela.save(destinazione)


def _marchio(scena: dict, destinazione: Path) -> None:
    """La schermata finale: solo marchio e promessa, su verde."""
    tela = Image.new("RGB", (L, A), VERDE_SCURO)
    disegno = ImageDraw.Draw(tela)
    for y in range(A):
        q = y / (A - 1)
        disegno.line([(0, y), (L, y)], fill=tuple(
            round(VERDE[i] + (VERDE_SCURO[i] - VERDE[i]) * q) for i in range(3)))

    logo = Image.open(RADICE / "app" / "static" / "icone" / "icona-512.png").convert("RGBA")
    logo = logo.resize((150, 150), Image.LANCZOS)
    # Il marchio e' verde su bianco: qui serve un disco chiaro sotto
    disco = Image.new("RGBA", (182, 182), (0, 0, 0, 0))
    ImageDraw.Draw(disco).ellipse([0, 0, 181, 181], fill=(255, 255, 255, 255))
    disco.paste(logo, (16, 16), logo)
    tela.paste(disco, ((L - 182) // 2, 150), disco)

    nome_font = ImageFont.truetype(str(FONT_BOLD), 76)
    claim_font = ImageFont.truetype(str(FONT_SEMI), 34)
    nome = scena["titolo"]
    larghezza = disegno.textbbox((0, 0), nome, font=nome_font)[2]
    disegno.text(((L - larghezza) // 2, 360), nome, font=nome_font, fill=BIANCO)

    y = 470
    for riga in _a_capo(scena["sotto"], claim_font, L - 300):
        larghezza = disegno.textbbox((0, 0), riga, font=claim_font)[2]
        disegno.text(((L - larghezza) // 2, y), riga, font=claim_font, fill=(226, 243, 227))
        y += 48

    tela.save(destinazione)


def _voce(testo: str, destinazione: Path) -> None:
    from app.social.video_generator import voce_elevenlabs

    destinazione.with_suffix(".mp3").write_bytes(voce_elevenlabs(testo))
    subprocess.run([
        _ffmpeg(), "-v", "error", "-y", "-i", str(destinazione.with_suffix(".mp3")),
        "-ac", "1", "-ar", str(FREQUENZA_AUDIO), str(destinazione),
    ], check=True)


def _unisci_voci(pezzi: list[Path], destinazione: Path) -> list[float]:
    """Mette in fila le frasi con una pausa dopo ciascuna.

    Ritorna quanto dura ogni pezzo: e' anche quanto resta a schermo la sua
    scena, cosi' voce e immagini non si sfasano mai.
    """
    registrazioni = []
    for percorso in pezzi:
        with wave.open(str(percorso), "rb") as w:
            registrazioni.append(w.readframes(w.getnframes()))

    pause = [PAUSA_TRA_FRASI] * (len(registrazioni) - 1) + [CODA_FINALE]
    durate = []
    with wave.open(str(destinazione), "wb") as uscita:
        uscita.setnchannels(1)
        uscita.setsampwidth(2)
        uscita.setframerate(FREQUENZA_AUDIO)
        for registrazione, pausa in zip(registrazioni, pause):
            silenzio = b"\x00\x00" * int(FREQUENZA_AUDIO * pausa)
            uscita.writeframes(registrazione + silenzio)
            durate.append(len(registrazione) / 2 / FREQUENZA_AUDIO + pausa)
    return durate


def _scena_video(ffmpeg: str, scena: dict, durata: float, uscita: Path,
                 prima: bool = False) -> None:
    fotogrammi = max(1, round(durata * FPS))
    # La prima scena non sfuma in entrata: il primo fotogramma e' anche la
    # copertina che si vede quando il browser non fa partire il video da solo,
    # e un rettangolo nero in homepage sembra un errore di caricamento.
    apertura = "" if prima else "fade=t=in:st=0:d=0.35,"
    dissolvenza = f"{apertura}fade=t=out:st={max(0.1, durata - 0.35):.2f}:d=0.35"

    if scena["fonte"]["tipo"] == "repertorio":
        filtro = (
            f"[0:v]scale={L}:{A}:force_original_aspect_ratio=increase,crop={L}:{A},"
            f"fps={FPS},eq=brightness=-0.03:saturation=0.98[sfondo];"
            f"[sfondo][1:v]overlay=0:0,{dissolvenza},setsar=1,format=yuv420p[video]"
        )
        comando = [ffmpeg, "-v", "error", "-y",
                   "-stream_loop", "-1", "-i", str(scena["clip"]),
                   "-i", str(scena["sovrimpressione"]),
                   "-filter_complex", filtro, "-map", "[video]", "-an",
                   "-frames:v", str(fotogrammi), *CODIFICA, str(uscita)]
        subprocess.run(comando, check=True)
        return

    if scena["fonte"]["tipo"] == "marchio":
        # Fermo, ma con una dissolvenza in entrata: e' la firma finale
        subprocess.run([
            ffmpeg, "-v", "error", "-y", "-framerate", str(FPS), "-i", str(scena["immagine"]),
            "-vf", f"loop=loop=-1:size=1:start=0,{dissolvenza},setsar=1,format=yuv420p",
            "-frames:v", str(fotogrammi), *CODIFICA, str(uscita)],
            check=True)
        return

    # Schermata del sito: si avvicina piano, partendo dalla pagina intera.
    # Con un ritaglio fisso si perdevano la colonna dei filtri e mezzo elenco:
    # una schermata tagliata a meta' non spiega niente a chi non conosce il
    # sito. Lo zoom parte dall'immagine grande, quindi resta nitida.
    zoom = f"min({ZOOM_FINALE},1+{ZOOM_FINALE - 1}*on/{fotogrammi})"
    subprocess.run([
        ffmpeg, "-v", "error", "-y", "-framerate", str(FPS), "-i", str(scena["immagine"]),
        "-i", str(scena["sovrimpressione"]),
        "-filter_complex",
        (f"[0:v]loop=loop=-1:size=1:start=0,scale={L_SCATTO}:{A_SCATTO},"
         f"zoompan=z='{zoom}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
         f"s={L}x{A}:fps={FPS}[sfondo];"
         f"[sfondo][1:v]overlay=0:0,{dissolvenza},setsar=1,format=yuv420p[video]"),
        "-map", "[video]", "-an", "-frames:v", str(fotogrammi), *CODIFICA, str(uscita)],
        check=True)


def genera(sito: str, uscita: Path, cartella: Path) -> Path:
    from dotenv import load_dotenv

    load_dotenv(RADICE / ".env")
    ffmpeg = _ffmpeg()
    cartella.mkdir(parents=True, exist_ok=True)

    print("1/5  Voce")
    pezzi = []
    for i, scena in enumerate(SCENE):
        wav = cartella / f"voce{i}.wav"
        if not wav.exists():
            _voce(scena["voce"], wav)
        pezzi.append(wav)
    audio = cartella / "voce.wav"
    durate = _unisci_voci(pezzi, audio)
    print("     durate:", " ".join(f"{d:.1f}s" for d in durate),
          f"= {sum(durate):.1f}s")

    print("2/5  Schermate del sito")
    for i, scena in enumerate(SCENE):
        if scena["fonte"]["tipo"] != "schermata":
            continue
        immagine = cartella / f"schermata{i}.png"
        if not immagine.exists():
            _scatta(sito, scena["fonte"]["percorso"], immagine)
        scena["immagine"] = immagine
        print("     ", scena["fonte"]["percorso"])

    print("3/5  Clip di repertorio")
    from app.social import repertorio

    for i, scena in enumerate(SCENE):
        if scena["fonte"]["tipo"] != "repertorio":
            continue
        clip = cartella / f"clip{i}.mp4"
        if not clip.exists():
            trovata = repertorio.cerca_clip(
                scena["fonte"]["parole"], durate[i], cartella, f"clip{i}",
                orientamento="landscape")
            if not trovata:
                raise SystemExit(f"Nessuna clip per '{scena['fonte']['parole']}'")
            print("      ", scena["fonte"]["parole"], "->", trovata["autore"])
        scena["clip"] = clip

    print("4/5  Grafica")
    for i, scena in enumerate(SCENE):
        if scena["fonte"]["tipo"] == "marchio":
            immagine = cartella / f"marchio{i}.png"
            _marchio(scena, immagine)
            scena["immagine"] = immagine
        else:
            sovrimpressione = cartella / f"testo{i}.png"
            _sovrimpressione(scena, sovrimpressione)
            scena["sovrimpressione"] = sovrimpressione

    print("5/5  Montaggio")
    pezzi_video = []
    for i, scena in enumerate(SCENE):
        pezzo = cartella / f"scena{i}.mp4"
        _scena_video(ffmpeg, scena, durate[i], pezzo, prima=(i == 0))
        pezzi_video.append(pezzo)

    elenco = cartella / "scene.txt"
    elenco.write_text("".join(f"file '{p.as_posix()}'\n" for p in pezzi_video), encoding="utf-8")
    muto = cartella / "muto.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(elenco), "-c", "copy", str(muto)], check=True)
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(muto), "-i", str(audio),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", str(uscita)], check=True)

    peso = uscita.stat().st_size / 1024 / 1024
    print(f"\nFatto: {uscita}  ({peso:.1f} MB, {sum(durate):.1f}s)")
    return uscita


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sito", default="http://localhost:8099",
                        help="da dove riprendere le schermate vere dell'app")
    parser.add_argument("--uscita", default="video_homepage.mp4")
    parser.add_argument("--lavoro", default="lavorazione_video_homepage",
                        help="cartella dei file intermedi (riusati se gia' presenti)")
    argomenti = parser.parse_args()
    genera(argomenti.sito, Path(argomenti.uscita), Path(argomenti.lavoro))
