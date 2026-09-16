"""I tre tipi di contenuto e le uscite sui social.

Un contenuto e' un'idea con il suo media; le uscite sono le pubblicazioni sui
singoli social, ognuna col suo orario. Il tipo (immagini, post video, video
completo) decide come si genera il media e dove si puo' pubblicare: TikTok
accetta solo video.
"""
import json
import secrets

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import SocialContent, SocialDraft, User
from app.social import image_generator
from app.social.tipi import (
    IMMAGINI,
    VIDEO_COMPLETO,
    VIDEO_SLIDE,
    piattaforme_per_tipo,
    tipi_ammessi,
    tipo_di,
)
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit


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
def admin(csrf_client):
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        u = User(email=f"adm-{secrets.token_hex(4)}@test.local",
                 password_md5=hash_password(password), confirmed=1, user_type_id=3)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid, email = u.id, u.email
    assert csrf_client.post("/api/login", data={"email": email, "password": password}).status_code == 200
    yield csrf_client
    csrf_client.get("/logout")
    reset_rate_limit()
    with Session(engine) as s:
        u = s.get(User, uid)
        if u:
            s.delete(u)
            s.commit()


def _contenuto(pulizia, tipo=IMMAGINI, media=None):
    with Session(engine) as s:
        c = SocialContent(
            content_kind=tipo,
            caption_base="Caption",
            captions=json.dumps({"facebook": "Testo Facebook", "instagram": "Testo Instagram",
                                 "tiktok": "Testo TikTok"}, ensure_ascii=False),
            media_urls=media,
            extra_content=json.dumps({"hook": "Hook", "script_segments": ["Uno", "Due"]}),
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        pulizia.append(c.id)
        return c.id


def _uscita(content_id, platform="instagram", stato="draft"):
    with Session(engine) as s:
        u = SocialDraft(content_id=content_id, platform=platform, caption="x", status=stato)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _tipo_salvato(content_id):
    with Session(engine) as s:
        return s.get(SocialContent, content_id).content_kind


# --------------------------------------------------------------------------- tipi

def test_dove_si_puo_pubblicare_ogni_tipo():
    assert piattaforme_per_tipo(IMMAGINI) == ("facebook", "instagram"), "TikTok accetta solo video"
    assert "tiktok" in piattaforme_per_tipo(VIDEO_SLIDE)
    assert "tiktok" in piattaforme_per_tipo(VIDEO_COMPLETO)


def test_le_uscite_vecchie_partono_dal_tipo_della_piattaforma():
    """Le bozze nate prima dei contenuti: TikTok video, le altre immagini."""
    assert tipo_di(SocialDraft(platform="instagram", caption="x")) == IMMAGINI
    assert tipo_di(SocialDraft(platform="tiktok", caption="x")) == VIDEO_COMPLETO
    assert tipo_di(SocialDraft(platform="tiktok", caption="x", content_kind=VIDEO_SLIDE)) == VIDEO_SLIDE


def test_su_tiktok_non_si_offrono_i_post_di_sole_immagini():
    assert tipi_ammessi("tiktok") == (VIDEO_SLIDE, VIDEO_COMPLETO)
    assert IMMAGINI in tipi_ammessi("instagram")


# --------------------------------------------------------------------------- generazione

def test_ogni_stile_prende_la_sua_strada(monkeypatch, pulizia):
    chiamate = []
    monkeypatch.setattr(image_generator, "generate_carousel_for_content",
                        lambda content_id, use_ai_cover=True: chiamate.append("carosello") or {"ok": True, "message": ""})
    monkeypatch.setattr(image_generator, "generate_image_for_content",
                        lambda content_id, use_ai_cover=True: chiamate.append("immagine") or {"ok": True, "message": ""})
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_content",
                        lambda content_id, usa_repertorio=True: chiamate.append(f"video:{usa_repertorio}") or {"ok": True, "message": ""})

    con_slide = _contenuto(pulizia)
    with Session(engine) as s:
        c = s.get(SocialContent, con_slide)
        c.extra_content = json.dumps({"hook": "Hook", "carousel_slides": ["Uno", "Due"]})
        s.add(c)
        s.commit()
    senza_slide = _contenuto(pulizia)

    image_generator.generate_media_for_content(con_slide, stile=IMMAGINI)
    image_generator.generate_media_for_content(senza_slide, stile=IMMAGINI)
    image_generator.generate_media_for_content(senza_slide, stile=VIDEO_SLIDE)
    image_generator.generate_media_for_content(senza_slide, stile=VIDEO_COMPLETO)

    assert chiamate == ["carosello", "immagine", "video:False", "video:True"]


def test_lo_stile_generato_resta_scritto(monkeypatch, pulizia):
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_content",
                        lambda content_id, usa_repertorio=True: {"ok": True, "message": "fatto"})
    content_id = _contenuto(pulizia)
    image_generator.generate_media_for_content(content_id, stile=VIDEO_SLIDE)
    assert _tipo_salvato(content_id) == VIDEO_SLIDE


def test_una_generazione_fallita_non_sposta_il_contenuto_di_scheda(monkeypatch, pulizia):
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_content",
                        lambda content_id, usa_repertorio=True: {"ok": False, "message": "niente voce"})
    content_id = _contenuto(pulizia, tipo=IMMAGINI)
    image_generator.generate_media_for_content(content_id, stile=VIDEO_COMPLETO)
    assert _tipo_salvato(content_id) == IMMAGINI


