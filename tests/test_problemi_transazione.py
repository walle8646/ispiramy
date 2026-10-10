"""Quota del consulente rimasta ferma, e la lista dei movimenti da riprovare.

Se il rimborso al cliente riesce e il trasferimento al consulente no, il
denaro del consulente non si segna come pagato: si riprova fra 6 ore, al
riavvio e dal pannello, una volta sola.
"""
import secrets
from datetime import timedelta
from types import SimpleNamespace

from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, Dispute, Notification, TransactionProblem, User
from app.scheduler import check_booking_noshow, release_booking_payment
from app.utils.orari import now_italy_naive
from app.utils.password import hash_password
from app.utils.problemi_transazione import ORE_FRA_UN_TENTATIVO, registra, riprova_adesso
from app.utils.rate_limit import reset_rate_limit
from tests.test_matrice_call_contestazioni import (
    _contestazione,
    _login,
    _prenota,
    _stato_pagamento,
)


def _problema(booking_id, kind="transfer"):
    with Session(engine) as s:
        riga = s.exec(select(TransactionProblem).where(
            TransactionProblem.booking_id == booking_id,
            TransactionProblem.kind == kind,
        )).first()
        if riga is None:
            return None
        return SimpleNamespace(
            id=riga.id,
            status=riga.status,
            kind=riga.kind,
            amount_cents=riga.amount_cents,
            attempts=riga.attempts,
            error_reason=riga.error_reason,
            next_attempt_at=riga.next_attempt_at,
            resolution_note=riga.resolution_note,
        )


import pytest


@pytest.fixture
def scena(client):
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        def utente(ruolo, tipo, stripe="acct_test_problemi"):
            u = User(
                email=f"prob-{ruolo}-{secrets.token_hex(4)}@test.local",
                password_md5=hash_password(password),
                confirmed=1,
                nome=ruolo.capitalize(),
                cognome="Prova",
                user_type_id=tipo,
                platform_fee_percent=20,
                stripe_account_id=stripe if tipo == 1 and ruolo == "consulente" else None,
                stripe_onboarding_complete=ruolo == "consulente",
            )
            s.add(u)
            return u

        cliente = utente("cliente", 1, stripe=None)
        consulente = utente("consulente", 1)
        verificatore = utente("verificatore", 2, stripe=None)
        admin = utente("admin", 3, stripe=None)
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
            for problema in s.exec(select(TransactionProblem).where(
                TransactionProblem.booking_id.in_(id_prenotazioni)
            )).all():
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


def _stripe(monkeypatch, trasferimento):
    import stripe

    rimborsi = []

    def refund(**kwargs):
        rimborsi.append(kwargs)
        return SimpleNamespace(id="re_problema")

    monkeypatch.setattr(stripe.Refund, "create", refund)
    monkeypatch.setattr(stripe.Transfer, "create", trasferimento)
    return rimborsi


def test_la_quota_fallita_resta_ferma_e_non_si_paga_due_volte(scena, csrf_client, monkeypatch):
    assert ORE_FRA_UN_TENTATIVO == 6
    booking_id = _prenota(scena, -30)
    dispute_id = _contestazione(scena, booking_id)
    chiamate = []

    def transfer(**kwargs):
        chiamate.append(kwargs)
        raise RuntimeError("conto rifiutato")

    _stripe(monkeypatch, transfer)
    _login(csrf_client, scena.email_admin, scena.password)
    risposta = csrf_client.post(
        f"/admin/api/disputes/{dispute_id}/status",
        json={"status": "resolved", "refund_percentage": 50},
    )
    assert risposta.status_code == 200, risposta.text
    assert _stato_pagamento(booking_id)[1] == "partially_refunded"
    with Session(engine) as s:
        assert s.get(Booking, booking_id).stripe_transfer_id is None
    problema = _problema(booking_id)
    assert problema is not None
    assert problema.status == "open"
    assert problema.kind == "transfer"
    assert problema.amount_cents == 800
    assert problema.attempts == 1
    assert "rifiutato" in problema.error_reason
    assert problema.next_attempt_at > now_italy_naive()

    from app.scheduler import scheduler
    job = scheduler.get_job(f"consultant_share_{booking_id}")
    assert job is not None
    from datetime import datetime
    from zoneinfo import ZoneInfo
    fra = (job.next_run_time - datetime.now(ZoneInfo("Europe/Rome"))).total_seconds()
    assert 5 * 3600 < fra < 7 * 3600
    assert chiamate[0]["idempotency_key"] == f"consultant-share-{booking_id}"
    assert chiamate[0]["amount"] == 800

    # Il rilascio intero non deve partire: il cliente è già stato rimborsato in parte.
    release_booking_payment(booking_id, forza=True)
    assert len(chiamate) == 1
    assert _stato_pagamento(booking_id)[1] == "partially_refunded"

    def transfer_ok(**kwargs):
        chiamate.append(kwargs)
        return SimpleNamespace(id="tr_quota_ok")

    monkeypatch.setattr("stripe.Transfer.create", transfer_ok)
    assert riprova_adesso(problema.id) == "ok"
    assert len(chiamate) == 2
    assert chiamate[1]["amount"] == 800
    assert chiamate[1]["idempotency_key"] == f"consultant-share-{booking_id}"
    with Session(engine) as s:
        assert s.get(Booking, booking_id).stripe_transfer_id == "tr_quota_ok"
    assert _problema(booking_id).status == "resolved"

    assert riprova_adesso(problema.id) == "chiuso"
    assert len(chiamate) == 2


