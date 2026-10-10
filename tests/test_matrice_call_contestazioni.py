"""Matrice call, contestazioni e pannello admin.

Senza registrazione la contestazione non si apre, da nessuna parte.
Entro 48 ore dalla fine, con la registrazione accesa, sì.
La decisione dell'amministratore rimborsa o paga e avvisa entrambi.
"""
import secrets
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, Dispute, Notification, TransactionProblem, User
from app.scheduler import check_booking_noshow, release_booking_payment
from app.utils.orari import ORE_FINESTRA_CONTESTAZIONE, now_italy_naive
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit

TESTO = "Il consulente non ha affrontato il tema che avevo chiesto in prenotazione"


@pytest.fixture
def scena(client):
    """Cliente, consulente con Stripe, verificatore e amministratore."""
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        def utente(ruolo, tipo, stripe=None):
            u = User(
                email=f"mat-{ruolo}-{secrets.token_hex(4)}@test.local",
                password_md5=hash_password(password),
                confirmed=1,
                nome=ruolo.capitalize(),
                cognome="Prova",
                user_type_id=tipo,
                platform_fee_percent=20,
                stripe_account_id=stripe,
                stripe_onboarding_complete=bool(stripe),
            )
            s.add(u)
            return u

        cliente = utente("cliente", 1)
        consulente = utente("consulente", 1, stripe="acct_test_matrice")
        verificatore = utente("verificatore", 2)
        admin = utente("admin", 3)
        s.commit()
        for u in (cliente, consulente, verificatore, admin):
            s.refresh(u)
        dati = SimpleNamespace(
            cliente=cliente.id,
            consulente=consulente.id,
            verificatore=verificatore.id,
            admin=admin.id,
            password=password,
            email_cliente=cliente.email,
            email_consulente=consulente.email,
            email_verificatore=verificatore.email,
            email_admin=admin.email,
        )
    yield dati
    reset_rate_limit()
    ids = [dati.cliente, dati.consulente, dati.verificatore, dati.admin]
    with Session(engine) as s:
        prenotazioni = s.exec(select(Booking).where(
            (Booking.client_user_id.in_(ids)) | (Booking.consultant_user_id.in_(ids))
        )).all()
        id_prenotazioni = [b.id for b in prenotazioni]
        if id_prenotazioni:
            for problema in s.exec(select(TransactionProblem).where(TransactionProblem.booking_id.in_(id_prenotazioni))).all():
                s.delete(problema)
            for d in s.exec(select(Dispute).where(Dispute.booking_id.in_(id_prenotazioni))).all():
                s.delete(d)
        for n in s.exec(select(Notification).where(Notification.user_id.in_(ids))).all():
            s.delete(n)
        s.commit()
        for b in s.exec(select(Booking).where(Booking.id.in_(id_prenotazioni))).all():
            s.delete(b)
        s.commit()
        for uid in ids:
            u = s.get(User, uid)
            if u:
                s.delete(u)
        s.commit()


def _login(http, email, password):
    http.get("/logout")
    reset_rate_limit()
    marker = 'name="csrf-token" content="'
    pagina = http.get("/login").text
    if marker in pagina:
        http.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    risposta = http.post("/api/login", data={"email": email, "password": password})
    assert risposta.status_code == 200, risposta.text


def _prenota(scena, minuti_all_inizio, durata=60, **extra):
    inizio = (now_italy_naive() + timedelta(minutes=minuti_all_inizio)).replace(second=0, microsecond=0)
    fine = inizio + timedelta(minutes=durata)
    campi = dict(
        client_user_id=scena.cliente,
        consultant_user_id=scena.consulente,
        booking_date=inizio,
        start_time=inizio.strftime("%H:%M"),
        end_time=fine.strftime("%H:%M"),
        duration_minutes=durata,
        price=Decimal("19.99"),
        service_fee=Decimal("1.99"),
        status="confirmed",
        payment_status="held",
        payment_method="stripe",
        stripe_payment_intent_id="pi_test_matrice",
        recording_requested=True,
    )
    campi.update(extra)
    with Session(engine) as s:
        b = Booking(**campi)
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id


def _contestazione(scena, booking_id, status="open", **extra):
    with Session(engine) as s:
        d = Dispute(
            booking_id=booking_id,
            client_user_id=scena.cliente,
            consultant_user_id=scena.consulente,
            description=TESTO,
            status=status,
            **extra,
        )
        s.add(d)
        s.commit()
        s.refresh(d)
        return d.id


