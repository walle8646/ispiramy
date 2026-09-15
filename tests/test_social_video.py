"""Video verticali per TikTok: script -> voce ElevenLabs -> slide -> MP4.

Il test piu' importante e' quello che monta un video vero con ffmpeg (la voce
e il caricamento su S3 sono simulati): verifica che esca proprio quello che
TikTok accetta, H.264 + AAC in 1080x1920 e almeno 3 secondi.
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
from app.models import SocialDraft
from app.social import image_generator, publisher, video_generator as vg


@pytest.fixture
def pulizia():
    creati = []
    yield creati
    with Session(engine) as s:
        for draft_id in creati:
            d = s.get(SocialDraft, draft_id)
            if d:
                s.delete(d)
        s.commit()


@pytest.fixture
def voce_configurata(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "chiave-di-prova")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voce-di-prova")


def _draft(pulizia, extra=None, platform="tiktok", stato="draft"):
    with Session(engine) as s:
        d = SocialDraft(
            platform=platform,
            caption="Caption TikTok #prova",
            extra_content=json.dumps(extra, ensure_ascii=False) if extra is not None else None,
            status=stato,
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        pulizia.append(d.id)
        return d.id


def _wav_finto(secondi: float, frequenza: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frequenza)
        campioni = int(frequenza * secondi)
        w.writeframes(b"".join(
            struct.pack("<h", int(2000 * math.sin(2 * math.pi * 220 * i / frequenza)))
            for i in range(campioni)
        ))
    return buf.getvalue()


# --------------------------------------------------------------------------- script

def test_i_segmenti_del_modello_hanno_l_hook_in_testa():
    segmenti = vg.segmenti_dello_script({
        "hook": "Il capo non ti ascolta?",
        "script_segments": ["Primo consiglio: parla con i numeri.", "Secondo: scegli il momento."],
    })
    assert segmenti == ["Il capo non ti ascolta?", "Primo consiglio: parla con i numeri.", "Secondo: scegli il momento."]


def test_l_hook_non_si_ripete_se_apre_gia_lo_script():
    segmenti = vg.segmenti_dello_script({
        "hook": "Il capo non ti ascolta?",
        "script_segments": ["Il capo non ti ascolta? Succede a tanti.", "Ecco cosa fare."],
    })
    assert segmenti[0] == "Il capo non ti ascolta? Succede a tanti."
    assert len(segmenti) == 2


def test_i_draft_vecchi_si_dividono_per_frasi_e_accorpano_quelle_brevi():
    """Prima di script_segments c'era solo il blocco unico."""
    segmenti = vg.segmenti_dello_script({
        "script": "Ecco come. Blocca due ore al giorno senza notifiche e difendile. "
                  "Poi imposta obiettivi realistici per la settimana. Fine.",
    })
    assert segmenti[0].startswith("Ecco come. Blocca due ore")
    assert all(len(s) >= 20 for s in segmenti)
    assert segmenti[-1].endswith("Fine.")


def test_troppi_segmenti_si_accorpano_senza_perdere_la_fine():
    segmenti = vg.segmenti_dello_script({"script_segments": [f"Frase numero {i}." for i in range(1, 15)]})
    assert len(segmenti) == vg.MAX_SEGMENTI
    assert segmenti[-1].endswith("Frase numero 14.")


def test_senza_script_non_parte_niente(pulizia, voce_configurata):
    esito = vg.generate_video_for_draft(_draft(pulizia, extra={}))
    assert esito["ok"] is False
    assert "script" in esito["message"]


def test_un_draft_pubblicato_non_si_rigenera(pulizia, voce_configurata):
    esito = vg.generate_video_for_draft(_draft(pulizia, extra={"script": "Una frase abbastanza lunga per il video."}, stato="published"))
    assert esito["ok"] is False


# --------------------------------------------------------------------------- ElevenLabs

