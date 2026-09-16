"""Video verticali: script -> voce ElevenLabs -> scene -> MP4.

Il video appartiene al contenuto e vale per tutte le sue uscite. Il test piu'
importante e' quello che monta un video vero con ffmpeg (voce e caricamento su
S3 simulati): verifica che esca proprio quello che TikTok accetta, H.264 + AAC
in 1080x1920 e almeno 3 secondi.
"""
import io
import json
import math
import re
import secrets
import struct
import subprocess
import wave

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import SocialContent, SocialDraft
from app.social import image_generator, publisher, video_generator as vg


@pytest.fixture
def pulizia():
    creati = []
    yield creati
    with Session(engine) as s:
        for content_id in creati:
            for uscita in s.exec(select(SocialDraft).where(SocialDraft.content_id == content_id)).all():
                s.delete(uscita)
            c = s.get(SocialContent, content_id)
            if c:
                s.delete(c)
        s.commit()


@pytest.fixture
def voce_configurata(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "chiave-di-prova")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voce-di-prova")


def _contenuto(pulizia, extra=None, caption="Caption di prova", tipo="video_completo", titolo=None):
    with Session(engine) as s:
        c = SocialContent(
            content_kind=tipo,
            caption_base=caption,
            source_title=titolo,
            extra_content=json.dumps(extra, ensure_ascii=False) if extra is not None else None,
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        pulizia.append(c.id)
        return c.id


def _wav_finto(secondi: float, frequenza: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frequenza)
        w.writeframes(b"".join(
            struct.pack("<h", int(2000 * math.sin(2 * math.pi * 220 * i / frequenza)))
            for i in range(int(frequenza * secondi))
        ))
    return buf.getvalue()


# --------------------------------------------------------------------------- script

def test_i_segmenti_del_modello_hanno_l_hook_in_testa():
    segmenti = vg.segmenti_dello_script({
        "hook": "Il capo non ti ascolta?",
        "script_segments": ["Primo consiglio: parla con i numeri.", "Secondo: scegli il momento."],
    })
    assert segmenti == ["Il capo non ti ascolta?", "Primo consiglio: parla con i numeri.", "Secondo: scegli il momento."]


def test_per_il_parlato_vince_l_hook_del_video():
    """Il carosello e il video hanno due hook diversi: a voce si legge il secondo."""
    segmenti = vg.segmenti_dello_script({
        "hook": "Hook del carosello",
        "hook_video": "Hook parlato",
        "script_segments": ["Una frase.", "Un'altra frase."],
    })
    assert segmenti[0] == "Hook parlato"


def test_l_hook_non_si_ripete_se_apre_gia_lo_script():
    segmenti = vg.segmenti_dello_script({
        "hook": "Il capo non ti ascolta?",
        "script_segments": ["Il capo non ti ascolta? Succede a tanti.", "Ecco cosa fare."],
    })
    assert segmenti[0] == "Il capo non ti ascolta? Succede a tanti."
    assert len(segmenti) == 2


def test_i_contenuti_vecchi_si_dividono_per_frasi_e_accorpano_quelle_brevi():
    segmenti = vg.segmenti_dello_script({
        "script": "Ecco come. Blocca due ore al giorno senza notifiche e difendile. "
                  "Poi imposta obiettivi realistici per la settimana. Fine.",
    })
    assert segmenti[0].startswith("Ecco come. Blocca due ore")
    assert all(len(s) >= 20 for s in segmenti)
    assert segmenti[-1].endswith("Fine.")


def test_un_contenuto_instagram_usa_le_slide_del_carosello():
    """Instagram non ha uno script: con solo l'hook usciva un video di una
    scena sola, quattro secondi e nessuna clip."""
    segmenti = vg.segmenti_dello_script({
        "hook": "Il colloquio in inglese fa paura?",
        "carousel_slides": ["Prepara tre risposte pronte.", "Registrati e riascoltati.", "Chiedi aiuto a un esperto."],
    })
    assert segmenti[0] == "Il colloquio in inglese fa paura?"
    assert len(segmenti) == 4


def test_troppi_segmenti_si_accorpano_senza_perdere_la_fine():
    segmenti = vg.segmenti_dello_script({"script_segments": [f"Frase numero {i}." for i in range(1, 15)]})
    assert len(segmenti) == vg.MAX_SEGMENTI
    assert segmenti[-1].endswith("Frase numero 14.")


def test_le_parole_chiave_seguono_le_frasi_anche_con_l_hook_aggiunto():
    extra = {
        "hook": "Il capo non ti ascolta?",
        "script_segments": ["Parla con i numeri.", "Scegli il momento giusto."],
        "scene_keywords": ["office meeting", "woman calendar"],
    }
    segmenti = vg.segmenti_dello_script(extra)
    assert len(segmenti) == 3
    assert vg.parole_per_scena(extra, segmenti) == ["", "office meeting", "woman calendar"]


def test_senza_parole_chiave_ogni_scena_resta_senza():
    assert vg.parole_per_scena({}, ["Uno", "Due"]) == ["", ""]


def test_quando_il_materiale_non_basta():
    assert vg._materiale_sufficiente(["a", "b"], ["x", "y"], True) is False, "due scene sono troppo poche"
    assert vg._materiale_sufficiente(["a", "b", "c"], ["", "", ""], True) is False, "senza scene niente repertorio"
    assert vg._materiale_sufficiente(["a", "b", "c"], ["", "", ""], False) is True, "il video di slide non le usa"
    assert vg._materiale_sufficiente(["a", "b", "c"], ["office", "", ""], True) is True


def test_senza_niente_da_dire_il_video_non_parte(monkeypatch, pulizia, voce_configurata):
    monkeypatch.setattr("app.social.content_generator.completa_script_video",
                        lambda titolo, caption, slide=None: {"script_segments": [], "scene_keywords": []})
    esito = vg.generate_video_for_content(_contenuto(pulizia, extra={}, caption=""))
    assert esito["ok"] is False
    assert "testo" in esito["message"]


def test_un_contenuto_inesistente():
    assert vg.generate_video_for_content(999999)["ok"] is False


# --------------------------------------------------------------------------- ElevenLabs

def test_senza_chiavi_dice_cosa_manca_prima_di_lavorare(monkeypatch, pulizia):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.setattr(vg, "_produci", lambda *a, **k: pytest.fail("non doveva generare nulla"))
    esito = vg.generate_video_for_content(
        _contenuto(pulizia, extra={"script_segments": ["Una frase.", "Due frasi.", "Tre frasi."],
                                   "scene_keywords": ["office", "desk", ""]}))
    assert esito["ok"] is False
    assert "ELEVENLABS_API_KEY" in esito["message"] and "ELEVENLABS_VOICE_ID" in esito["message"]


def test_la_richiesta_a_elevenlabs(monkeypatch, voce_configurata):
    chiamate = []

    class _Risposta:
        status_code = 200
        content = b"ID3audio"

    monkeypatch.setattr(vg.requests, "post", lambda url, **kw: chiamate.append((url, kw)) or _Risposta())
    assert vg.voce_elevenlabs("Ciao a tutti") == b"ID3audio"

    url, kw = chiamate[0]
    assert url == "https://api.elevenlabs.io/v1/text-to-speech/voce-di-prova"
    assert kw["headers"]["xi-api-key"] == "chiave-di-prova"
    assert kw["json"]["text"] == "Ciao a tutti"
    assert kw["json"]["model_id"] == "eleven_multilingual_v2"
    assert kw["params"]["output_format"].startswith("mp3_")


def test_un_rifiuto_di_elevenlabs_si_legge(monkeypatch, voce_configurata):
    class _Risposta:
        status_code = 401
        content = b""
        text = '{"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr(vg.requests, "post", lambda url, **kw: _Risposta())
    with pytest.raises(vg.VideoNonGenerato, match="401.*Invalid API key"):
        vg.voce_elevenlabs("Ciao")


# --------------------------------------------------------------------------- script ricavato

def test_lo_script_ricavato_resta_salvato_sul_contenuto(monkeypatch, pulizia):
    """Si paga il modello una volta: la generazione successiva lo ritrova."""
    monkeypatch.setattr("app.social.content_generator.completa_script_video",
                        lambda titolo, caption, slide=None: {
                            "script_segments": ["Uno.", "Due.", "Tre."],
                            "scene_keywords": ["office", "calendar", "handshake"],
                        })
    content_id = _contenuto(pulizia, extra={"hook": "Hook"})
    nuovo = vg._completa_materiale(content_id, {"hook": "Hook"})

    assert nuovo["script_segments"] == ["Uno.", "Due.", "Tre."]
    with Session(engine) as s:
        salvato = json.loads(s.get(SocialContent, content_id).extra_content)
    assert salvato["scene_keywords"] == ["office", "calendar", "handshake"]
    assert salvato["hook"] == "Hook", "il resto del contenuto non si perde"


def test_se_il_modello_non_risponde_si_va_avanti_lo_stesso(monkeypatch, pulizia):
    def esplode(titolo, caption, slide=None):
        raise RuntimeError("OpenAI giù")

    monkeypatch.setattr("app.social.content_generator.completa_script_video", esplode)
    content_id = _contenuto(pulizia, extra={"hook": "Hook"})
    assert vg._completa_materiale(content_id, {"hook": "Hook"}) == {"hook": "Hook"}


def test_un_post_di_immagini_diventa_un_video_con_piu_scene(monkeypatch, pulizia, voce_configurata):
    """Il caso segnalato: 'video completo' su un post Instagram dava 4 secondi e un'immagine."""
    monkeypatch.setattr("app.social.content_generator.completa_script_video",
                        lambda titolo, caption, slide=None: {
                            "script_segments": ["Il colloquio in inglese fa paura?", "Prepara tre risposte.",
                                                "Registrati e riascoltati.", "Trova un esperto su Ispiramy."],
                            "scene_keywords": ["job interview", "woman studying", "microphone recording", ""],
                        })
    prodotto = {}
    monkeypatch.setattr(vg, "_produci",
                        lambda content_id, segmenti, parole, ffmpeg, usa_repertorio=True:
                        prodotto.update(segmenti=segmenti, parole=parole, repertorio=usa_repertorio)
                        or ("https://esempio/v.mp4", 18.0, ["Autore"]))
    monkeypatch.setattr(vg, "ffmpeg_exe", lambda: "ffmpeg")

    content_id = _contenuto(pulizia, extra={"hook": "Il colloquio in inglese fa paura?"})
    esito = vg.generate_video_for_content(content_id, usa_repertorio=True)

    assert esito["ok"] is True
    assert len(prodotto["segmenti"]) == 4, "una scena sola era il bug"
    assert any(prodotto["parole"]), "senza parole chiave non ci sarebbero clip di repertorio"


def test_la_chiusura_non_usa_mai_il_repertorio(monkeypatch, pulizia, voce_configurata, tmp_path):
    """Marchio e invito finale li disegniamo noi: sul video della homepage
    proprio le scritte generate dall'AI sono uscite storpiate."""
    cercate = []
    monkeypatch.setattr(vg.repertorio, "cerca_clip",
                        lambda parole, durata, cartella, nome: cercate.append(parole) or None)
    monkeypatch.setattr(vg, "voce_elevenlabs", lambda testo: _wav_finto(0.6, vg.FREQUENZA_AUDIO))
    monkeypatch.setattr(vg, "_in_wav", lambda ff, sorgente, dest: dest.write_bytes(sorgente.read_bytes()))
    monkeypatch.setattr(vg, "_monta",
                        lambda ffmpeg, scene, durate, audio, uscita, musica=None: uscita.write_bytes(b"x"))
    monkeypatch.setattr(vg, "_carica_su_s3", lambda dati, key, ct: "https://esempio/v.mp4")

    vg._produci(1, ["Prima", "Seconda", "Chiusura"], ["a", "b", "c"], "ffmpeg")
    assert cercate == ["a", "b"], "la clip si cerca per tutte le scene tranne l'ultima"


# --------------------------------------------------------------------------- audio

def test_voce_e_scene_hanno_la_stessa_durata_anche_se_brevissima(tmp_path):
    """TikTok rifiuta i video sotto i 3 secondi: il silenzio finale allunga la CTA."""
    ffmpeg = pytest.importorskip("imageio_ffmpeg").get_ffmpeg_exe()
    corto = tmp_path / "corto.mp3"
    corto.write_bytes(_wav_finto(0.4))
    wav = tmp_path / "corto.wav"
    vg._in_wav(ffmpeg, corto, wav)

    uscita = tmp_path / "voce.wav"
    durate = vg._unisci_voci([wav, wav], uscita)
    with wave.open(str(uscita), "rb") as w:
        durata_audio = w.getnframes() / w.getframerate()

    assert sum(durate) >= vg.DURATA_MINIMA
    assert abs(sum(durate) - durata_audio) < 0.01


# --------------------------------------------------------------------------- montaggio

def test_il_montaggio_codifica_una_scena_alla_volta(monkeypatch, tmp_path):
    """Tutte le scene in un solo comando ffmpeg arrivavano a 3 GB di memoria:
    su Render (512 MB) il processo veniva ucciso e il video non usciva."""
    comandi = []
    monkeypatch.setattr(vg, "_esegui", lambda argomenti, cosa: comandi.append(argomenti))
    scene = [{"tipo": "slide", "clip": None, "immagine": tmp_path / f"s{i}.png"} for i in range(4)]
    durate = [1.4, 2.25, 1.9, 3.1]
    vg._monta("ffmpeg", scene, durate, tmp_path / "voce.wav", tmp_path / "video.mp4")

    scene_comandi, unione = comandi[:-1], comandi[-1]
    assert len(scene_comandi) == 4
    for comando in scene_comandi:
        assert comando.count("-i") == 1, "ogni scena deve avere un solo ingresso"
        assert "-loop" not in comando
        assert comando[comando.index("-vf") + 1].startswith("loop=")
        assert comando[comando.index("-threads") + 1] == "1"
    assert "concat" in unione
    assert unione[unione.index("-c:v") + 1] == "copy", "le scene si uniscono senza ricodificare"
    assert "-shortest" not in unione, "-shortest con l'AAC tagliava gli ultimi fotogrammi del video"
    fotogrammi = [int(c[c.index("-frames:v") + 1]) for c in scene_comandi]
    assert sum(fotogrammi) == round(sum(durate) * vg.FPS)


def test_le_scene_di_repertorio_usano_la_clip_con_la_grafica_sopra(monkeypatch, tmp_path):
    comandi = []
    monkeypatch.setattr(vg, "_esegui", lambda argomenti, cosa: comandi.append(argomenti))
    scene = [
        {"tipo": "repertorio", "clip": tmp_path / "c1.mp4", "immagine": tmp_path / "t1.png"},
        {"tipo": "slide", "clip": None, "immagine": tmp_path / "s2.png"},
    ]
    vg._monta("ffmpeg", scene, [2.0, 2.0], tmp_path / "voce.wav", tmp_path / "video.mp4")

    repertorio_cmd = comandi[0]
    assert "-stream_loop" in repertorio_cmd, "la clip si ripete se e' piu' corta della frase"
    filtro = repertorio_cmd[repertorio_cmd.index("-filter_complex") + 1]
    assert "overlay=0:0" in filtro and f"crop={vg.W}:{vg.H}" in filtro
    assert "-an" in repertorio_cmd, "l'audio della clip non deve coprire la voce"


def test_la_musica_va_sotto_la_voce(monkeypatch, tmp_path):
    comandi = []
    monkeypatch.setattr(vg, "_esegui", lambda argomenti, cosa: comandi.append(argomenti))
    scene = [{"tipo": "slide", "clip": None, "immagine": tmp_path / "s.png"}]
    vg._monta("ffmpeg", scene, [4.0], tmp_path / "voce.wav", tmp_path / "video.mp4", musica=tmp_path / "m.mp3")

    unione = comandi[-1]
    filtro = unione[unione.index("-filter_complex") + 1]
    assert f"volume={vg.VOLUME_MUSICA}" in filtro
    assert "amix=inputs=2" in filtro and "normalize=0" in filtro, "amix dimezzerebbe anche la voce"
    assert "afade=t=out" in filtro, "la musica sfuma alla fine"


def test_senza_musica_si_monta_solo_la_voce(monkeypatch, tmp_path):
    comandi = []
    monkeypatch.setattr(vg, "_esegui", lambda argomenti, cosa: comandi.append(argomenti))
    scene = [{"tipo": "slide", "clip": None, "immagine": tmp_path / "s.png"}]
    vg._monta("ffmpeg", scene, [4.0], tmp_path / "voce.wav", tmp_path / "video.mp4")
    assert "-filter_complex" not in comandi[-1]


def test_un_ffmpeg_ucciso_dal_sistema_lo_dice(monkeypatch):
    class _Esito:
        returncode = -9
        stderr = ""

    monkeypatch.setattr(vg.subprocess, "run", lambda *a, **k: _Esito())
    with pytest.raises(vg.VideoNonGenerato, match="memoria"):
        vg._esegui(["ffmpeg"], "Montaggio della scena 1")


# --------------------------------------------------------------------------- video vero

def _ffmpeg_o_salta():
    try:
        return vg.ffmpeg_exe()
    except vg.VideoNonGenerato:
        pytest.skip("ffmpeg non disponibile")


def test_esce_un_mp4_che_tiktok_accetta(monkeypatch, pulizia, voce_configurata, tmp_path):
    ffmpeg = _ffmpeg_o_salta()
    caricati = {}

    def carica(dati, key, content_type):
        percorso = tmp_path / "video.mp4"
        percorso.write_bytes(dati)
        caricati.update(key=key, content_type=content_type, percorso=percorso)
        return f"https://esempio/{key}"

    monkeypatch.setattr(vg, "voce_elevenlabs", lambda testo: _wav_finto(0.9))
    monkeypatch.setattr(vg, "_carica_su_s3", carica)

    content_id = _contenuto(pulizia, extra={
        "hook": "Il capo non ti ascolta?",
        "script_segments": ["Parla con i numeri, non con le impressioni."],
    })
    # Dal bottone: la stessa strada di "Genera immagini", smistata per tipo
    esito = image_generator.generate_media_for_content(content_id)
    assert esito["ok"] is True, esito["message"]

    assert caricati["content_type"] == "video/mp4"
    assert re.search(r"^social/content-\d+/\d+-video\.mp4$", caricati["key"])
    info = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(caricati["percorso"])],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    assert re.search(r"Video: h264.*1080x1920", info), info
    assert "Audio: aac" in info, info
    ore, minuti, secondi = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    assert int(ore) * 3600 + int(minuti) * 60 + float(secondi) >= 3

    with Session(engine) as s:
        assert s.get(SocialContent, content_id).media_urls == f"https://esempio/{caricati['key']}"


