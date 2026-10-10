"""Profilo, rientro in call e allegati della chat.

- Un errore nel caricare /profile sloggava l'utente (session.clear()): Stripe
  rimanda proprio lì dopo il pagamento, quindi chi prenotava si ritrovava fuori.
- Dopo la recensione la call è chiusa, ma il profilo continuava a proporre
  "Entra in call": ora le API lo dicono esplicitamente (closed_by_review).
- Gli allegati si potevano solo scaricare (link S3 diretto salvato come
  "attachment"); ora passano da un endpoint che controlla il partecipante e
  firma un link per vederli o scaricarli.
"""
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, CallMessage, Review, User
from app.routes import booking as booking_routes
from app.routes.booking import _chiave_allegato_chat, _nome_da_chiave, _nome_file_sicuro
from app.utils.orari import now_italy_naive
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit

BUCKET = "ispiramy-images"


def _url(booking_id, nome="7_20260911_101500_123456_foto.jpg", bucket=BUCKET):
    return f"https://{bucket}.s3.eu-west-1.amazonaws.com/call-attachments/{booking_id}/{nome}"


@pytest.fixture
def cliente_in_call(csrf_client, monkeypatch):
    """Cliente loggato con una consulenza in corso adesso."""
    monkeypatch.setenv("S3_BUCKET_NAME", BUCKET)
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    adesso = now_italy_naive()
    with Session(engine) as s:
        cliente = User(email=f"sess-c-{secrets.token_hex(4)}@test.local",
                       password_md5=hash_password(password), confirmed=1, nome="Anna", cognome="Neri")
        consulente = User(email=f"sess-p-{secrets.token_hex(4)}@test.local",
                          password_md5="x", confirmed=1, nome="Luca", cognome="Bianchi")
        s.add(cliente)
        s.add(consulente)
        s.commit()
        s.refresh(cliente)
        s.refresh(consulente)
        b = Booking(
            client_user_id=cliente.id, consultant_user_id=consulente.id,
            booking_date=datetime(adesso.year, adesso.month, adesso.day),
            start_time="00:00", end_time="23:59", duration_minutes=60,
            status="confirmed", payment_status="held",
            client_joined_at=adesso, consultant_joined_at=adesso,
        )
        s.add(b)
        s.commit()
        s.refresh(b)
        ids = {"booking": b.id, "cliente": cliente.id, "consulente": consulente.id}

    r = csrf_client.post("/api/login", data={"email": cliente.email, "password": password})
    assert r.status_code == 200, r.text
    yield csrf_client, ids

    csrf_client.get("/logout")
    reset_rate_limit()
    with Session(engine) as s:
        for m in s.exec(select(CallMessage).where(CallMessage.booking_id == ids["booking"])).all():
            s.delete(m)
        for r in s.exec(select(Review).where(Review.booking_id == ids["booking"])).all():
            s.delete(r)
        s.commit()
        b = s.get(Booking, ids["booking"])
        if b:
            s.delete(b)
        s.commit()
        for uid in (ids["cliente"], ids["consulente"]):
            u = s.get(User, uid)
            if u:
                s.delete(u)
        s.commit()


def _lascia_recensione(ids):
    with Session(engine) as s:
        s.add(Review(booking_id=ids["booking"], reviewer_user_id=ids["cliente"],
                     consultant_user_id=ids["consulente"],
                     rating_helpful=5, rating_prepared=5, rating_communication=5))
        s.commit()


class TestProfiloNonSlogga:
    def test_errore_nel_profilo_non_chiude_la_sessione(self, cliente_in_call, monkeypatch):
        client, _ = cliente_in_call

        def rotto(_user):
            raise RuntimeError("query fallita")

        monkeypatch.setattr("app.routes.user_profile.has_payment_method", rotto)
        r = client.get("/profile", follow_redirects=False)
        assert r.status_code == 500
        assert "Sei ancora connesso" in r.text

        monkeypatch.undo()
        r = client.get("/profile", follow_redirects=False)
        assert r.status_code == 200, "dopo l'errore l'utente risulta sloggato"


