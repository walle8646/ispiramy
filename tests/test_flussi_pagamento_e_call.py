"""Pagamento e videochiamata: i casi che i test già presenti non coprivano.

- Il compenso del consulente si calcola sui centesimi giusti (niente float) e
  non comprende le spese di servizio. Senza conto Stripe o PayPal il denaro
  resta trattenuto e si riprova, non viene segnato come già pagato.
- Una contestazione ferma il rilascio.
- Si entra in videochiamata da 10 minuti prima fino a 5 minuti dopo la fine,
  solo chi partecipa, e solo se la consulenza è confermata. Fuori orario non
  si segna nemmeno la presenza, perché è quella che decide rimborso e compenso.
- L'annullamento fino a 2 ore prima rimborsa tutto (spese di servizio comprese).
"""
import secrets
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, Dispute, Review, User
from app.scheduler import release_booking_payment, check_booking_noshow
from app.utils.orari import finestra_ingresso, now_italy_naive
from app.utils.password import hash_password
from app.utils.prezzi import (
    centesimi,
    compenso_consulente_centesimi,
    percentuale_commissione,
)
from app.utils.rate_limit import reset_rate_limit


def test_il_compenso_non_perde_i_centesimi_e_ignora_le_spese():
    """19,99 € con il 20% sono 16,00 € al consulente, non 15,99.

    int(19.99 * 100) in float vale 1998: un centesimo spariva a ogni
    trasferimento. Le spese di servizio (1,99 €) non entrano nel conto.
    """
    assert centesimi(Decimal("19.99")) == 1999
    assert compenso_consulente_centesimi(Decimal("19.99"), 20) == 1600
    assert compenso_consulente_centesimi(Decimal("20.00"), 20) == 1600
    # 20 € + 1,99 di spese farebbero 1759 centesimi all'80%: non è il compenso
    assert compenso_consulente_centesimi(Decimal("21.99"), 20) != 1600


def test_commissione_zero_resta_zero():
    """Zero non è 'non impostata': altrimenti si tratterrebbe comunque il 20%."""
    consulente = SimpleNamespace(platform_fee_percent=0)
    assert percentuale_commissione(consulente) == 0
    assert percentuale_commissione(None) == 20
    assert percentuale_commissione(SimpleNamespace(platform_fee_percent=None)) == 20
    assert compenso_consulente_centesimi(Decimal("50"), 0) == 5000


class TestFinestra:
    def _booking(self, minuti_all_inizio, durata=60):
        inizio = (now_italy_naive() + timedelta(minutes=minuti_all_inizio)).replace(second=0, microsecond=0)
        fine = inizio + timedelta(minutes=durata)
        return Booking(
            booking_date=inizio,
            start_time=inizio.strftime("%H:%M"),
            end_time=fine.strftime("%H:%M"),
            duration_minutes=durata,
        )

    def test_prima_dei_dieci_minuti_e_presto(self):
        assert finestra_ingresso(self._booking(30)) == "presto"

    def test_nei_dieci_minuti_prima_e_aperta(self):
        assert finestra_ingresso(self._booking(9)) == "aperta"

    def test_a_meta_consulenza_si_entra_ancora(self):
        """Prima il pulsante spariva 10 minuti dopo l'inizio, su un'ora di call."""
        assert finestra_ingresso(self._booking(-20, durata=60)) == "aperta"

    def test_nei_cinque_minuti_dopo_la_fine_si_rientra(self):
        assert finestra_ingresso(self._booking(-63, durata=60)) == "aperta"

    def test_dopo_il_margine_e_chiusa(self):
        assert finestra_ingresso(self._booking(-70, durata=60)) == "chiusa"