def _apri(http, booking_id):
    return http.post(f"/api/booking/{booking_id}/dispute", json={"description": TESTO})


def _stato_pagamento(booking_id):
    with Session(engine) as s:
        b = s.get(Booking, booking_id)
        return b.status, b.payment_status


def _stato_contestazione(dispute_id):
    with Session(engine) as s:
        return s.get(Dispute, dispute_id).status


def _avvisi(user_id, tipo):
    with Session(engine) as s:
        return s.exec(select(Notification).where(
            Notification.user_id == user_id,
            Notification.type == tipo,
        )).all()


class TestSenzaRegistrazioneNonSiContesta:
    def test_l_api_rifiuta_e_non_crea_nulla(self, scena, csrf_client):
        booking_id = _prenota(scena, -30, recording_requested=False)
        _login(csrf_client, scena.email_cliente, scena.password)
        risposta = _apri(csrf_client, booking_id)
        assert risposta.status_code == 400
        assert "registrazione" in risposta.json()["detail"].lower()
        with Session(engine) as s:
            assert s.exec(select(Dispute).where(Dispute.booking_id == booking_id)).first() is None

    def test_lo_storico_non_offre_il_pulsante(self, scena, csrf_client):
        booking_id = _prenota(scena, -90, recording_requested=False)
        _login(csrf_client, scena.email_cliente, scena.password)
        storia = csrf_client.get("/api/booking/history").json()["bookings"]
        card = next(b for b in storia if b["id"] == booking_id)
        assert card["can_dispute"] is False

    def test_la_mail_di_recensione_non_mostra_il_modulo(self, scena, client):
        booking_id = _prenota(scena, -30, recording_requested=False, review_token="a" * 24)
        pagina = client.get("/review/" + "a" * 24)
        assert pagina.status_code == 200
        assert 'class="review-btn-dispute"' not in pagina.text
        assert "Senza registrazione" in pagina.text
        assert booking_id

    def test_la_pagina_di_prenotazione_lo_dice(self, scena, csrf_client):
        _login(csrf_client, scena.email_cliente, scena.password)
        pagina = csrf_client.get(f"/book/{scena.consulente}")
        assert pagina.status_code == 200
        assert "Senza registrazione non potrai aprire una contestazione" in pagina.text

    def test_termini_e_faq_dicono_la_stessa_cosa(self, client):
        for path in ("/terms", "/faq"):
            testo = client.get(path).text
            assert "Senza registrazione" in testo
            assert "48 ore" in testo


class TestFinestraERegistrazioneAccesa:
    def test_entro_48_ore_si_apre_e_il_compenso_resta_fermo(self, scena, csrf_client, monkeypatch):
        assert ORE_FINESTRA_CONTESTAZIONE == 48
        booking_id = _prenota(scena, -(47 * 60))
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))
        _login(csrf_client, scena.email_cliente, scena.password)

        assert _apri(csrf_client, booking_id).status_code == 200
        release_booking_payment(booking_id)
        assert chiamate == []
        assert _stato_pagamento(booking_id)[1] == "held"
        assert len(_avvisi(scena.consulente, "dispute_opened")) == 1
        assert _avvisi(scena.cliente, "dispute_opened") == []

    def test_dopo_48_ore_e_troppo_tardi(self, scena, csrf_client):
        booking_id = _prenota(scena, -(50 * 60))
        _login(csrf_client, scena.email_cliente, scena.password)
        risposta = _apri(csrf_client, booking_id)
        assert risposta.status_code == 400
        assert "scaduto" in risposta.json()["detail"].lower()
        storia = csrf_client.get("/api/booking/history").json()["bookings"]
        card = next(b for b in storia if b["id"] == booking_id)
        assert card["can_dispute"] is False

    def test_con_la_registrazione_lo_storico_e_la_mail_la_offrono(self, scena, csrf_client, client):
        booking_id = _prenota(scena, -90, review_token="b" * 24)
        _login(csrf_client, scena.email_cliente, scena.password)
        storia = csrf_client.get("/api/booking/history").json()["bookings"]
        card = next(b for b in storia if b["id"] == booking_id)
        assert card["can_dispute"] is True
        pagina = client.get("/review/" + "b" * 24)
        assert 'class="review-btn-dispute"' in pagina.text

    def test_una_consulenza_annullata_non_si_contesta(self, scena, csrf_client):
        booking_id = _prenota(scena, -30, status="cancelled")
        _login(csrf_client, scena.email_cliente, scena.password)
        risposta = _apri(csrf_client, booking_id)
        assert risposta.status_code == 400
        assert "annullat" in risposta.json()["detail"].lower()

    def test_il_consulente_non_puo_aprirla(self, scena, csrf_client):
        booking_id = _prenota(scena, -30)
        _login(csrf_client, scena.email_consulente, scena.password)
        assert _apri(csrf_client, booking_id).status_code == 403

    def test_la_call_mostra_il_modulo_solo_se_si_registra(self, scena, csrf_client):
        con = _prenota(scena, -5, recording_requested=True)
        senza = _prenota(scena, -4, recording_requested=False)
        _login(csrf_client, scena.email_cliente, scena.password)
        pagina_si = csrf_client.get(f"/booking/call/{con}")
        assert pagina_si.status_code == 200
        assert 'class="review-btn-dispute"' in pagina_si.text
        pagina_no = csrf_client.get(f"/booking/call/{senza}")
        assert pagina_no.status_code == 200
        assert "non registrare" in pagina_no.text
        assert 'class="review-btn-dispute"' not in pagina_no.text