class TestRientroDopoRecensione:
    def test_prima_della_recensione_si_puo_entrare(self, cliente_in_call):
        client, ids = cliente_in_call
        stato = client.get(f"/api/booking/{ids['booking']}/call-status-extended").json()
        assert stato["closed_by_review"] is False
        assert stato["is_expired"] is False

        prossimi = client.get("/api/booking/upcoming").json()["bookings"]
        card = next(b for b in prossimi if b["id"] == ids["booking"])
        assert card["closed_by_review"] is False

    def test_dopo_la_recensione_il_profilo_lo_sa(self, cliente_in_call):
        client, ids = cliente_in_call
        _lascia_recensione(ids)

        stato = client.get(f"/api/booking/{ids['booking']}/call-status-extended").json()
        assert stato["closed_by_review"] is True
        assert stato["can_resume"] is False

        prossimi = client.get("/api/booking/upcoming").json()["bookings"]
        card = next((b for b in prossimi if b["id"] == ids["booking"]), None)
        if card is not None:
            assert card["closed_by_review"] is True

    def test_la_pagina_call_rimanda_al_profilo_con_avviso(self, cliente_in_call):
        client, ids = cliente_in_call
        _lascia_recensione(ids)
        r = client.get(f"/booking/call/{ids['booking']}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/profile?call_closed=review"


class TestChiaveAllegato:
    @pytest.fixture(autouse=True)
    def _bucket(self, monkeypatch):
        monkeypatch.setenv("S3_BUCKET_NAME", BUCKET)

    def test_allegato_della_chat_valido(self):
        assert _chiave_allegato_chat(12, _url(12)) == "call-attachments/12/7_20260911_101500_123456_foto.jpg"

    @pytest.mark.parametrize("url", [
        _url(13),                                   # altra consulenza
        _url(12, bucket="altro-bucket"),            # altro bucket
        "https://ispiramy-images.s3.eu-west-1.amazonaws.com/avatars/1.jpg",
        "https://ispiramy-images.s3.eu-west-1.amazonaws.com/call-attachments/12/../13/x.jpg",
        "https://ispiramy-images.s3.eu-west-1.amazonaws.com/call-attachments/12/",
        "http://ispiramy-images.s3.eu-west-1.amazonaws.com/call-attachments/12/x.jpg",
        "javascript:alert(1)",
        "",
        None,
    ])
    def test_rifiutati(self, url):
        assert _chiave_allegato_chat(12, url) is None

    def test_nome_file_sicuro(self):
        assert _nome_file_sicuro('fattura "è" 2026.pdf', "pdf") == "fattura_e_2026.pdf"
        assert _nome_file_sicuro("", "png") == "file.png"
        assert _nome_file_sicuro("../../etc/passwd", "txt") == "etc_passwd.txt"

    def test_nome_da_chiave(self):
        assert _nome_da_chiave("call-attachments/12/7_20260911_101500_123456_mia_foto.jpg") == "mia_foto.jpg"


class _S3Finto:
    def __init__(self):
        self.params = None

    def generate_presigned_url(self, op, Params, ExpiresIn):
        assert op == "get_object"
        self.params = Params
        return f"https://s3.example/firmato?key={Params['Key']}"


class TestEndpointAllegato:
    def test_visualizza_immagine_inline(self, cliente_in_call, monkeypatch):
        client, ids = cliente_in_call
        s3 = _S3Finto()
        monkeypatch.setattr(booking_routes, "_get_chat_s3_client", lambda: s3)

        r = client.get(f"/api/booking/{ids['booking']}/chat/attachment",
                       params={"url": _url(ids["booking"]), "mode": "view"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://s3.example/firmato")
        assert s3.params["ResponseContentDisposition"] == "inline"
        assert s3.params["ResponseContentType"] == "image/jpeg"

    def test_scarica_come_allegato(self, cliente_in_call, monkeypatch):
        client, ids = cliente_in_call
        s3 = _S3Finto()
        monkeypatch.setattr(booking_routes, "_get_chat_s3_client", lambda: s3)

        url = _url(ids["booking"], "7_20260911_101500_123456_preventivo.docx")
        r = client.get(f"/api/booking/{ids['booking']}/chat/attachment",
                       params={"url": url, "mode": "view"}, follow_redirects=False)
        # un .docx non si può mostrare: si scarica anche se chiesto "view"
        assert r.status_code == 302
        assert s3.params["ResponseContentDisposition"].startswith('attachment; filename="preventivo.docx"')
        assert "ResponseContentType" not in s3.params

    def test_allegato_di_altra_consulenza(self, cliente_in_call, monkeypatch):
        client, ids = cliente_in_call
        monkeypatch.setattr(booking_routes, "_get_chat_s3_client", lambda: _S3Finto())
        r = client.get(f"/api/booking/{ids['booking']}/chat/attachment",
                       params={"url": _url(ids["booking"] + 1000)}, follow_redirects=False)
        assert r.status_code == 404

    def test_chi_non_partecipa_non_apre(self, cliente_in_call, monkeypatch):
        client, ids = cliente_in_call
        monkeypatch.setattr(booking_routes, "_get_chat_s3_client", lambda: _S3Finto())
        with Session(engine) as s:
            b = s.get(Booking, ids["booking"])
            b.client_user_id = ids["consulente"]  # il cliente loggato non è più partecipante
            s.add(b)
            s.commit()
        r = client.get(f"/api/booking/{ids['booking']}/chat/attachment",
                       params={"url": _url(ids["booking"])}, follow_redirects=False)
        assert r.status_code == 403


class TestInvioAllegati:
    def test_solo_allegati_della_chat(self, cliente_in_call):
        client, ids = cliente_in_call
        buono = {"url": _url(ids["booking"]), "filename": "foto.jpg", "file_size": 1234,
                 "file_type": "image/jpeg", "extra": "<script>"}
        r = client.post(f"/api/booking/{ids['booking']}/chat/send", json={
            "message": "",
            "attachments": [
                buono,
                {"url": "javascript:alert(1)", "filename": "x.jpg"},
                {"url": "https://evil.example/x.jpg", "filename": "x.jpg"},
                "non-un-oggetto",
            ],
        })
        assert r.status_code == 200, r.text
        allegati = r.json()["attachments"]
        assert allegati == [{"url": buono["url"], "filename": "foto.jpg",
                             "file_size": 1234, "file_type": "image/jpeg"}]

    def test_solo_allegati_non_validi_e_nessun_testo(self, cliente_in_call):
        client, ids = cliente_in_call
        r = client.post(f"/api/booking/{ids['booking']}/chat/send", json={
            "message": "", "attachments": [{"url": "https://evil.example/x.jpg"}],
        })
        assert r.status_code == 400


class TestTokenDellaCall:
    """Il token si ottiene solo quando entrambi hanno premuto "Partecipa".

    Chi apriva la call per primo vedeva "Impossibile avviare la chiamata:
    Failed to get Agora token", un messaggio che non diceva nulla. Il motivo
    vero arriva dal server e la pagina ora aspetta l'altra persona.
    """

    def test_se_l_altro_non_e_entrato_il_motivo_e_esplicito(self, cliente_in_call):
        client, ids = cliente_in_call
        with Session(engine) as s:
            b = s.get(Booking, ids["booking"])
            b.consultant_joined_at = None
            s.add(b)
            s.commit()

        r = client.get(f"/api/booking/{ids['booking']}/agora-token")
        assert r.status_code == 403
        assert "Entrambi" in r.json()["detail"], "la pagina riconosce l'attesa da questo testo"

    def test_la_pagina_aspetta_invece_di_fallire(self):
        pagina = (Path(__file__).resolve().parent.parent / "app" / "templates" / "call.html").read_text(encoding="utf-8-sig")
        assert "throw new Error('Failed to get Agora token')" not in pagina
        assert "call.attesa_ingresso" in pagina
        assert "remoteUserName" in pagina
        assert "In attesa che ${remoteUserName} entri nella chiamata" not in pagina


class TestDataDellaConsulenza:
    """booking_date e' DATE su PostgreSQL e DATETIME su SQLite.

    Il codice della call faceva booking.booking_date.date(): in produzione
    quel valore e' gia' un date e l'endpoint del token rispondeva 500, cioe'
    "Impossibile avviare la chiamata". Nei test su SQLite non si vedeva, quindi
    qui si verifica la funzione con tutte le forme e si vieta il vecchio modo.
    """

    def test_accetta_date_datetime_e_stringa(self):
        from datetime import date as _date

        from app.utils.orari import data_consulenza

        atteso = _date(2026, 9, 12)
        assert data_consulenza(_date(2026, 9, 12)) == atteso
        assert data_consulenza(datetime(2026, 9, 12, 15, 30)) == atteso
        assert data_consulenza("2026-09-12") == atteso
        assert data_consulenza("2026-09-12 15:30:00") == atteso

    def test_nessuno_chiama_piu_date_su_booking_date(self):
        radice = Path(__file__).resolve().parent.parent / "app"
        colpevoli = []
        for f in radice.rglob("*.py"):
            for n, riga in enumerate(f.read_text(encoding="utf-8-sig").splitlines(), 1):
                if "booking_date.date()" in riga:
                    colpevoli.append(f"{f.relative_to(radice.parent)}:{n}")
        assert not colpevoli, (
            "su PostgreSQL booking_date e' un date: usare data_consulenza(). " + ", ".join(colpevoli)
        )


class TestEffettiSfondo:
    """Gli effetti sfondo non partivano proprio: due dipendenze esterne rotte.

    - il modulo su download.agora.io, indirizzo senza versione, rispondeva 404
      ("Modulo effetti sfondo non caricato");
    - l'immagine di sfondo su S3 non manda gli header CORS, e serve
      crossOrigin='anonymous' per darla al processore video.
    """

    def _pagina(self):
        return (Path(__file__).resolve().parent.parent / "app" / "templates" / "call.html").read_text(encoding="utf-8-sig")

    def test_il_modulo_ha_una_versione_fissata(self):
        pagina = self._pagina()
        assert "download.agora.io/sdk/release/agora-extension-virtual-background.js" not in pagina
        assert "agora-extension-virtual-background@" in pagina

    def test_lo_sfondo_ispiramy_e_servito_da_noi(self):
        pagina = self._pagina()
        assert "ispiramy-images.s3" not in pagina, "immagine su S3: niente CORS, il browser la rifiuta"
        assert "statico('call-background.png')" in pagina
        assert (Path(__file__).resolve().parent.parent / "app" / "static" / "call-background.png").exists()

    def test_si_puo_scegliere_un_immagine_dal_computer(self):
        pagina = self._pagina()
        assert 'id="bgFileInput"' in pagina
        assert 'data-bg="custom"' in pagina
        assert "impostaImmagineUtente" in pagina

    def test_chi_invia_vede_la_propria_anteprima(self):
        """Il messaggio ottimistico non ha ancora l'indirizzo dell'allegato:
        senza anteprima locale chi inviava vedeva un'immagine rotta."""
        pagina = self._pagina()
        assert "anteprimaLocale" in pagina
        assert "URL.createObjectURL(f)" in pagina
        assert "URL.revokeObjectURL" in pagina


class TestLinkEAvvisiInChat:
    """Due cose che mancavano nella chat: i link e il perché di un invio fallito."""

    def _file(self, nome):
        return (Path(__file__).resolve().parent.parent / "app" / "templates" / nome).read_text(encoding="utf-8-sig")

    def test_i_link_si_scrivono_con_la_funzione_comune(self):
        """Un solo punto che escapa il testo e trasforma in link solo http(s):
        se ognuno lo rifacesse a modo suo, uno dei tre diventerebbe un XSS."""
        base = self._file("base.html")
        assert "window.testoConLink" in base
        assert 'rel="noopener noreferrer nofollow"' in base
        for pagina in ("call.html", "chat.html", "chat_widget.html"):
            assert "testoConLink(" in self._file(pagina), f"{pagina} non rende cliccabili i link"

    def test_un_allegato_rifiutato_lo_dice(self):
        """Con la moderazione attiva l'immagine viene bloccata: prima il
        messaggio restava per sempre fermo su "invio..."."""
        call = self._file("call.html")
        assert "allegatiRifiutati" in call
        assert "mostraAvvisoChat" in call
        assert "rimuoviMessaggioProvvisorio" in call
        assert "chat-avviso" in call


class TestSfondoVerificato:
    """Lo sfondo lo vede l'altra persona: passa dagli stessi controlli."""

    def test_immagine_non_adatta_rifiutata(self, cliente_in_call, monkeypatch):
        client, ids = cliente_in_call

        async def blocca(url):
            return {"approved": False, "reason": "L'immagine contiene contenuti non appropriati."}

        monkeypatch.setattr("app.utils.ai_service.modera_immagine_chat", blocca)
        r = client.post(f"/api/booking/{ids['booking']}/background/check",
                        json={"image": "data:image/jpeg;base64,AAAA"})
        assert r.status_code == 200
        assert r.json()["approved"] is False
        assert "non appropriati" in r.json()["reason"]

    def test_immagine_adatta_approvata(self, cliente_in_call, monkeypatch):
        client, ids = cliente_in_call

        async def consenti(url):
            return {"approved": True, "reason": ""}

        monkeypatch.setattr("app.utils.ai_service.modera_immagine_chat", consenti)
        r = client.post(f"/api/booking/{ids['booking']}/background/check",
                        json={"image": "data:image/jpeg;base64,AAAA"})
        assert r.status_code == 200 and r.json()["approved"] is True

    def test_serve_un_immagine(self, cliente_in_call):
        client, ids = cliente_in_call
        r = client.post(f"/api/booking/{ids['booking']}/background/check",
                        json={"image": "https://esempio.it/foto.jpg"})
        assert r.status_code == 400

    def test_chi_non_partecipa_non_puo_usarlo(self, cliente_in_call):
        """Non deve diventare un servizio di moderazione per chiunque."""
        client, ids = cliente_in_call
        with Session(engine) as s:
            b = s.get(Booking, ids["booking"])
            b.client_user_id = ids["consulente"]
            s.add(b)
            s.commit()
        r = client.post(f"/api/booking/{ids['booking']}/background/check",
                        json={"image": "data:image/jpeg;base64,AAAA"})
        assert r.status_code == 403

    def test_la_pagina_verifica_prima_di_applicare(self):
        call = (Path(__file__).resolve().parent.parent / "app" / "templates" / "call.html").read_text(encoding="utf-8-sig")
        assert "background/check" in call
        assert "sfondoApprovato" in call
        assert "verificata" in call, "le immagini ricordate prima del controllo vanno riscelte"


class TestSfondoIspiramy:
    def test_l_immagine_ha_le_misure_giuste(self):
        """1280x720: la webcam riprende spesso in 4:3 e l'immagine viene
        ritagliata ai lati, quindi il marchio sta nella fascia centrale."""
        from PIL import Image

        percorso = Path(__file__).resolve().parent.parent / "app" / "static" / "call-background.png"
        with Image.open(percorso) as immagine:
            assert immagine.size == (1280, 720)

    def test_lo_sfondo_si_puo_rigenerare(self):
        """Lo script che lo produce sta nel repo, insieme al logo sorgente."""
        radice = Path(__file__).resolve().parent.parent
        assert (radice / "scripts" / "genera_sfondo_call.py").exists()
        assert (radice / "app" / "static" / "logo-marchio.png").exists()
