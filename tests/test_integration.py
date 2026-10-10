"""Test di integrazione sui flussi che attraversano piu' componenti."""
import secrets
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, User
from app.routes.booking import hours_until_booking, now_italy_naive
from app.utils.orari import ORE_LIMITE_ANNULLAMENTO
from app.utils.notification_email import generate_email_html


class TestHomepage:
    def test_home_si_carica(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Ispiramy" in resp.text


class TestTemplateEmail:
    """Un nome template sconosciuto fa fallire l'invio in silenzio: i template
    configurati in notification_types devono esistere tutti."""

    def test_template_noti_producono_html(self):
        dati = {
            "user_name": "Mario", "consultant_name": "Anna", "client_name": "Mario",
            "other_user_name": "Anna", "date": "10/01/2026", "time": "10:00",
            "duration": "60", "action_url": "https://example.test/profile",
            "review_url": "https://example.test/review/x", "rating": "5",
            "comment": "ottimo", "author_name": "Mario", "contact_name": "Anna",
            "question_title": "titolo", "contact_date": "oggi", "reviewer_name": "Mario",
            "deadline": "12/09 alle 15:30", "topic": "Contratto d'affitto",
            "other_name": "Anna",
            "outcome_it": "La contestazione è stata accolta.",
            "outcome_en": "The dispute was upheld.",
        }
        import re
        from app.utils.notification_types import NOTIFICATION_TYPES
        # Tutti i template dei tipi inseriti all'avvio, non un elenco a parte
        # che si dimentica di aggiornare
        configurati = {tpl for *_, tpl in NOTIFICATION_TYPES if tpl}
        assert {"booking_request.html", "booking_accepted.html", "booking_request_expired.html"} <= configurati
        for nome in sorted(configurati):
            html = generate_email_html(nome, dati)
            assert html, f"template {nome} non generato"
            # Nessun segnaposto {qualcosa} deve restare non sostituito
            residui = set(re.findall(r"\{([a-z_]+)\}", html))
            assert not residui, f"{nome}: segnaposto non sostituiti {residui}"

    def test_alias_dei_nomi_legacy(self):
        """Alcuni DB hanno i nomi vecchi: devono continuare a funzionare."""
        dati = {"user_name": "Mario", "other_user_name": "Anna", "date": "10/01/2026",
                "time": "10:00", "duration": "60", "action_url": "https://example.test"}
        assert generate_email_html("booking_reminder_1h.html", dati)
        assert generate_email_html("booking_reminder_10min.html", dati)

    def test_template_inesistente_ritorna_none(self):
        assert generate_email_html("non_esiste.html", {}) is None


class TestFinestraDiCancellazione:
    """Il cliente non deve poter annullare (e farsi rimborsare) a ridosso o dopo
    la consulenza: era possibile fino a 15 minuti dopo la fine."""

    def _crea_booking(self, ore_da_ora: float):
        quando = now_italy_naive() + timedelta(hours=ore_da_ora)
        with Session(engine) as s:
            cliente = User(email=f"c-{secrets.token_hex(4)}@test.local", password_md5="x")
            consulente = User(email=f"p-{secrets.token_hex(4)}@test.local", password_md5="x")
            s.add(cliente)
            s.add(consulente)
            s.commit()
            s.refresh(cliente)
            s.refresh(consulente)
            b = Booking(
                client_user_id=cliente.id,
                consultant_user_id=consulente.id,
                booking_date=datetime(quando.year, quando.month, quando.day),
                start_time=quando.strftime("%H:%M"),
                end_time=(quando + timedelta(hours=1)).strftime("%H:%M"),
                duration_minutes=60,
                status="confirmed",
                payment_status="held",
            )
            s.add(b)
            s.commit()
            s.refresh(b)
            return b.id, cliente.id, consulente.id

    def _pulisci(self, ids):
        with Session(engine) as s:
            b = s.get(Booking, ids[0])
            if b:
                s.delete(b)
            for uid in ids[1:]:
                u = s.get(User, uid)
                if u:
                    s.delete(u)
            s.commit()

    def test_consulenza_gia_iniziata_e_fuori_finestra(self):
        ids = self._crea_booking(-1)
        try:
            with Session(engine) as s:
                assert hours_until_booking(s.get(Booking, ids[0])) < 0
        finally:
            self._pulisci(ids)

    def test_consulenza_a_ridosso_e_fuori_finestra(self):
        ids = self._crea_booking(1)
        try:
            with Session(engine) as s:
                assert hours_until_booking(s.get(Booking, ids[0])) < ORE_LIMITE_ANNULLAMENTO
        finally:
            self._pulisci(ids)

    def test_consulenza_tra_sei_ore_e_annullabile(self):
        ids = self._crea_booking(6)
        try:
            with Session(engine) as s:
                assert hours_until_booking(s.get(Booking, ids[0])) >= ORE_LIMITE_ANNULLAMENTO
        finally:
            self._pulisci(ids)


class TestPresenzaInCall:
    """La presenza istantanea non deve piu' toccare i timestamp di partecipazione,
    che sono la prova su cui il job no-show decide i rimborsi."""

    def test_mark_absent_non_azzera_i_joined_at(self):
        from app.routes.booking import mark_absent, mark_present

        mark_present(12345, 1)
        mark_present(12345, 2)
        rimasti = mark_absent(12345, 1)
        assert rimasti == {2}
        assert mark_absent(12345, 2) == set()


class TestGuardiaRecensioni:
    """Non si deve poter recensire una consulenza mai avvenuta: oltre a falsare
    i voti, la presenza di una recensione chiude la call."""

    def _booking(self, **kwargs):
        base = dict(
            client_user_id=1,
            consultant_user_id=2,
            booking_date=datetime(2026, 1, 10),
            start_time="10:00",
            end_time="11:00",
            duration_minutes=60,
        )
        base.update(kwargs)
        return Booking(**base)

    def test_consulenza_mai_iniziata_non_recensibile(self):
        from app.routes.review import consulenza_recensibile
        motivo = consulenza_recensibile(self._booking(status="confirmed"))
        assert motivo is not None
        assert "svolto" in motivo

    def test_consulenza_annullata_non_recensibile(self):
        from app.routes.review import consulenza_recensibile
        assert consulenza_recensibile(self._booking(status="cancelled")) is not None

    def test_in_attesa_di_pagamento_non_recensibile(self):
        from app.routes.review import consulenza_recensibile
        assert consulenza_recensibile(self._booking(status="pending_payment")) is not None

    def test_call_avviata_e_recensibile(self):
        from app.routes.review import consulenza_recensibile
        b = self._booking(status="confirmed", call_started_at=datetime(2026, 1, 10, 10, 2))
        assert consulenza_recensibile(b) is None

    def test_consulenza_completata_e_recensibile(self):
        from app.routes.review import consulenza_recensibile
        assert consulenza_recensibile(self._booking(status="completed")) is None


class TestConversazioniSoloInScrittura:
    def test_get_su_chat_inesistente_non_crea_righe(self, csrf_client):
        import secrets
        from sqlmodel import Session, func, select as sm_select
        from app.database import engine
        from app.models import Conversation, User
        from app.utils.password import hash_password

        with Session(engine) as s:
            a = User(email=f"m1-{secrets.token_hex(4)}@test.local",
                     password_md5=hash_password("password-di-prova"), confirmed=1)
            b = User(email=f"m2-{secrets.token_hex(4)}@test.local",
                     password_md5=hash_password("password-di-prova"), confirmed=1)
            s.add(a)
            s.add(b)
            s.commit()
            s.refresh(a)
            s.refresh(b)
            ids = (a.id, b.id, a.email)
            prima = s.exec(sm_select(func.count(Conversation.id))).one()

        try:
            from app.utils.rate_limit import reset_rate_limit
            reset_rate_limit()
            login = csrf_client.post("/api/login",
                                     data={"email": ids[2], "password": "password-di-prova"})
            assert login.status_code == 200, login.text

            resp = csrf_client.get(f"/api/messaggi/{ids[1]}")
            assert resp.status_code == 200
            assert resp.json()["messages"] == []

            with Session(engine) as s:
                dopo = s.exec(sm_select(func.count(Conversation.id))).one()
            assert dopo == prima, "una GET ha creato una conversazione"
        finally:
            csrf_client.get("/logout")
            with Session(engine) as s:
                for uid in ids[:2]:
                    u = s.get(User, uid)
                    if u:
                        s.delete(u)
                s.commit()
