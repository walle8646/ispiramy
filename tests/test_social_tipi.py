"""I tre tipi di contenuto: immagini, video da immagini, video completo.

Sono tre strade distinte anche nella generazione: lo stile scelto col bottone
decide come si produce il media e resta scritto sulla bozza, perche' e' anche
la scheda dell'admin in cui comparira'.
"""
import json
import secrets

import pytest
from sqlmodel import Session

from app.database import engine
from app.models import SocialDraft, User
from app.social import image_generator
from app.social.tipi import IMMAGINI, VIDEO_COMPLETO, VIDEO_SLIDE, tipi_ammessi, tipo_di
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit


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


def _draft(pulizia, platform="instagram", **campi):
    with Session(engine) as s:
        d = SocialDraft(platform=platform, caption="Caption", status="draft",
                        extra_content=json.dumps({"hook": "Hook", "script_segments": ["Uno", "Due"]}), **campi)
        s.add(d)
        s.commit()
        s.refresh(d)
        pulizia.append(d.id)
        return d.id


def _tipo_salvato(draft_id):
    with Session(engine) as s:
        return s.get(SocialDraft, draft_id).content_kind


# --------------------------------------------------------------------------- tipi

def test_le_bozze_nuove_partono_dal_tipo_della_piattaforma():
    assert tipo_di(SocialDraft(platform="instagram", caption="x")) == IMMAGINI
    assert tipo_di(SocialDraft(platform="facebook", caption="x")) == IMMAGINI
    assert tipo_di(SocialDraft(platform="tiktok", caption="x")) == VIDEO_COMPLETO


def test_il_tipo_salvato_vince_sul_predefinito():
    """Un video fatto di slide resta fra i post anche se e' un TikTok."""
    draft = SocialDraft(platform="tiktok", caption="x", content_kind=VIDEO_SLIDE)
    assert tipo_di(draft) == VIDEO_SLIDE


def test_un_valore_sconosciuto_non_manda_la_bozza_in_una_scheda_inesistente():
    assert tipo_di(SocialDraft(platform="facebook", caption="x", content_kind="strano")) == IMMAGINI


def test_su_tiktok_non_si_offrono_i_post_di_sole_immagini():
    assert tipi_ammessi("tiktok") == (VIDEO_SLIDE, VIDEO_COMPLETO)
    assert IMMAGINI in tipi_ammessi("instagram")


# --------------------------------------------------------------------------- generazione

def test_ogni_stile_prende_la_sua_strada(monkeypatch, pulizia):
    chiamate = []
    monkeypatch.setattr(image_generator, "generate_carousel_for_draft",
                        lambda draft_id, use_ai_cover=True: chiamate.append("carosello") or {"ok": True, "message": ""})
    monkeypatch.setattr(image_generator, "generate_image_for_draft",
                        lambda draft_id, use_ai_cover=True: chiamate.append("immagine") or {"ok": True, "message": ""})
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_draft",
                        lambda draft_id, usa_repertorio=True: chiamate.append(f"video:{usa_repertorio}") or {"ok": True, "message": ""})

    instagram = _draft(pulizia, "instagram")
    facebook = _draft(pulizia, "facebook")

    image_generator.generate_media_for_draft(instagram, stile=IMMAGINI)
    image_generator.generate_media_for_draft(facebook, stile=IMMAGINI)
    image_generator.generate_media_for_draft(facebook, stile=VIDEO_SLIDE)
    image_generator.generate_media_for_draft(facebook, stile=VIDEO_COMPLETO)

    assert chiamate == ["carosello", "immagine", "video:False", "video:True"]


def test_lo_stile_generato_resta_scritto_sulla_bozza(monkeypatch, pulizia):
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_draft",
                        lambda draft_id, usa_repertorio=True: {"ok": True, "message": "fatto"})
    draft_id = _draft(pulizia, "facebook")
    assert _tipo_salvato(draft_id) is None

    image_generator.generate_media_for_draft(draft_id, stile=VIDEO_SLIDE)
    assert _tipo_salvato(draft_id) == VIDEO_SLIDE


