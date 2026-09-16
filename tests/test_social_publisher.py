"""Regressioni sulla pubblicazione dei contenuti social.

Tre bug, verificati prima della correzione:
1. il campo "Media URL" era un <input> a riga singola: il browser elimina gli
   a-capo dal valore, e ogni salvataggio (compreso quello automatico fatto da
   "Approva") incollava gli URL del carosello in un'unica stringa non valida;
2. un post rifiutato dal social non si poteva più ripubblicare: il publisher
   ritrovava il vecchio post e lo riportava in 'failed' con l'errore di prima;
3. un post programmato con un requisito mancante restava 'approved' per
   sempre, ritentato ogni 5 minuti, senza alcun errore visibile.
"""
import secrets
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import SocialDraft, User
from app.social import publisher
from app.social.publisher import parse_media_urls
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit

URLS = [f"https://ispiramy-images.s3.eu-north-1.amazonaws.com/social/d/slide-{i}.png" for i in range(3)]


# ---------- fixture ----------

@pytest.fixture
def admin(csrf_client):
    """Amministratore loggato sul client condiviso; logout e pulizia alla fine."""
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        u = User(email=f"adm-{secrets.token_hex(4)}@test.local",
                 password_md5=hash_password(password), confirmed=1, user_type_id=3)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid, email = u.id, u.email
    r = csrf_client.post("/api/login", data={"email": email, "password": password})
    assert r.status_code == 200, r.text
    yield csrf_client
    csrf_client.get("/logout")
    reset_rate_limit()
    with Session(engine) as s:
        u = s.get(User, uid)
        if u:
            s.delete(u)
            s.commit()


@pytest.fixture
def bozze():
    """Crea bozze su richiesta e le rimuove tutte alla fine."""
    creati = []

    def _crea(**campi):
        base = dict(platform="instagram", caption="Caption di prova", status="draft")
        base.update(campi)
        with Session(engine) as s:
            d = SocialDraft(**base)
            s.add(d)
            s.commit()
            s.refresh(d)
            creati.append(d.id)
            return d.id

    yield _crea
    with Session(engine) as s:
        for did in creati:
            d = s.get(SocialDraft, did)
            if d:
                s.delete(d)
        s.commit()


@pytest.fixture
def post_for_me(monkeypatch):
    """Post for Me simulato. `stato` guida le risposte, `chiamate` le registra."""
    stato = {"post_esistente": None, "esito": None, "account": True}
    chiamate = []

    def finta_api(method, path, body=None):
        chiamate.append((method, path, body))
        if path.startswith("/social-accounts"):
            dati = [{"platform": p, "status": "connected", "id": f"acc-{p}"}
                    for p in ("facebook", "instagram", "tiktok")] if stato["account"] else []
            return {"data": dati}
        if path.startswith("/social-posts?external_id"):
            return {"data": [stato["post_esistente"]] if stato["post_esistente"] else []}
        if path.startswith("/social-post-results"):
            return {"data": [stato["esito"]] if stato["esito"] else []}
        if method == "POST" and path == "/social-posts":
            return {"id": f"post-{len(chiamate)}"}
        return {"data": []}

    monkeypatch.setattr(publisher, "_api", finta_api)
    return stato, chiamate


def _get(did):
    with Session(engine) as s:
        return s.get(SocialDraft, did)


# ---------- 1. campo media ----------

class TestParserMedia:
    def test_uno_per_riga(self):
        assert parse_media_urls("\n".join(URLS)) == URLS

    def test_recupera_gli_url_incollati(self):
        """È la forma in cui il vecchio campo salvava i caroselli."""
        assert parse_media_urls("".join(URLS)) == URLS

    def test_spazi_virgole_e_righe_vuote(self):
        assert parse_media_urls(f" {URLS[0]} ,\n\n{URLS[1]};{URLS[2]}  ") == URLS

    def test_vuoto(self):
        assert parse_media_urls("") == []
        assert parse_media_urls(None) == []


class TestCodaProgrammata:
    def test_la_coda_ignora_le_uscite_non_approvate(self, monkeypatch, bozze):
        """Una data su un'uscita non approvata non pubblica niente."""
        pubblicati = []
        monkeypatch.setattr(publisher, "publish_draft",
                            lambda draft_id: pubblicati.append(draft_id) or {"ok": True})
        monkeypatch.setattr(publisher, "check_publishing_results", lambda: None)

        ieri = datetime.now() - timedelta(days=1)
        fermo = bozze(platform="facebook", status="draft", scheduled_at=ieri)
        pronto = bozze(platform="facebook", status="approved", scheduled_at=ieri)

        publisher.process_social_queue()
        assert pronto in pubblicati
        assert fermo not in pubblicati