# --------------------------------------------------------------------------- pubblicazione

def test_i_video_su_facebook_e_instagram_escono_come_reel():
    """Un verticale 9:16 nel diario normale rende molto meno di un Reel, e
    senza dirlo la collocazione la sceglieva Post for Me."""
    video = ["https://ispiramy-images.s3.eu-north-1.amazonaws.com/social/content-9/x-video.mp4"]
    assert publisher._configurazione_piattaforma("facebook", video) == {"placement": "reels"}
    instagram = publisher._configurazione_piattaforma("instagram", video)
    assert instagram["placement"] == "reels"
    assert instagram["share_to_feed"] is True, "il Reel deve vedersi anche nel feed del profilo"


def test_le_foto_restano_nel_diario():
    foto = ["https://esempio/a.jpg", "https://esempio/b.jpg"]
    assert publisher._configurazione_piattaforma("facebook", foto) is None
    assert publisher._configurazione_piattaforma("instagram", foto) is None
    assert publisher._configurazione_piattaforma("linkedin", foto) is None


def test_un_carosello_con_dentro_un_video_non_diventa_un_reel():
    misto = ["https://esempio/a.jpg", "https://esempio/b.mp4"]
    assert publisher._configurazione_piattaforma("instagram", misto) is None


def test_tiktok_dichiara_l_ai_solo_per_i_video_generati_da_noi():
    nostro = "https://ispiramy-images.s3.eu-north-1.amazonaws.com/social/content-9/20260915120000-video.mp4"
    vecchio = "https://ispiramy-images.s3.eu-north-1.amazonaws.com/social/draft-8/20260912120000-video.mp4"
    girato = "https://ispiramy-images.s3.eu-north-1.amazonaws.com/uploads/intervista.mp4"
    assert publisher._configurazione_piattaforma("tiktok", [nostro])["is_ai_generated"] is True
    assert publisher._configurazione_piattaforma("tiktok", [vecchio])["is_ai_generated"] is True
    assert publisher._configurazione_piattaforma("tiktok", [girato])["is_ai_generated"] is False
    assert publisher._configurazione_piattaforma("tiktok", [nostro])["privacy_status"] == "public"
    assert "is_ai_generated" not in publisher._configurazione_piattaforma("instagram", [nostro])


