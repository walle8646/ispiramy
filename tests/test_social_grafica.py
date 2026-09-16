"""Generazione della grafica di un contenuto social.

Il media appartiene al contenuto, non alla singola uscita: si genera una volta
e vale per TikTok, per i Reels di Instagram e per quelli di Facebook. Le uscite
gia' pubblicate pero' tengono il media con cui sono uscite.
"""
import json

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import SocialContent, SocialDraft
from app.social import image_generator


@pytest.fixture
def pulizia():
    creati = {"contenuti": [], "uscite": []}
    yield creati
    with Session(engine) as s:
        for uscita_id in creati["uscite"]:
            u = s.get(SocialDraft, uscita_id)
            if u:
                s.delete(u)
        for content_id in creati["contenuti"]:
            c = s.get(SocialContent, content_id)
            if c:
                s.delete(c)
        s.commit()


def _contenuto(pulizia, extra=None, caption="Testo di prova.", titolo=None, tipo="immagini", media=None):
    with Session(engine) as s:
        c = SocialContent(
            content_kind=tipo,
            caption_base=caption,
            source_title=titolo,
            media_urls=media,
            extra_content=json.dumps(extra, ensure_ascii=False) if extra else None,
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        pulizia["contenuti"].append(c.id)
        return c.id


def _uscita(pulizia, content_id, platform="instagram", stato="draft", media=None):
    with Session(engine) as s:
        u = SocialDraft(content_id=content_id, platform=platform, status=stato,
                        caption="Caption", media_urls=media)
        s.add(u)
        s.commit()
        s.refresh(u)
        pulizia["uscite"].append(u.id)
        return u.id


# --------------------------------------------------------------- testo in evidenza

def test_il_testo_grande_e_l_hook_se_c_e():
    contenuto = SocialContent(extra_content=json.dumps({"hook": "Tre errori nel colloquio"}),
                              source_title="Titolo diverso", caption_base="Caption")
    assert image_generator._testo_in_evidenza(contenuto) == "Tre errori nel colloquio"


def test_senza_hook_si_usa_la_prima_slide():
    contenuto = SocialContent(extra_content=json.dumps({"carousel_slides": ["Il problema", "Il consiglio"]}))
    assert image_generator._testo_in_evidenza(contenuto) == "Il problema"


def test_senza_slide_si_usa_il_titolo_della_domanda():
    contenuto = SocialContent(source_title="Come trovare lavoro a Milano", caption_base="Caption")
    assert image_generator._testo_in_evidenza(contenuto) == "Come trovare lavoro a Milano"


def test_ultima_spiaggia_la_prima_frase_senza_hashtag_ne_link():
    contenuto = SocialContent(
        caption_base="Cambiare lavoro spaventa. Ma si può fare.\n\n👉 https://ispiramy.com/consultants #lavoro")
    assert image_generator._testo_in_evidenza(contenuto) == "Cambiare lavoro spaventa."


# --------------------------------------------------------------- generazione

def test_le_immagini_vengono_caricate_in_jpeg(monkeypatch, pulizia):
    """Il PNG e' il motivo per cui Instagram rifiutava i caroselli."""
    caricate = []
    monkeypatch.setattr(image_generator, "_upload_immagine",
                        lambda img, key: caricate.append((img, key)) or f"https://esempio/{key}")

    content_id = _contenuto(pulizia, titolo="Come chiedere un aumento")
    esito = image_generator.generate_media_for_content(content_id)

    assert esito["ok"] is True, esito["message"]
    assert len(caricate) == 1, "senza slide si fa una sola immagine"
    immagine, chiave = caricate[0]
    assert immagine.size == (image_generator.W, image_generator.H)
    assert chiave.startswith(f"social/content-{content_id}/") and chiave.endswith("-post.jpg")


def test_con_le_slide_si_fa_il_carosello(monkeypatch, pulizia):
    caricate = []
    monkeypatch.setattr(image_generator, "_upload_immagine",
                        lambda img, key: caricate.append(key) or f"https://esempio/{key}")

    content_id = _contenuto(pulizia, extra={"hook": "Hook", "carousel_slides": ["Uno", "Due", "Tre"]})
    esito = image_generator.generate_media_for_content(content_id)

    assert esito["ok"] is True
    assert len(caricate) == 4, "copertina più tre slide"
    assert all("-slide-" in chiave for chiave in caricate)


def test_un_contenuto_senza_testo_non_produce_niente(pulizia):
    content_id = _contenuto(pulizia, caption="")
    esito = image_generator.generate_image_for_content(content_id, use_ai_cover=False)
    assert esito["ok"] is False
    assert "testo" in esito["message"]


def test_un_contenuto_inesistente():
    assert image_generator.generate_media_for_content(999999)["ok"] is False


# --------------------------------------------------------------- media condiviso

def test_il_media_generato_arriva_a_tutte_le_uscite_non_ancora_partite(monkeypatch, pulizia):
    monkeypatch.setattr(image_generator, "_upload_immagine", lambda img, key: f"https://esempio/{key}")
    content_id = _contenuto(pulizia, titolo="Una domanda")
    da_fare = _uscita(pulizia, content_id, "instagram", stato="draft")
    approvata = _uscita(pulizia, content_id, "facebook", stato="approved")
    gia_uscita = _uscita(pulizia, content_id, "tiktok", stato="published", media="https://vecchio/video.mp4")

    assert image_generator.generate_media_for_content(content_id)["ok"] is True

    with Session(engine) as s:
        nuovo = s.get(SocialContent, content_id).media_urls
        assert s.get(SocialDraft, da_fare).media_urls == nuovo
        assert s.get(SocialDraft, approvata).media_urls == nuovo
        # Cambiare il media di un post gia' uscito direbbe il falso su cosa e' stato pubblicato
        assert s.get(SocialDraft, gia_uscita).media_urls == "https://vecchio/video.mp4"


def test_il_tipo_generato_resta_scritto_sul_contenuto_e_sulle_uscite(monkeypatch, pulizia):
    monkeypatch.setattr(image_generator, "_upload_immagine", lambda img, key: f"https://esempio/{key}")
    content_id = _contenuto(pulizia, titolo="Una domanda", tipo="video_completo")
    uscita_id = _uscita(pulizia, content_id, "instagram")

    image_generator.generate_media_for_content(content_id, stile="immagini")

    with Session(engine) as s:
        assert s.get(SocialContent, content_id).content_kind == "immagini"
        assert s.get(SocialDraft, uscita_id).content_kind == "immagini"


# --------------------------------------------------------------- pagina

def test_la_pagina_offre_i_tre_modi_di_generare():
    """Tre bottoni distinti: immagini, video da immagini, video completo."""
    import io
    import os
    percorso = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "templates", "admin", "social.html")
    html = io.open(percorso, encoding="utf-8").read()
    assert "generateMedia({{ c.id }}, '{{ tipo }}')" in html
    for etichetta in ("Genera immagini", "Genera video da immagini", "Genera video completo"):
        assert etichetta in html