@pytest.fixture
def persone(client):
    """client avvia l'app (tipi di notifica). Due utenti e la password del cliente."""
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        cliente = User(
            email=f"flusso-c-{secrets.token_hex(4)}@test.local",
            password_md5=hash_password(password), confirmed=1, nome="Mario", cognome="Rossi",
        )
        consulente = User(
            email=f"flusso-p-{secrets.token_hex(4)}@test.local",
            password_md5=hash_password(password), confirmed=1, nome="Anna", cognome="Verdi",
            prezzo_consulenza=20, platform_fee_percent=20,
            stripe_account_id="acct_test_verifica", stripe_onboarding_complete=True,
        )
        estraneo = User(
            email=f"flusso-e-{secrets.token_hex(4)}@test.local",
            password_md5=hash_password(password), confirmed=1, nome="Luca", cognome="Neri",
        )
        s.add(cliente)
        s.add(consulente)
        s.add(estraneo)
        s.commit()
        s.refresh(cliente)
        s.refresh(consulente)
        s.refresh(estraneo)
        dati = SimpleNamespace(
            cliente=cliente.id, consulente=consulente.id, estraneo=estraneo.id,
            password=password, email_cliente=cliente.email, email_estraneo=estraneo.email,
        )
    yield dati
    reset_rate_limit()
    with Session(engine) as s:
        for uid in (dati.cliente, dati.consulente, dati.estraneo):
            for b in s.exec(select(Booking).where(
                (Booking.client_user_id == uid) | (Booking.consultant_user_id == uid)
            )).all():
                for d in s.exec(select(Dispute).where(Dispute.booking_id == b.id)).all():
                    s.delete(d)
                for r in s.exec(select(Review).where(Review.booking_id == b.id)).all():
                    s.delete(r)
                s.delete(b)
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


def _crea(persone, minuti_all_inizio, durata=60, **extra):
    inizio = (now_italy_naive() + timedelta(minutes=minuti_all_inizio)).replace(second=0, microsecond=0)
    fine = inizio + timedelta(minutes=durata)
    campi = dict(
        client_user_id=persone.cliente,
        consultant_user_id=persone.consulente,
        booking_date=inizio,
        start_time=inizio.strftime("%H:%M"),
        end_time=fine.strftime("%H:%M"),
        duration_minutes=durata,
        price=Decimal("19.99"),
        service_fee=Decimal("1.99"),
        status="confirmed",
        payment_status="held",
        payment_method="stripe",
        stripe_payment_intent_id="pi_test_verifica",
        recording_requested=False,
    )
    campi.update(extra)
    with Session(engine) as s:
        b = Booking(**campi)
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id


def _recensisci(booking_id, persone):
    with Session(engine) as s:
        s.add(Review(
            booking_id=booking_id,
            reviewer_user_id=persone.cliente,
            consultant_user_id=persone.consulente,
            rating_helpful=5,
            rating_prepared=5,
            rating_communication=5,
        ))
        s.commit()


def _stato(booking_id):
    with Session(engine) as s:
        b = s.get(Booking, booking_id)
        return b.status, b.payment_status, b.client_joined_at, b.stripe_transfer_id


class TestVideochiamata:
    def test_a_meta_consulenza_il_profilo_dice_che_si_puo_entrare(self, persone, csrf_client):
        booking_id = _crea(persone, -20)
        _login(csrf_client, persone.email_cliente, persone.password)
        dati = csrf_client.get("/api/booking/upcoming").json()
        card = next(b for b in dati["bookings"] if b["id"] == booking_id)
        assert card["can_join"] is True

        risposta = csrf_client.post(f"/api/booking/{booking_id}/join")
        assert risposta.status_code == 200, risposta.text
        assert _stato(booking_id)[2] is not None

    def test_un_giorno_prima_non_si_entra_e_non_si_segna_la_presenza(self, persone, csrf_client):
        booking_id = _crea(persone, 24 * 60)
        _login(csrf_client, persone.email_cliente, persone.password)

        card = next(b for b in csrf_client.get("/api/booking/upcoming").json()["bookings"] if b["id"] == booking_id)
        assert card["can_join"] is False

        join = csrf_client.post(f"/api/booking/{booking_id}/join")
        assert join.status_code == 403
        assert "10 minuti" in join.json()["detail"]
        assert _stato(booking_id)[2] is None

        pagina = csrf_client.get(f"/booking/call/{booking_id}", follow_redirects=False)
        assert pagina.status_code == 303
        assert "call_closed=presto" in pagina.headers["location"]

        token = csrf_client.get(f"/api/booking/{booking_id}/agora-token")
        assert token.status_code == 403

    def test_a_consulenza_finita_non_si_rientra(self, persone, csrf_client):
        booking_id = _crea(persone, -80)
        _login(csrf_client, persone.email_cliente, persone.password)
        join = csrf_client.post(f"/api/booking/{booking_id}/join")
        assert join.status_code == 403
        assert _stato(booking_id)[2] is None
        pagina = csrf_client.get(f"/booking/call/{booking_id}", follow_redirects=False)
        assert pagina.status_code == 303
        assert "call_closed=chiusa" in pagina.headers["location"]

    def test_nella_fascia_la_pagina_si_apre_e_un_estraneo_no(self, persone, csrf_client):
        booking_id = _crea(persone, -5)
        _login(csrf_client, persone.email_cliente, persone.password)
        pagina = csrf_client.get(f"/booking/call/{booking_id}")
        assert pagina.status_code == 200
        assert "AgoraRTC" in pagina.text or "bookingId" in pagina.text

        _login(csrf_client, persone.email_estraneo, persone.password)
        assert csrf_client.post(f"/api/booking/{booking_id}/join").status_code == 403
        assert csrf_client.get(f"/booking/call/{booking_id}").status_code == 403

    def test_una_richiesta_non_ancora_accettata_non_ha_la_call(self, persone, csrf_client):
        booking_id = _crea(persone, -5, status="awaiting_acceptance", payment_status="authorized")
        _login(csrf_client, persone.email_cliente, persone.password)
        assert csrf_client.post(f"/api/booking/{booking_id}/join").status_code == 403
        pagina = csrf_client.get(f"/booking/call/{booking_id}", follow_redirects=False)
        assert pagina.status_code == 303
        assert pagina.headers["location"] == "/profile"