# --------------------------------------------------------------------------- generatore e pagina

def test_gli_script_segments_del_modello_finiscono_nel_contenuto(pulizia):
    from app.social.content_generator import salva_contenuti

    domanda = secrets.randbelow(10**6) + 5 * 10**6
    pacchetto = {
        "source_question_id": domanda,
        "source_title": "Domanda di prova",
        "content": {"tiktok": {
            "hook": "Hook", "script": "Script intero.", "caption": "Caption",
            "script_segments": ["Hook", "Script intero."],
            "scene_keywords": ["office", "desk"],
        }},
    }
    assert salva_contenuti([pacchetto]) == 1
    with Session(engine) as s:
        contenuto = s.exec(select(SocialContent).where(SocialContent.source_question_id == domanda)).one()
        pulizia.append(contenuto.id)
        extra = json.loads(contenuto.extra_content)
    assert extra["script_segments"] == ["Hook", "Script intero."]
    assert extra["scene_keywords"] == ["office", "desk"]


def _pagina():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "admin" / "social.html").read_text(encoding="utf-8")


def test_la_pagina_ha_una_scheda_per_ogni_tipo():
    html = _pagina()
    assert "mostraScheda('{{ tipo }}', this)" in html
    assert 'data-tipo="{{ c.content_kind }}"' in html, "senza il tipo sulla card le schede non filtrano"


def test_la_pagina_mostra_i_video_come_video():
    html = _pagina()
    assert "<video" in html
    assert ".mp4" in html