def test_se_il_trasferimento_e_gia_partito_non_se_ne_fa_un_altro(scena, monkeypatch):
    booking_id = _prenota(scena, -30, payment_status="partially_refunded", stripe_transfer_id="tr_gia")
    registra(booking_id, "transfer", 800, "risposta persa")
    chiamate = []
    import stripe
    monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))
    from app.utils.problemi_transazione import _riprova_quota
    _riprova_quota(booking_id)
    assert chiamate == []
    assert _problema(booking_id).status == "resolved"


def test_senza_conto_la_quota_aspetta_e_poi_parte_una_volta(scena, csrf_client, monkeypatch):
    with Session(engine) as s:
        consulente = s.get(User, scena.consulente)
        consulente.stripe_account_id = None
        s.add(consulente)
        s.commit()
    booking_id = _prenota(scena, -30)
    dispute_id = _contestazione(scena, booking_id)
    chiamate = []
    _stripe(monkeypatch, lambda **kw: chiamate.append(kw))
    _login(csrf_client, scena.email_admin, scena.password)
    risposta = csrf_client.post(
        f"/admin/api/disputes/{dispute_id}/status",
        json={"status": "resolved", "refund_percentage": 50},
    )
    assert risposta.status_code == 200, risposta.text
    assert chiamate == []
    problema = _problema(booking_id)
    assert problema.amount_cents == 800
    assert "Stripe" in problema.error_reason

    with Session(engine) as s:
        consulente = s.get(User, scena.consulente)
        consulente.stripe_account_id = "acct_test_problemi"
        s.add(consulente)
        s.commit()

    def transfer_ok(**kwargs):
        chiamate.append(kwargs)
        return SimpleNamespace(id="tr_dopo")

    import stripe
    monkeypatch.setattr(stripe.Transfer, "create", transfer_ok)
    assert riprova_adesso(problema.id) == "ok"
    assert len(chiamate) == 1
    assert chiamate[0]["amount"] == 800
    assert riprova_adesso(problema.id) == "chiuso"
    assert len(chiamate) == 1


def test_segnare_risolto_chiede_una_nota_e_non_paga(scena, csrf_client, monkeypatch):
    booking_id = _prenota(scena, -30, payment_status="partially_refunded")
    registra(booking_id, "transfer", 800, "da chiudere a mano")
    problema = _problema(booking_id)
    chiamate = []
    import stripe
    monkeypatch.setattr(stripe.Transfer, "create", lambda **kw: chiamate.append(kw))
    _login(csrf_client, scena.email_admin, scena.password)

    vuota = csrf_client.post(
        f"/admin/api/transaction-problems/{problema.id}/resolve",
        json={"note": "   "},
    )
    assert vuota.status_code == 400
    assert _problema(booking_id).status == "open"

    chiusa = csrf_client.post(
        f"/admin/api/transaction-problems/{problema.id}/resolve",
        json={"note": "Pagato a mano fuori dalla piattaforma"},
    )
    assert chiusa.status_code == 200, chiusa.text
    assert _problema(booking_id).status == "resolved"
    assert "fuori dalla piattaforma" in _problema(booking_id).resolution_note
    ancora = csrf_client.post(f"/admin/api/transaction-problems/{problema.id}/retry")
    assert ancora.status_code == 200
    assert ancora.json()["esito"] == "chiuso"
    assert chiamate == []


def test_il_rimborso_al_cliente_che_fallisce_non_crea_un_problema(scena, csrf_client, monkeypatch):
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
    assert _problema(booking_id, "refund") is None
    assert _problema(booking_id, "transfer") is None
    assert _stato_pagamento(booking_id)[1] == "held"


def test_il_rimborso_del_no_show_fallito_si_riprova(scena, monkeypatch):
    booking_id = _prenota(scena, -80)
    import stripe

    def boom(**kwargs):
        raise RuntimeError("rete no-show")

    monkeypatch.setattr(stripe.Refund, "create", boom)
    check_booking_noshow(booking_id)
    assert _stato_pagamento(booking_id) == ("no_show", "held")
    problema = _problema(booking_id, "refund")
    assert problema is not None
    assert problema.amount_cents == 2198
    assert problema.status == "open"
    from app.scheduler import scheduler
    assert scheduler.get_job(f"refund_retry_{booking_id}") is not None