class TestRilascioERimborso:
    def test_il_trasferimento_usa_i_centesimi_giusti(self, persone, monkeypatch):
        booking_id = _crea(persone, -20)
        trasferimenti = []

        def crea(**kwargs):
            trasferimenti.append(kwargs)
            return SimpleNamespace(id="tr_test_verifica")

        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", crea)
        _recensisci(booking_id, persone)

        release_booking_payment(booking_id)

        assert len(trasferimenti) == 1
        assert trasferimenti[0]["amount"] == 1600
        assert trasferimenti[0]["currency"] == "eur"
        assert trasferimenti[0]["destination"] == "acct_test_verifica"
        stato, pagamento, _, transfer_id = _stato(booking_id)
        assert pagamento == "released"
        assert transfer_id == "tr_test_verifica"
        assert stato == "confirmed"

    def test_senza_conto_stripe_il_denaro_resta_trattenuto(self, persone, monkeypatch):
        with Session(engine) as s:
            consulente = s.get(User, persone.consulente)
            consulente.stripe_account_id = None
            s.add(consulente)
            s.commit()
        booking_id = _crea(persone, -20)
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))
        _recensisci(booking_id, persone)

        release_booking_payment(booking_id)

        assert chiamate == []
        assert _stato(booking_id)[1] == "held"
        from app.scheduler import scheduler
        assert scheduler.get_job(f"payment_release_{booking_id}") is not None

    def test_una_contestazione_aperta_blocca_il_compenso(self, persone, monkeypatch):
        booking_id = _crea(persone, -20)
        with Session(engine) as s:
            s.add(Dispute(
                booking_id=booking_id,
                client_user_id=persone.cliente,
                consultant_user_id=persone.consulente,
                description="La consulenza non ha affrontato il tema chiesto",
                status="open",
            ))
            s.commit()
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))

        release_booking_payment(booking_id)

        assert chiamate == []
        assert _stato(booking_id)[1] == "held"

    def test_aprire_la_contestazione_annulla_il_rilascio_programmato(self, persone, csrf_client, monkeypatch):
        booking_id = _crea(persone, -20, call_started_at=now_italy_naive(), recording_requested=True)
        annullati = []
        monkeypatch.setattr(
            "app.scheduler.cancel_payment_release",
            lambda bid: annullati.append(bid),
        )
        _login(csrf_client, persone.email_cliente, persone.password)
        risposta = csrf_client.post(
            f"/api/booking/{booking_id}/dispute",
            json={"description": "Il consulente non ha trattato l'argomento concordato"},
        )
        assert risposta.status_code == 200, risposta.text
        assert annullati == [booking_id]

    def test_annullare_con_due_ore_di_anticipo_rimborsa_tutto(self, persone, csrf_client, monkeypatch):
        booking_id = _crea(persone, 6 * 60)
        rimborsi = []

        def crea(**kwargs):
            rimborsi.append(kwargs)
            return SimpleNamespace(id="re_test_verifica")

        import stripe
        monkeypatch.setattr(stripe.Refund, "create", crea)
        _login(csrf_client, persone.email_cliente, persone.password)

        risposta = csrf_client.delete(f"/api/booking/cancel/{booking_id}")
        assert risposta.status_code == 200, risposta.text
        assert rimborsi[0]["payment_intent"] == "pi_test_verifica"
        assert "amount" not in rimborsi[0], "il rimborso è intero, spese di servizio comprese"
        assert _stato(booking_id)[1] == "refunded"
        assert _stato(booking_id)[0] == "cancelled"

    def test_a_ridosso_della_consulenza_non_si_annulla(self, persone, csrf_client, monkeypatch):
        booking_id = _crea(persone, 60)
        rimborsi = []
        import stripe
        monkeypatch.setattr(stripe.Refund, "create", lambda **kw: rimborsi.append(kw))
        _login(csrf_client, persone.email_cliente, persone.password)

        risposta = csrf_client.delete(f"/api/booking/cancel/{booking_id}")
        assert risposta.status_code == 400
        assert rimborsi == []
        assert _stato(booking_id) == ("confirmed", "held", None, None)

    def test_se_il_consulente_non_si_presenta_il_cliente_e_rimborsato(self, persone, monkeypatch):
        booking_id = _crea(persone, -80)
        with Session(engine) as s:
            b = s.get(Booking, booking_id)
            b.client_joined_at = now_italy_naive()
            s.add(b)
            s.commit()
        rimborsi = []

        def crea(**kwargs):
            rimborsi.append(kwargs)
            return SimpleNamespace(id="re_noshow")

        import stripe
        monkeypatch.setattr(stripe.Refund, "create", crea)

        check_booking_noshow(booking_id)

        assert rimborsi and rimborsi[0]["payment_intent"] == "pi_test_verifica"
        assert "amount" not in rimborsi[0]
        assert _stato(booking_id)[0] == "no_show"
        assert _stato(booking_id)[1] == "refunded"

    def test_senza_recensione_il_compenso_resta_fermo(self, persone, monkeypatch):
        """Prima partiva 48 ore dopo la fine, anche se nessuno aveva recensito."""
        booking_id = _crea(persone, -24 * 60)
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))

        release_booking_payment(booking_id)

        assert chiamate == []
        assert _stato(booking_id)[1] == "held"

    def test_la_recensione_fa_partire_il_compenso(self, persone, csrf_client, monkeypatch):
        booking_id = _crea(
            persone, -120, status="completed", call_started_at=now_italy_naive(),
        )
        chiamate = []

        def crea(**kwargs):
            chiamate.append(kwargs)
            return SimpleNamespace(id="tr_dopo_recensione")

        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", crea)
        _login(csrf_client, persone.email_cliente, persone.password)

        risposta = csrf_client.post(
            f"/api/booking/{booking_id}/review",
            json={"rating_helpful": 5, "rating_prepared": 4, "rating_communication": 5, "comment": "Chiara"},
        )

        assert risposta.status_code == 200, risposta.text
        assert len(chiamate) == 1
        assert chiamate[0]["amount"] == 1600
        assert _stato(booking_id)[1] == "released"

    def test_recensione_con_contestazione_aperta_non_paga(self, persone, csrf_client, monkeypatch):
        booking_id = _crea(
            persone, -120, status="completed", call_started_at=now_italy_naive(),
            recording_requested=True,
        )
        with Session(engine) as s:
            s.add(Dispute(
                booking_id=booking_id,
                client_user_id=persone.cliente,
                consultant_user_id=persone.consulente,
                description="La consulenza non ha affrontato il tema chiesto",
                status="open",
            ))
            s.commit()
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))
        _login(csrf_client, persone.email_cliente, persone.password)

        risposta = csrf_client.post(
            f"/api/booking/{booking_id}/review",
            json={"rating_helpful": 2, "rating_prepared": 2, "rating_communication": 2},
        )

        assert risposta.status_code == 200, risposta.text
        assert chiamate == []
        assert _stato(booking_id)[1] == "held"

    def test_prima_delle_48_ore_senza_recensione_il_compenso_resta_fermo(self, persone, monkeypatch):
        """La fine è da 47 ore: manca ancora un'ora alle 48."""
        booking_id = _crea(persone, -(48 * 60))
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))

        release_booking_payment(booking_id)

        assert chiamate == []
        assert _stato(booking_id)[1] == "held"

    def test_dopo_48_ore_senza_recensione_il_compenso_parte(self, persone, monkeypatch):
        booking_id = _crea(persone, -(50 * 60))
        chiamate = []

        def crea(**kwargs):
            chiamate.append(kwargs)
            return SimpleNamespace(id="tr_48_ore")

        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", crea)

        release_booking_payment(booking_id)

        assert len(chiamate) == 1
        assert _stato(booking_id)[1] == "released"

    def test_dopo_48_ore_una_contestazione_blocca_ancora(self, persone, monkeypatch):
        booking_id = _crea(persone, -(50 * 60))
        with Session(engine) as s:
            s.add(Dispute(
                booking_id=booking_id,
                client_user_id=persone.cliente,
                consultant_user_id=persone.consulente,
                description="Contestazione ancora aperta",
                status="in_review",
            ))
            s.commit()
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))

        release_booking_payment(booking_id)

        assert chiamate == []
        assert _stato(booking_id)[1] == "held"

    def test_contestazione_respinta_paga_anche_senza_recensione(self, persone, monkeypatch):
        booking_id = _crea(persone, -30)
        chiamate = []

        def crea(**kwargs):
            chiamate.append(kwargs)
            return SimpleNamespace(id="tr_forza")

        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", crea)

        release_booking_payment(booking_id, forza=True)

        assert len(chiamate) == 1
        assert _stato(booking_id)[1] == "released"

    def test_nemmeno_la_decisione_paga_se_la_contestazione_e_ancora_aperta(self, persone, monkeypatch):
        booking_id = _crea(persone, -30)
        with Session(engine) as s:
            s.add(Dispute(
                booking_id=booking_id,
                client_user_id=persone.cliente,
                consultant_user_id=persone.consulente,
                description="Ancora aperta",
                status="open",
            ))
            s.commit()
        chiamate = []
        import stripe
        monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))

        release_booking_payment(booking_id, forza=True)

        assert chiamate == []
        assert _stato(booking_id)[1] == "held"

    def test_la_scadenza_di_riserva_e_il_numero_unico(self, persone):
        from zoneinfo import ZoneInfo

        from app.scheduler import schedule_payment_release, scheduler
        from app.utils.orari import ORE_RILASCIO_SENZA_RECENSIONE

        assert ORE_RILASCIO_SENZA_RECENSIONE == 48
        booking_id = _crea(persone, 24 * 60)
        # La data si conta da adesso: una data scritta a mano invecchia, e
        # quando passa il job nasce gia' scaduto. Con lo scheduler acceso -
        # cioe' a suite intera, dove qualche test avvia l'app - APScheduler lo
        # scartava come perso e get_job non trovava piu' niente.
        fine = (now_italy_naive() + timedelta(days=2)).replace(second=0, microsecond=0)
        schedule_payment_release(booking_id, fine)
        job = scheduler.get_job(f"payment_release_{booking_id}")
        atteso = fine.replace(tzinfo=ZoneInfo("Europe/Rome")) + timedelta(hours=48)
        assert job is not None
        assert abs((job.next_run_time - atteso).total_seconds()) < 2