class TestComeFinisceLaCall:
    def test_se_entrano_entrambi_risulta_svolta_e_il_compenso_aspetta(self, scena, monkeypatch):
        """Non esiste uno stato 'interrotta': se entrambi sono entrati, è svolta."""
        booking_id = _prenota(scena, -80)
        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            b.client_joined_at = now_italy_naive()
            b.consultant_joined_at = now_italy_naive()
            s.add(b)
            s.commit()
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: (_ for _ in ()).throw(AssertionError("non ora")))
        check_booking_noshow(booking_id)
        assert _stato_pagamento(booking_id) == ("completed", "held")

    def test_se_il_cliente_non_entra_il_consulente_viene_pagato_dopo(self, scena, monkeypatch):
        booking_id = _prenota(scena, -80)
        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            b.consultant_joined_at = now_italy_naive()
            s.add(b)
            s.commit()
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: (_ for _ in ()).throw(AssertionError("non ora")))
        check_booking_noshow(booking_id)
        assert _stato_pagamento(booking_id) == ("no_show", "held")
        assert len(_avvisi(scena.consulente, "booking_noshow")) == 1

    def test_se_non_entra_nessuno_il_cliente_e_rimborsato(self, scena, monkeypatch):
        booking_id = _prenota(scena, -80)
        rimborsi = []

        def crea(**kwargs):
            rimborsi.append(kwargs)
            return SimpleNamespace(id="re_nessuno")

        import stripe
        monkeypatch.setattr(stripe.Refund, "create", crea)
        check_booking_noshow(booking_id)
        assert "amount" not in rimborsi[0]
        assert _stato_pagamento(booking_id) == ("no_show", "refunded")
        assert len(_avvisi(scena.cliente, "booking_noshow")) == 1
        assert _avvisi(scena.consulente, "booking_noshow") == []

    def test_il_cliente_assente_puo_ancora_contestare_se_c_era_la_registrazione(self, scena, csrf_client, monkeypatch):
        """Comportamento attuale: l'assenza del cliente non chiude la contestazione."""
        booking_id = _prenota(scena, -80)
        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            b.consultant_joined_at = now_italy_naive()
            s.add(b)
            s.commit()
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: None)
        check_booking_noshow(booking_id)
        _login(csrf_client, scena.email_cliente, scena.password)
        assert _apri(csrf_client, booking_id).status_code == 200
        release_booking_payment(booking_id, forza=True)
        assert _stato_pagamento(booking_id)[1] == "held"