def test_senza_chiavi_dice_cosa_manca_prima_di_lavorare(monkeypatch, pulizia):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.setattr(vg, "_produci", lambda *a, **k: pytest.fail("non doveva generare nulla"))
    esito = vg.generate_video_for_draft(_draft(pulizia, extra={"script": "Una frase abbastanza lunga per il video."}))
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

    draft_id = _draft(pulizia, extra={
        "hook": "Il capo non ti ascolta?",
        "script_segments": ["Parla con i numeri, non con le impressioni."],
    })
    # Dal bottone: la stessa strada di "Genera grafica", smistata per piattaforma
    esito = image_generator.generate_media_for_draft(draft_id)
    assert esito["ok"] is True, esito["message"]

    assert caricati["content_type"] == "video/mp4"
    assert re.search(r"^social/draft-\d+/\d+-video\.mp4$", caricati["key"])
    info = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(caricati["percorso"])],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    assert re.search(r"Video: h264.*1080x1920", info), info
    assert "Audio: aac" in info, info
    ore, minuti, secondi = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    assert int(ore) * 3600 + int(minuti) * 60 + float(secondi) >= 3

    with Session(engine) as s:
        assert s.get(SocialDraft, draft_id).media_urls == f"https://esempio/{caricati['key']}"


# --------------------------------------------------------------------------- pubblicazione

def test_tiktok_dichiara_l_ai_solo_per_i_video_generati_da_noi():
    nostro = "https://ispiramy-images.s3.eu-north-1.amazonaws.com/social/draft-9/20260915120000-video.mp4"
    girato = "https://ispiramy-images.s3.eu-north-1.amazonaws.com/uploads/intervista.mp4"
    assert publisher._configurazione_piattaforma("tiktok", [nostro])["is_ai_generated"] is True
    assert publisher._configurazione_piattaforma("tiktok", [girato])["is_ai_generated"] is False
    assert publisher._configurazione_piattaforma("tiktok", [nostro])["privacy_status"] == "public"
    assert publisher._configurazione_piattaforma("instagram", [nostro]) is None


def test_la_configurazione_tiktok_arriva_a_post_for_me(monkeypatch, pulizia):
    inviati = []
    monkeypatch.setattr(publisher, "get_connected_accounts", lambda: {"tiktok": {"id": "spc_tiktok"}})
    monkeypatch.setattr(publisher, "_find_existing_post", lambda ext: None)
    monkeypatch.setattr(publisher, "check_publishing_results", lambda: None)
    monkeypatch.setattr(publisher, "_api", lambda metodo, percorso, corpo=None: inviati.append(corpo) or {"id": "sp_1"})

    draft_id = _draft(pulizia, extra={}, stato="approved")
    with Session(engine) as s:
        d = s.get(SocialDraft, draft_id)
        d.media_urls = "https://ispiramy-images.s3.eu-north-1.amazonaws.com/social/draft-1/20260915120000-video.mp4"
        s.add(d)
        s.commit()

    assert publisher.publish_draft(draft_id)["ok"] is True
    assert inviati[0]["platform_configurations"] == {
        "tiktok": {
            "privacy_status": "public",
            "allow_comment": True,
            "is_ai_generated": True,
            "disclose_your_brand": True,
        }
    }


# --------------------------------------------------------------------------- generatore e pagina

def test_gli_script_segments_del_modello_finiscono_nel_draft(pulizia):
    from app.social.content_generator import save_packages_as_drafts

    domanda = secrets.randbelow(10**6) + 5 * 10**6
    pacchetto = {
        "source_question_id": domanda,
        "source_title": "Domanda di prova",
        "content": {"tiktok": {
            "hook": "Hook", "script": "Script intero.", "caption": "Caption",
            "script_segments": ["Hook", "Script intero."],
        }},
    }
    assert save_packages_as_drafts([pacchetto]) == 1
    with Session(engine) as s:
        draft = s.exec(select(SocialDraft).where(SocialDraft.source_question_id == domanda)).one()
        pulizia.append(draft.id)
        assert json.loads(draft.extra_content)["script_segments"] == ["Hook", "Script intero."]


def _pagina():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "admin" / "social.html").read_text(encoding="utf-8")


def test_la_pagina_ha_il_bottone_genera_video_per_tiktok():
    html = _pagina()
    assert "d.platform == 'tiktok'" in html
    assert "Genera video" in html


def test_la_pagina_mostra_i_video_come_video():
    html = _pagina()
    assert "<video" in html
    assert ".mp4" in html