def test_pagine_e_mail_raccontano_la_regola_nuova(client):
    """Termini, faq e email dicono 48 ore / 48 hours se la recensione non arriva."""
    from app.utils.notification_email import generate_email_html
    from app.utils.orari import ORE_RILASCIO_SENZA_RECENSIONE

    ore = str(ORE_RILASCIO_SENZA_RECENSIONE)
    italiano = client.get("/terms").text
    assert "recensione" in italiano and f"{ore} ore" in italiano
    inglese = client.get("/terms", cookies={"ispiramy_lingua": "en"}).text
    assert "leaves a review" in inglese and f"{ore} hours" in inglese

    faq_en = client.get("/faq", cookies={"ispiramy_lingua": "en"}).text
    assert f"{ore} hours" in faq_en
    come = client.get("/come-funziona").text
    assert f"{ore} ore" in come and "contestazione" in come

    dati = {
        "consultant_name": "Anna", "client_name": "Mario", "user_name": "Mario",
        "date": "20/10/2026", "time": "10:00", "duration": "60",
        "action_url": "http://localhost/profile", "review_url": "http://localhost/review/x",
        "reason_section": "",
    }
    for nome in ("booking_confirmed.html", "review_request.html", "review_reminder.html", "dispute_opened.html"):
        html = generate_email_html(nome, dati)
        assert html, nome
        assert f"{ore} ore" in html
        assert f"{ore} hours" in html