class TestCampoMediaInDashboard:
    def test_la_dashboard_usa_un_area_di_testo(self, admin):
        """Il media sta sul contenuto: niente <input> a riga singola, perderebbe gli a-capo."""
        from app.models import SocialContent

        with Session(engine) as s:
            contenuto = SocialContent(caption_base="Testo", media_urls="\n".join(URLS))
            s.add(contenuto)
            s.commit()
            s.refresh(contenuto)
            cid = contenuto.id
        try:
            html = admin.get("/admin/social").text
            assert f'<input type="text" class="media-input" id="media-{cid}"' not in html
            assert f'id="media-{cid}"' in html
            blocco = html.split(f'id="media-{cid}"', 1)[1].split("</textarea>", 1)[0]
            assert all(u in blocco for u in URLS)
        finally:
            with Session(engine) as s:
                s.delete(s.get(SocialContent, cid))
                s.commit()

    def test_salvare_url_incollati_li_rimette_uno_per_riga(self, admin, bozze):
        did = bozze()
        r = admin.post(f"/admin/social/drafts/{did}", json={"media_urls": "".join(URLS)})
        assert r.status_code == 200, r.text
        assert _get(did).media_urls.splitlines() == URLS


# ---------- 2. ritento dei post falliti ----------

class TestRitento:
    def test_post_rifiutato_dal_social_si_puo_ripubblicare(self, admin, bozze, post_for_me):
        stato, chiamate = post_for_me
        did = bozze(platform="facebook", status="failed",
                    postforme_post_id="post-vecchio", error="caption troppo lunga")
        # Post for Me conosce ancora il vecchio post, con esito negativo
        stato["post_esistente"] = {"id": "post-vecchio"}
        stato["esito"] = {"success": False, "error": "caption troppo lunga"}

        r = admin.post(f"/admin/social/drafts/{did}/status", json={"action": "approve"})
        assert r.status_code == 200, r.text
        d = _get(did)
        assert d.status == "approved"
        assert d.publish_attempt == 1
        assert d.postforme_post_id is None

        # Il nuovo tentativo non trova post con il NUOVO external_id...
        stato["post_esistente"] = None
        stato["esito"] = None
        esito = publisher.publish_draft(did)

        assert esito["ok"], esito
        creazioni = [c for c in chiamate if c[0] == "POST" and c[1] == "/social-posts"]
        assert len(creazioni) == 1, "doveva essere creato un post nuovo"
        assert creazioni[0][2]["external_id"] == f"ispiramy-draft-{did}-r1"
        assert _get(did).status == "publishing"

    def test_errore_in_invio_mantiene_l_idempotenza(self, admin, bozze, post_for_me):
        """Se l'invio era fallito prima di arrivare al social (nessun post id),
        il ritento deve riusare lo stesso external_id: se Post for Me l'aveva
        creato comunque, va ritrovato e non duplicato."""
        stato, chiamate = post_for_me
        did = bozze(platform="facebook", status="failed", error="timeout di rete")

        r = admin.post(f"/admin/social/drafts/{did}/status", json={"action": "approve"})
        assert r.status_code == 200
        assert _get(did).publish_attempt == 0

        stato["post_esistente"] = {"id": "creato-nonostante-il-timeout"}
        publisher.publish_draft(did)

        assert not [c for c in chiamate if c[0] == "POST" and c[1] == "/social-posts"], \
            "ha creato un doppione invece di ritrovare il post"
        cercati = [c[1] for c in chiamate if c[1].startswith("/social-posts?external_id")]
        assert cercati and cercati[0].endswith(f"ispiramy-draft-{did}")


# ---------- 3. requisiti mancanti ----------

class TestRequisiti:
    def test_instagram_senza_immagini_non_si_approva(self, admin, bozze):
        did = bozze(platform="instagram")
        r = admin.post(f"/admin/social/drafts/{did}/status", json={"action": "approve"})
        assert r.status_code == 400
        assert "immagini" in r.json()["message"]
        assert _get(did).status == "draft"

    def test_tiktok_senza_video_non_si_approva(self, admin, bozze):
        did = bozze(platform="tiktok")
        r = admin.post(f"/admin/social/drafts/{did}/status", json={"action": "approve"})
        assert r.status_code == 400
        assert "video" in r.json()["message"]

    def test_facebook_solo_testo_si_approva(self, admin, bozze):
        did = bozze(platform="facebook")
        r = admin.post(f"/admin/social/drafts/{did}/status", json={"action": "approve"})
        assert r.status_code == 200
        assert _get(did).status == "approved"

    def test_programmato_non_pubblicabile_diventa_fallito_e_visibile(self, bozze, post_for_me):
        stato, _ = post_for_me
        stato["account"] = False  # nessun account collegato su Post for Me
        passato = datetime.now() - timedelta(minutes=10)
        did = bozze(platform="facebook", status="approved", scheduled_at=passato)

        publisher.process_social_queue()

        d = _get(did)
        assert d.status == "failed"
        assert d.error and "Nessun account" in d.error

    def test_programmato_nel_futuro_non_viene_toccato(self, bozze, post_for_me):
        futuro = datetime.now() + timedelta(days=1)
        did = bozze(platform="facebook", status="approved", scheduled_at=futuro)
        publisher.process_social_queue()
        assert _get(did).status == "approved"