class TestDecisioneAdmin:
    def _mock_stripe(self, monkeypatch):
        import stripe
        rimborsi, trasferimenti = [], []

        def refund(**kwargs):
            rimborsi.append(kwargs)
            return SimpleNamespace(id="re_admin")

        def transfer(**kwargs):
            trasferimenti.append(kwargs)
            return SimpleNamespace(id="tr_admin")

        monkeypatch.setattr(stripe.Refund, "create", refund)
        monkeypatch.setattr(stripe.Transfer, "create", transfer)
        return rimborsi, trasferimenti

    def test_rimborso_totale_include_le_spese_e_avvisa_entrambi(self, scena, csrf_client, monkeypatch):
        booking_id = _prenota(scena, -30)
        dispute_id = _contestazione(scena, booking_id)
        rimborsi, trasferimenti = self._mock_stripe(monkeypatch)
        _login(csrf_client, scena.email_admin, scena.password)

        risposta = csrf_client.post(
            f"/admin/api/disputes/{dispute_id}/status",
            json={"status": "resolved", "refund_percentage": 100},
        )
        assert risposta.status_code == 200, risposta.text
        assert rimborsi[0]["amount"] == 2198
        assert trasferimenti == []
        assert _stato_pagamento(booking_id) == ("confirmed", "refunded")
        assert _stato_contestazione(dispute_id) == "resolved"
        assert len(_avvisi(scena.cliente, "dispute_decided")) == 1
        assert len(_avvisi(scena.consulente, "dispute_decided")) == 1
        assert "rimbors" in _avvisi(scena.cliente, "dispute_decided")[0].message.lower()
        assert _avvisi(scena.consulente, "payment_released") == []

    def test_rimborso_parziale_paga_il_resto_al_consulente(self, scena, csrf_client, monkeypatch):
        booking_id = _prenota(scena, -30)
        dispute_id = _contestazione(scena, booking_id)
        rimborsi, trasferimenti = self._mock_stripe(monkeypatch)
        _login(csrf_client, scena.email_admin, scena.password)

        risposta = csrf_client.post(
            f"/admin/api/disputes/{dispute_id}/status",
            json={"status": "resolved", "refund_percentage": 50},
        )
        assert risposta.status_code == 200, risposta.text
        assert rimborsi[0]["amount"] == 1099
        assert trasferimenti[0]["amount"] == 800
        assert _stato_pagamento(booking_id)[1] == "partially_refunded"
        assert len(_avvisi(scena.cliente, "dispute_decided")) == 1
        assert len(_avvisi(scena.consulente, "dispute_decided")) == 1
        assert len(_avvisi(scena.consulente, "payment_released")) == 1

    def test_respinta_paga_il_consulente_e_avvisa_entrambi(self, scena, csrf_client, monkeypatch):
        booking_id = _prenota(scena, -30)
        dispute_id = _contestazione(scena, booking_id)
        _, trasferimenti = self._mock_stripe(monkeypatch)
        _login(csrf_client, scena.email_admin, scena.password)

        risposta = csrf_client.post(
            f"/admin/api/disputes/{dispute_id}/status",
            json={"status": "rejected"},
        )
        assert risposta.status_code == 200, risposta.text
        assert trasferimenti[0]["amount"] == 1600
        assert _stato_pagamento(booking_id)[1] == "released"
        assert len(_avvisi(scena.cliente, "dispute_decided")) == 1
        assert len(_avvisi(scena.consulente, "dispute_decided")) == 1
        assert "non è stata accolta" in _avvisi(scena.cliente, "dispute_decided")[0].message

    def test_se_il_rimborso_fallisce_la_contestazione_resta_aperta(self, scena, csrf_client, monkeypatch):
        booking_id = _prenota(scena, -30)
        dispute_id = _contestazione(scena, booking_id)
        import stripe

        def boom(**kwargs):
            raise RuntimeError("rete")

        monkeypatch.setattr(stripe.Refund, "create", boom)
        _login(csrf_client, scena.email_admin, scena.password)
        risposta = csrf_client.post(
            f"/admin/api/disputes/{dispute_id}/status",
            json={"status": "resolved", "refund_percentage": 100},
        )
        assert risposta.status_code == 502
        assert _stato_contestazione(dispute_id) == "open"
        assert _stato_pagamento(booking_id)[1] == "held"
        assert _avvisi(scena.cliente, "dispute_decided") == []

    def test_l_email_della_decisione_e_in_entrambe_le_lingue(self):
        from app.utils.notification_email import generate_email_html
        html = generate_email_html("dispute_decided.html", {
            "user_name": "Mario",
            "date": "10/10/2026",
            "time": "09:00",
            "outcome_it": "Ti rimborsiamo l'intero importo pagato.",
            "outcome_en": "You will receive a full refund of what you paid.",
            "action_url": "http://localhost/profile",
        })
        assert "Ti rimborsiamo" in html
        assert "full refund" in html
        import re
        assert not re.findall(r"\{[a-z_]+\}", html)

    def test_l_anteprima_usa_il_totale_pagato(self, scena, csrf_client):
        booking_id = _prenota(scena, -30, recording_filename="booking_demo")
        dispute_id = _contestazione(scena, booking_id)
        _login(csrf_client, scena.email_admin, scena.password)
        pagina = csrf_client.get(f"/admin/disputes/{dispute_id}")
        assert pagina.status_code == 200
        assert "const TOTALE_PAGATO = 21.98" in pagina.text
        assert "const PREZZO_CONSULENZA = 19.99" in pagina.text
        assert "Avvia Analisi AI" in pagina.text
        assert "Scarica MP4" in pagina.text
        assert "Rifiutata" in pagina.text
        testo = pagina.text
        assert dispute_id and booking_id and "21.98" in testo


