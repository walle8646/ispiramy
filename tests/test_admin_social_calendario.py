"""Le due schede della pagina social e il calendario condiviso.

Il calendario deve mostrare sia i post programmati sia quelli gia' usciti, con
gli orari in ora italiana: scheduled_at e' gia' italiano, published_at e'
salvato in UTC, e mostrarli insieme senza convertire darebbe due ore di
differenza fra un post programmato e lo stesso post una volta pubblicato.
"""
import json
import secrets
from datetime import datetime

import pytest
from sqlmodel import Session

from app.database import engine
from app.models import SocialDraft, User
from app.routes.admin_social import _voci_calendario
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit


@pytest.fixture
def admin(csrf_client):
    """Amministratore loggato sul client condiviso."""
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        u = User(email=f"adm-{secrets.token_hex(4)}@test.local",
                 password_md5=hash_password(password), confirmed=1, user_type_id=3)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid, email = u.id, u.email
    risposta = csrf_client.post("/api/login", data={"email": email, "password": password})
    assert risposta.status_code == 200, risposta.text
    yield csrf_client
    csrf_client.get("/logout")
    reset_rate_limit()
    with Session(engine) as s:
        u = s.get(User, uid)
        if u:
            s.delete(u)
            s.commit()


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


def _draft(platform="instagram", **campi):
    return SocialDraft(platform=platform, caption="Caption di prova", **campi)


def test_le_bozze_programmate_finiscono_nel_calendario():
    quando = datetime(2026, 9, 18, 15, 45)
    voci = _voci_calendario([_draft(scheduled_at=quando, status="approved", source_title="Burnout")])
    assert len(voci) == 1
    assert voci[0]["giorno"] == "2026-09-18"
    assert voci[0]["ora"] == "15:45"
    assert voci[0]["status"] == "approved"
    assert voci[0]["titolo"] == "Burnout"


def test_i_pubblicati_si_vedono_all_ora_italiana():
    """published_at e' in UTC: a settembre l'Italia e' due ore avanti."""
    voci = _voci_calendario([
        _draft(status="published", published_at=datetime(2026, 9, 18, 13, 45), source_title="Uscito")
    ])
    assert voci[0]["ora"] == "15:45"
    assert voci[0]["giorno"] == "2026-09-18"


def test_le_bozze_senza_data_restano_fuori():
    assert _voci_calendario([_draft(status="draft")]) == []


def test_le_voci_sono_in_ordine_di_tempo():
    voci = _voci_calendario([
        _draft(scheduled_at=datetime(2026, 9, 20, 9, 0), source_title="dopo"),
        _draft(scheduled_at=datetime(2026, 9, 18, 18, 0), source_title="prima"),
        _draft(scheduled_at=datetime(2026, 9, 18, 8, 0), source_title="primissimo"),
    ])
    assert [v["titolo"] for v in voci] == ["primissimo", "prima", "dopo"]


def test_senza_titolo_si_usa_la_caption():
    voci = _voci_calendario([_draft(scheduled_at=datetime(2026, 9, 18, 10, 0))])
    assert voci[0]["titolo"].startswith("Caption")


def test_la_pagina_mostra_il_calendario_e_le_due_schede(admin, pulizia):
    with Session(engine) as s:
        d = _draft(platform="tiktok", scheduled_at=datetime(2026, 9, 18, 15, 45), status="approved",
                   source_title="Un video di prova")
        s.add(d)
        s.commit()
        s.refresh(d)
        pulizia.append(d.id)

    pagina = admin.get("/admin/social")
    assert pagina.status_code == 200
    html = pagina.text
    assert 'id="datiCalendario"' in html
    for tipo in ("immagini", "video_slide", "video_completo"):
        assert f"mostraScheda('{tipo}'" in html

    # il blocco JSON deve essere leggibile: e' il motivo per cui i dati non
    # stanno dentro il JavaScript
    grezzo = html.split('id="datiCalendario">')[1].split("</script>")[0]
    voci = json.loads(grezzo)
    assert any(v["titolo"] == "Un video di prova" and v["ora"] == "15:45" for v in voci)


def test_le_card_dichiarano_la_piattaforma(admin, pulizia):
    """Senza data-platform le schede non saprebbero cosa mostrare."""
    with Session(engine) as s:
        d = _draft(platform="tiktok", status="draft")
        s.add(d)
        s.commit()
        s.refresh(d)
        pulizia.append(d.id)
    html = admin.get("/admin/social").text
    assert f'data-platform="tiktok" data-tipo="video_completo" id="draft-{d.id}"' in html