def test_una_generazione_fallita_non_sposta_la_bozza_di_scheda(monkeypatch, pulizia):
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_draft",
                        lambda draft_id, usa_repertorio=True: {"ok": False, "message": "niente voce"})
    draft_id = _draft(pulizia, "instagram")
    image_generator.generate_media_for_draft(draft_id, stile=VIDEO_COMPLETO)
    assert _tipo_salvato(draft_id) is None


def test_senza_stile_si_usa_quello_della_bozza(monkeypatch, pulizia):
    chiamate = []
    import app.social.video_generator as vg
    monkeypatch.setattr(vg, "generate_video_for_draft",
                        lambda draft_id, usa_repertorio=True: chiamate.append(usa_repertorio) or {"ok": True, "message": ""})
    draft_id = _draft(pulizia, "tiktok", content_kind=VIDEO_SLIDE)
    image_generator.generate_media_for_draft(draft_id)
    assert chiamate == [False], "il TikTok gia' marcato come post video non deve tornare al repertorio"


# --------------------------------------------------------------------------- route

def test_la_route_rifiuta_uno_stile_inventato(admin, pulizia):
    draft_id = _draft(pulizia, "instagram")
    risposta = admin.post(f"/admin/social/drafts/{draft_id}/generate-media", json={"stile": "cinema"})
    assert risposta.status_code == 400
    assert "non valido" in risposta.json()["message"]


def test_si_sposta_una_bozza_di_scheda_senza_rigenerare(admin, pulizia):
    """La bozza 9 era un video di slide finito fra i video completi: doveva
    bastare spostarla, non rifare il video (e ripagare la voce)."""
    draft_id = _draft(pulizia, "tiktok")
    assert _tipo_salvato(draft_id) is None

    risposta = admin.post(f"/admin/social/drafts/{draft_id}/kind", json={"stile": VIDEO_SLIDE})
    assert risposta.status_code == 200
    assert "Post video" in risposta.json()["message"]
    assert _tipo_salvato(draft_id) == VIDEO_SLIDE


def test_non_si_sposta_un_tiktok_fra_i_post_di_immagini(admin, pulizia):
    draft_id = _draft(pulizia, "tiktok")
    risposta = admin.post(f"/admin/social/drafts/{draft_id}/kind", json={"stile": IMMAGINI})
    assert risposta.status_code == 400
    assert "TikTok" in risposta.json()["message"]
    assert _tipo_salvato(draft_id) is None


def test_lo_spostamento_rifiuta_i_tipi_inventati(admin, pulizia):
    draft_id = _draft(pulizia, "instagram")
    assert admin.post(f"/admin/social/drafts/{draft_id}/kind", json={"stile": "boh"}).status_code == 400


def test_lo_spostamento_di_una_bozza_inesistente(admin):
    assert admin.post("/admin/social/drafts/999999/kind", json={"stile": IMMAGINI}).status_code == 404


def test_la_pagina_offre_il_selettore_del_tipo():
    import io
    import os
    percorso = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "templates", "admin", "social.html")
    html = io.open(percorso, encoding="utf-8").read()
    assert "cambiaTipo({{ d.id }}, this.value)" in html


def test_la_route_passa_lo_stile_al_generatore(monkeypatch, admin, pulizia):
    ricevuti = {}
    monkeypatch.setattr(image_generator, "generate_media_for_draft",
                        lambda draft_id, use_ai_cover=True, stile=None: ricevuti.update(id=draft_id, stile=stile) or {"ok": True, "message": "ok"})
    draft_id = _draft(pulizia, "instagram")
    risposta = admin.post(f"/admin/social/drafts/{draft_id}/generate-media", json={"stile": VIDEO_COMPLETO})
    assert risposta.status_code == 200
    assert ricevuti == {"id": draft_id, "stile": VIDEO_COMPLETO}