def test_il_compenso_senza_conto_compare_e_si_chiude_quando_parte(scena, monkeypatch):
    with Session(engine) as s:
        consulente = s.get(User, scena.consulente)
        consulente.stripe_account_id = None
        s.add(consulente)
        s.commit()
    booking_id = _prenota(scena, -30)
    release_booking_payment(booking_id, forza=True)
    problema = _problema(booking_id, "payout")
    assert problema.status == "open"
    assert problema.amount_cents == 1600
    assert _stato_pagamento(booking_id)[1] == "held"
    from app.scheduler import scheduler
    assert scheduler.get_job(f"payment_release_{booking_id}") is not None

    with Session(engine) as s:
        consulente = s.get(User, scena.consulente)
        consulente.stripe_account_id = "acct_test_problemi"
        s.add(consulente)
        s.commit()
    chiamate = []

    def transfer(**kwargs):
        chiamate.append(kwargs)
        return SimpleNamespace(id="tr_compenso")

    import stripe
    monkeypatch.setattr(stripe.Transfer, "create", transfer)
    release_booking_payment(booking_id, forza=True)
    assert len(chiamate) == 1
    assert chiamate[0]["amount"] == 1600
    assert chiamate[0]["idempotency_key"] == f"consultant-payout-{booking_id}"
    assert _stato_pagamento(booking_id)[1] == "released"
    assert _problema(booking_id, "payout").status == "resolved"
    release_booking_payment(booking_id, forza=True)
    assert len(chiamate) == 1


def test_al_riavvio_si_riprova_solo_cio_che_e_gia_scaduto(scena, monkeypatch):
    scaduto = _prenota(scena, -30, payment_status="partially_refunded")
    futuro = _prenota(scena, -40, payment_status="partially_refunded")
    registra(scaduto, "transfer", 800, "scaduto")
    registra(futuro, "transfer", 800, "futuro")
    with Session(engine) as s:
        riga = s.exec(select(TransactionProblem).where(
            TransactionProblem.booking_id == scaduto,
            TransactionProblem.kind == "transfer",
            TransactionProblem.status == "open",
        )).first()
        riga.next_attempt_at = now_italy_naive() - timedelta(minutes=1)
        s.add(riga)
        s.commit()

    visti = []
    monkeypatch.setattr(
        "app.utils.problemi_transazione._riprova_quota",
        lambda bid: visti.append(("quota", bid)),
    )
    monkeypatch.setattr(
        "app.utils.problemi_transazione._riprova_rimborso",
        lambda bid: visti.append(("rimborso", bid)),
    )
    monkeypatch.setattr(
        "app.utils.problemi_transazione._riprova_compenso",
        lambda bid: visti.append(("compenso", bid)),
    )
    monkeypatch.setattr(
        "app.utils.problemi_transazione._programma",
        lambda kind, bid, quando: visti.append(("programma", kind, bid)),
    )
    from app.utils.problemi_transazione import riprendi_dopo_riavvio
    riprendi_dopo_riavvio()
    assert ("quota", scaduto) in visti
    assert ("quota", futuro) not in visti
    assert ("programma", "transfer", futuro) in visti


def test_verificatore_e_admin_vedono_la_lista_un_utente_no(scena, csrf_client, monkeypatch):
    booking_id = _prenota(scena, -30, payment_status="partially_refunded")
    registra(booking_id, "transfer", 800, "in attesa del conto")
    problema = _problema(booking_id)
    chiamate = []

    def transfer(**kwargs):
        chiamate.append(kwargs)
        return SimpleNamespace(id="tr_da_verificatore")

    import stripe
    monkeypatch.setattr(stripe.Transfer, "create", transfer)

    csrf_client.get("/logout")
    _login(csrf_client, scena.email_cliente, scena.password)
    assert csrf_client.get("/admin/transaction-problems", follow_redirects=False).status_code == 302
    assert csrf_client.post(f"/admin/api/transaction-problems/{problema.id}/retry").status_code == 401

    _login(csrf_client, scena.email_verificatore, scena.password)
    pagina = csrf_client.get("/admin/transaction-problems")
    assert pagina.status_code == 200
    assert "Problemi transazioni" in pagina.text
    assert "Riprova ora" in pagina.text
    assert "Segna come risolto" in pagina.text
    assert "in attesa del conto" in pagina.text
    assert f"#{booking_id}" in pagina.text
    assert "8.00" in pagina.text
    lista = csrf_client.get("/admin/disputes")
    assert 'class="nav-badge"' in lista.text
    from app.utils.problemi_transazione import conta_aperti
    assert f'class="nav-badge">{conta_aperti()}' in lista.text

    csrf_client.cookies.set("ispiramy_lingua", "en")
    inglese = csrf_client.get("/admin/transaction-problems")
    assert inglese.status_code == 200
    assert "Transaction problems" in inglese.text
    assert "Retry now" in inglese.text
    assert "Mark as resolved" in inglese.text

    risposta = csrf_client.post(f"/admin/api/transaction-problems/{problema.id}/retry")
    assert risposta.status_code == 200, risposta.text
    assert len(chiamate) == 1
    assert _problema(booking_id).status == "resolved"