class TestPermessi:
    def test_chi_non_e_loggato_e_un_utente_normale_non_entrano(self, scena, csrf_client):
        booking_id = _prenota(scena, -30)
        dispute_id = _contestazione(scena, booking_id)
        csrf_client.get("/logout")
        pagina = csrf_client.get("/admin/disputes", follow_redirects=False)
        assert pagina.status_code == 302
        assert pagina.headers["location"] == "/login"
        negata = csrf_client.post(
            f"/admin/api/disputes/{dispute_id}/status",
            json={"status": "rejected"},
        )
        assert negata.status_code in (401, 403)

        _login(csrf_client, scena.email_cliente, scena.password)
        pagina = csrf_client.get("/admin/disputes", follow_redirects=False)
        assert pagina.status_code == 302
        assert pagina.headers["location"] == "/login"
        negata = csrf_client.post(
            f"/admin/api/disputes/{dispute_id}/status",
            json={"status": "rejected"},
        )
        assert negata.status_code == 401
        assert _stato_contestazione(dispute_id) == "open"
        assert csrf_client.get(f"/admin/api/bookings/{booking_id}/download").status_code == 401
        assert csrf_client.get("/admin/transaction-problems", follow_redirects=False).status_code == 302

    def test_il_verificatore_vede_video_e_analisi_come_l_admin(self, scena, csrf_client, monkeypatch):
        booking_id = _prenota(scena, -30, recording_filename="booking_demo")
        dispute_id = _contestazione(scena, booking_id)

        class _s3_vuoto:
            def list_objects_v2(self, **kwargs):
                return {}

        monkeypatch.setattr("boto3.client", lambda *a, **k: _s3_vuoto())

        def niente_video(*args, **kwargs):
            raise FileNotFoundError("niente video di prova")

        monkeypatch.setattr("app.utils.gemini_analysis.download_video_from_s3", niente_video)
        _login(csrf_client, scena.email_verificatore, scena.password)

        lista = csrf_client.get("/admin/disputes")
        assert lista.status_code == 200
        assert TESTO in lista.text
        dettaglio = csrf_client.get(f"/admin/disputes/{dispute_id}")
        assert dettaglio.status_code == 200
        assert "Avvia Analisi AI" in dettaglio.text
        assert "Scarica MP4" in dettaglio.text
        assert "riservato agli amministratori" not in dettaglio.text

        analisi = csrf_client.post(f"/admin/api/disputes/{dispute_id}/ai-analysis")
        assert analisi.status_code != 403
        scarica = csrf_client.get(f"/admin/api/bookings/{booking_id}/download")
        assert scarica.status_code == 404

        decisione = csrf_client.post(
            f"/admin/api/disputes/{dispute_id}/status",
            json={"status": "in_review"},
        )
        assert decisione.status_code == 200
        assert _stato_contestazione(dispute_id) == "in_review"
        assert _stato_pagamento(booking_id)[1] == "held"

    def test_senza_video_l_analisi_dice_che_non_c_e(self, scena, csrf_client):
        booking_id = _prenota(scena, -30, recording_filename=None)
        dispute_id = _contestazione(scena, booking_id)
        _login(csrf_client, scena.email_admin, scena.password)
        pagina = csrf_client.get(f"/admin/disputes/{dispute_id}")
        assert "Nessun video" in pagina.text
        analisi = csrf_client.post(f"/admin/api/disputes/{dispute_id}/ai-analysis")
        assert analisi.status_code == 404
        assert "registrazione" in analisi.json()["detail"].lower()