def test_senza_stile_si_usa_quello_del_contenuto(monkeypatch, pulizia):
    chiamate = []
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_content",
                        lambda content_id, usa_repertorio=True: chiamate.append(usa_repertorio) or {"ok": True, "message": ""})
    content_id = _contenuto(pulizia, tipo=VIDEO_SLIDE)
    image_generator.generate_media_for_content(content_id)
    assert chiamate == [False], "un contenuto marcato come post video non deve tornare al repertorio"


# --------------------------------------------------------------------------- cambio di tipo

def test_si_sposta_un_contenuto_di_scheda_senza_rigenerare(admin, pulizia):
    """Il caso della bozza 9: un video di slide finito fra i video completi."""
    content_id = _contenuto(pulizia, tipo=VIDEO_COMPLETO)
    risposta = admin.post(f"/admin/social/contents/{content_id}/kind", json={"stile": VIDEO_SLIDE})
    assert risposta.status_code == 200
    assert "Post video" in risposta.json()["message"]
    assert _tipo_salvato(content_id) == VIDEO_SLIDE


def test_il_cambio_di_tipo_aggiorna_anche_le_uscite(admin, pulizia):
    content_id = _contenuto(pulizia, tipo=VIDEO_COMPLETO)
    uscita_id = _uscita(content_id, "instagram")
    admin.post(f"/admin/social/contents/{content_id}/kind", json={"stile": VIDEO_SLIDE})
    with Session(engine) as s:
        assert s.get(SocialDraft, uscita_id).content_kind == VIDEO_SLIDE


def test_non_si_passa_alle_immagini_con_un_uscita_su_tiktok(admin, pulizia):
    """Cambiare tipo la farebbe fallire piu' tardi, in silenzio."""
    content_id = _contenuto(pulizia, tipo=VIDEO_COMPLETO)
    _uscita(content_id, "tiktok")
    risposta = admin.post(f"/admin/social/contents/{content_id}/kind", json={"stile": IMMAGINI})
    assert risposta.status_code == 400
    assert "TikTok" in risposta.json()["message"]
    assert _tipo_salvato(content_id) == VIDEO_COMPLETO


def test_il_cambio_di_tipo_rifiuta_i_valori_inventati(admin, pulizia):
    content_id = _contenuto(pulizia)
    assert admin.post(f"/admin/social/contents/{content_id}/kind", json={"stile": "boh"}).status_code == 400


def test_il_cambio_di_tipo_su_un_contenuto_inesistente(admin):
    assert admin.post("/admin/social/contents/999999/kind", json={"stile": IMMAGINI}).status_code == 404


# --------------------------------------------------------------------------- uscite

def test_lo_stesso_contenuto_esce_su_piu_social_a_orari_diversi(admin, pulizia):
    """È il punto della struttura: un video, tre uscite, nessuna rigenerazione."""
    content_id = _contenuto(pulizia, tipo=VIDEO_COMPLETO, media="https://esempio/video.mp4")

    for piattaforma, quando in (("tiktok", "2026-09-20T18:00"),
                                ("instagram", "2026-09-21T09:00"),
                                ("facebook", "2026-09-22T19:30")):
        risposta = admin.post(f"/admin/social/contents/{content_id}/publications",
                              json={"platform": piattaforma, "scheduled_at": quando})
        assert risposta.status_code == 200, risposta.text

    with Session(engine) as s:
        uscite = s.exec(select(SocialDraft).where(SocialDraft.content_id == content_id)).all()
    assert sorted(u.platform for u in uscite) == ["facebook", "instagram", "tiktok"]
    assert all(u.media_urls == "https://esempio/video.mp4" for u in uscite), "il media è lo stesso per tutte"
    assert {u.scheduled_at.strftime("%d/%m %H:%M") for u in uscite} == {"20/09 18:00", "21/09 09:00", "22/09 19:30"}


def test_ogni_uscita_parte_col_testo_scritto_per_quel_social(admin, pulizia):
    content_id = _contenuto(pulizia, tipo=VIDEO_SLIDE)
    admin.post(f"/admin/social/contents/{content_id}/publications", json={"platform": "tiktok"})
    with Session(engine) as s:
        uscita = s.exec(select(SocialDraft).where(SocialDraft.content_id == content_id)).one()
    assert uscita.caption == "Testo TikTok"


def test_un_post_di_immagini_non_si_pubblica_su_tiktok(admin, pulizia):
    content_id = _contenuto(pulizia, tipo=IMMAGINI)
    risposta = admin.post(f"/admin/social/contents/{content_id}/publications", json={"platform": "tiktok"})
    assert risposta.status_code == 400
    assert "TikTok" in risposta.json()["message"]


def test_una_data_sbagliata_non_crea_l_uscita(admin, pulizia):
    content_id = _contenuto(pulizia)
    risposta = admin.post(f"/admin/social/contents/{content_id}/publications",
                          json={"platform": "instagram", "scheduled_at": "domani mattina"})
    assert risposta.status_code == 400
    with Session(engine) as s:
        assert s.exec(select(SocialDraft).where(SocialDraft.content_id == content_id)).all() == []


# --------------------------------------------------------------------------- pagina

def test_la_pagina_offre_il_selettore_del_tipo_e_l_aggiunta_di_uscite():
    import io
    import os
    percorso = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "templates", "admin", "social.html")
    html = io.open(percorso, encoding="utf-8").read()
    assert "cambiaTipo({{ c.id }}, this.value)" in html
    assert "aggiungiUscita({{ c.id }})" in html
    assert "piattaforme_per_tipo(c.content_kind)" in html, "le piattaforme offerte dipendono dal tipo"
