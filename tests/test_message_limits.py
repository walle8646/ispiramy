"""Test dei limiti sulla messaggistica.

Tre problemi distinti, tutti silenziosi:
- il conteggio ripartiva solo da una prenotazione in stato 'confirmed', e 15
  minuti dopo la consulenza quello stato diventa 'completed': la conversazione
  si richiudeva da sola, stavolta per sempre;
- i messaggi di sistema (offerte di consulenza) consumavano il credito;
- i limiti configurati dall'amministratore in configuration_property venivano
  ignorati dal backend, che applicava le costanti scritte nel codice.
"""
import secrets
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, ConfigurationProperty, Conversation, Message, User
from app.routes.messages import (
    CONFIG_KEY_MAX_LENGTH,
    CONFIG_KEY_MAX_MESSAGES,
    DEFAULT_MAX_MESSAGES_PER_CONVERSATION,
    allinea_limite_messaggi_obsoleto,
    campi_limite,
    conta_messaggi_conversazione,
    frase_messaggi,
    get_config_int,
    max_lunghezza_messaggio,
    max_messaggi_per_conversazione,
    svuota_cache_configurazione,
)


@pytest.fixture(autouse=True)
def cache_pulita():
    svuota_cache_configurazione()
    yield
    svuota_cache_configurazione()


@pytest.fixture
def conversazione():
    """Due utenti e la loro conversazione. Ripulisce tutto alla fine."""
    with Session(engine) as s:
        a = User(email=f"chat1-{secrets.token_hex(4)}@test.local", password_md5="x")
        b = User(email=f"chat2-{secrets.token_hex(4)}@test.local", password_md5="x")
        s.add(a)
        s.add(b)
        s.commit()
        s.refresh(a)
        s.refresh(b)
        conv = Conversation(user1_id=min(a.id, b.id), user2_id=max(a.id, b.id))
        s.add(conv)
        s.commit()
        s.refresh(conv)
        ids = (conv.id, a.id, b.id)

    yield ids

    with Session(engine) as s:
        for m in s.exec(select(Message).where(Message.conversation_id == ids[0])).all():
            s.delete(m)
        # Vanno rimosse le prenotazioni in ENTRAMBE le direzioni: SQLite riusa
        # gli id degli utenti cancellati, e una prenotazione orfana finirebbe
        # per corrispondere a una coppia di utenti creata in un test successivo.
        for bk in s.exec(
            select(Booking).where(
                (Booking.client_user_id.in_(ids[1:])) | (Booking.consultant_user_id.in_(ids[1:]))
            )
        ).all():
            s.delete(bk)
        conv = s.get(Conversation, ids[0])
        if conv:
            s.delete(conv)
        for uid in ids[1:]:
            u = s.get(User, uid)
            if u:
                s.delete(u)
        s.commit()


def _messaggi(conv_id, sender_id, quanti, quando, sistema=False):
    with Session(engine) as s:
        for i in range(quanti):
            s.add(Message(
                conversation_id=conv_id,
                sender_id=sender_id,
                content=f"messaggio {i}",
                created_at=quando + timedelta(seconds=i),
                is_system_message=sistema,
            ))
        s.commit()


def _booking(client_id, consultant_id, stato, payment, creato):
    with Session(engine) as s:
        b = Booking(
            client_user_id=client_id,
            consultant_user_id=consultant_id,
            booking_date=datetime(2026, 3, 1),
            start_time="10:00",
            end_time="11:00",
            duration_minutes=60,
            status=stato,
            payment_status=payment,
            created_at=creato,
        )
        s.add(b)
        s.commit()


class TestConteggio:
    def test_conta_i_messaggi_normali(self, conversazione):
        conv_id, a, _ = conversazione
        _messaggi(conv_id, a, 5, datetime(2026, 1, 1))
        with Session(engine) as s:
            assert conta_messaggi_conversazione(s, s.get(Conversation, conv_id)) == 5

    def test_i_messaggi_di_sistema_non_consumano_il_credito(self, conversazione):
        conv_id, a, _ = conversazione
        _messaggi(conv_id, a, 3, datetime(2026, 1, 1))
        _messaggi(conv_id, a, 4, datetime(2026, 1, 2), sistema=True)
        with Session(engine) as s:
            assert conta_messaggi_conversazione(s, s.get(Conversation, conv_id)) == 3

    def test_una_consulenza_pagata_azzera_il_conteggio(self, conversazione):
        conv_id, a, b = conversazione
        _messaggi(conv_id, a, 10, datetime(2026, 1, 1))
        _booking(a, b, "confirmed", "held", datetime(2026, 1, 5))
        _messaggi(conv_id, a, 2, datetime(2026, 1, 10))
        with Session(engine) as s:
            assert conta_messaggi_conversazione(s, s.get(Conversation, conv_id)) == 2

    def test_la_consulenza_conclusa_continua_ad_azzerare(self, conversazione):
        """È il caso che si rompeva: 15 minuti dopo la consulenza il job no-show
        porta lo stato a 'completed' e il vecchio filtro su 'confirmed' non
        corrispondeva più, facendo tornare il conteggio su tutta la cronologia."""
        conv_id, a, b = conversazione
        _messaggi(conv_id, a, 10, datetime(2026, 1, 1))
        _booking(a, b, "completed", "released", datetime(2026, 1, 5))
        _messaggi(conv_id, a, 2, datetime(2026, 1, 10))
        with Session(engine) as s:
            assert conta_messaggi_conversazione(s, s.get(Conversation, conv_id)) == 2

    def test_una_prenotazione_non_pagata_non_azzera_nulla(self, conversazione):
        conv_id, a, b = conversazione
        _messaggi(conv_id, a, 10, datetime(2026, 1, 1))
        _booking(a, b, "pending_payment", "pending", datetime(2026, 1, 5))
        _messaggi(conv_id, a, 2, datetime(2026, 1, 10))
        with Session(engine) as s:
            assert conta_messaggi_conversazione(s, s.get(Conversation, conv_id)) == 12

    def test_una_prenotazione_rimborsata_non_azzera_nulla(self, conversazione):
        conv_id, a, b = conversazione
        _messaggi(conv_id, a, 4, datetime(2026, 1, 1))
        _booking(a, b, "cancelled", "refunded", datetime(2026, 1, 5))
        _messaggi(conv_id, a, 3, datetime(2026, 1, 10))
        with Session(engine) as s:
            assert conta_messaggi_conversazione(s, s.get(Conversation, conv_id)) == 7

    def test_vale_anche_a_ruoli_invertiti(self, conversazione):
        """Il consulente può essere l'uno o l'altro dei due nella conversazione."""
        conv_id, a, b = conversazione
        _messaggi(conv_id, a, 6, datetime(2026, 1, 1))
        _booking(b, a, "confirmed", "held", datetime(2026, 1, 5))
        _messaggi(conv_id, a, 1, datetime(2026, 1, 10))
        with Session(engine) as s:
            assert conta_messaggi_conversazione(s, s.get(Conversation, conv_id)) == 1


class TestConfigurazione:
    def _imposta(self, chiave, valore):
        with Session(engine) as s:
            riga = s.exec(
                select(ConfigurationProperty)
                .where(ConfigurationProperty.property_key == chiave)
            ).first()
            if riga:
                riga.property_value = str(valore)
            else:
                riga = ConfigurationProperty(property_key=chiave, property_value=str(valore))
            s.add(riga)
            s.commit()
            return riga.id

    def _rimuovi(self, chiave):
        with Session(engine) as s:
            for riga in s.exec(
                select(ConfigurationProperty)
                .where(ConfigurationProperty.property_key == chiave)
            ).all():
                s.delete(riga)
            s.commit()

    def test_senza_riga_usa_il_default(self):
        self._rimuovi(CONFIG_KEY_MAX_MESSAGES)
        svuota_cache_configurazione()
        assert max_messaggi_per_conversazione() == DEFAULT_MAX_MESSAGES_PER_CONVERSATION

    def test_il_valore_configurato_viene_applicato(self):
        """È il bug principale: l'amministratore aveva impostato 40 e il backend
        continuava a imporre 80."""
        try:
            self._imposta(CONFIG_KEY_MAX_MESSAGES, 40)
            svuota_cache_configurazione()
            assert max_messaggi_per_conversazione() == 40

            self._imposta(CONFIG_KEY_MAX_LENGTH, 500)
            svuota_cache_configurazione()
            assert max_lunghezza_messaggio() == 500
        finally:
            self._rimuovi(CONFIG_KEY_MAX_MESSAGES)
            self._rimuovi(CONFIG_KEY_MAX_LENGTH)

    def test_valore_non_numerico_ripiega_sul_default(self):
        try:
            self._imposta(CONFIG_KEY_MAX_MESSAGES, "quaranta")
            svuota_cache_configurazione()
            assert max_messaggi_per_conversazione() == DEFAULT_MAX_MESSAGES_PER_CONVERSATION
        finally:
            self._rimuovi(CONFIG_KEY_MAX_MESSAGES)

    def test_la_cache_evita_una_query_per_messaggio(self):
        self._rimuovi(CONFIG_KEY_MAX_MESSAGES)
        svuota_cache_configurazione()
        primo = get_config_int(CONFIG_KEY_MAX_MESSAGES, 99)
        # cambia il valore sotto il naso della cache: deve restare il precedente
        try:
            self._imposta(CONFIG_KEY_MAX_MESSAGES, 7)
            assert get_config_int(CONFIG_KEY_MAX_MESSAGES, 99) == primo
            svuota_cache_configurazione()
            assert get_config_int(CONFIG_KEY_MAX_MESSAGES, 99) == 7
        finally:
            self._rimuovi(CONFIG_KEY_MAX_MESSAGES)


class TestEndpointChatConfig:
    def test_espone_gli_stessi_valori_applicati_dal_backend(self, client):
        resp = client.get("/api/chat-config")
        assert resp.status_code == 200
        dati = resp.json()
        assert dati["max_messages"] == max_messaggi_per_conversazione()
        assert dati["max_length"] == max_lunghezza_messaggio()


class TestFrequenzaInvio:
    """Il limite per conversazione non impedisce di martellare destinatari
    diversi, né di innescare una notifica (email inclusa) a ogni invio."""

    def test_invio_a_raffica_viene_frenato(self, csrf_client):
        from app.utils.password import hash_password
        from app.utils.rate_limit import reset_rate_limit

        reset_rate_limit()
        with Session(engine) as s:
            mittente = User(email=f"rl1-{secrets.token_hex(4)}@test.local",
                            password_md5=hash_password("password-di-prova"), confirmed=1)
            destinatario = User(email=f"rl2-{secrets.token_hex(4)}@test.local",
                                password_md5=hash_password("password-di-prova"), confirmed=1)
            s.add(mittente)
            s.add(destinatario)
            s.commit()
            s.refresh(mittente)
            s.refresh(destinatario)
            ids = (mittente.id, destinatario.id, mittente.email)

        try:
            reset_rate_limit()
            login = csrf_client.post("/api/login",
                                     data={"email": ids[2], "password": "password-di-prova"})
            assert login.status_code == 200, login.text

            esiti = []
            for i in range(40):
                r = csrf_client.post(f"/api/messaggi/{ids[1]}", data={"content": f"ciao {i}"})
                esiti.append(r.status_code)

            assert 429 in esiti, f"nessun 429 fra {sorted(set(esiti))}"
            assert esiti[0] in (200, 201), f"il primo invio doveva riuscire, ha dato {esiti[0]}"
        finally:
            csrf_client.get("/logout")
            reset_rate_limit()
            with Session(engine) as s:
                conv = s.exec(
                    select(Conversation).where(
                        Conversation.user1_id == min(ids[0], ids[1]),
                        Conversation.user2_id == max(ids[0], ids[1]),
                    )
                ).first()
                if conv:
                    for m in s.exec(select(Message).where(Message.conversation_id == conv.id)).all():
                        s.delete(m)
                    s.delete(conv)
                for uid in ids[:2]:
                    u = s.get(User, uid)
                    if u:
                        s.delete(u)
                s.commit()


class TestLimiteSessanta:
    """Una conversazione vale 60 messaggi, non 15.

    Il banner del telefono diceva «1 messaggi su 15» perché il numero era
    scritto a mano nel template, mentre il server ne usava un altro. Se il
    tetto torna a 15, o se schermo e server si separano di nuovo, questi
    test falliscono.
    """

    def test_la_parola_si_accorda_col_numero(self):
        assert frase_messaggi(1) == "1 messaggio"
        assert frase_messaggi(60) == "60 messaggi"
        assert "1 messaggi" not in frase_messaggi(1)

    def test_il_default_e_60(self):
        TestConfigurazione()._rimuovi(CONFIG_KEY_MAX_MESSAGES)
        svuota_cache_configurazione()
        assert DEFAULT_MAX_MESSAGES_PER_CONVERSATION == 60
        assert max_messaggi_per_conversazione() == 60
        assert campi_limite(0) == {
            "max_messages": 60,
            "messages_left": 60,
            "limit_reached": False,
        }
        assert campi_limite(59)["messages_left"] == 1
        assert campi_limite(60)["limit_reached"] is True

    def test_il_template_non_ha_un_15_suo(self):
        from pathlib import Path

        radice = Path(__file__).resolve().parent.parent
        chat = (radice / "app" / "templates" / "chat.html").read_text(encoding="utf-8")
        widget = (radice / "app" / "templates" / "chat_widget.html").read_text(encoding="utf-8")
        assert "MAX_MESSAGES = 15" not in chat
        assert "limite di 15" not in chat
        assert "${messagesLeft} messaggi" not in chat
        assert "max_messaggi_conversazione" in chat
        assert 'return "1 messaggio"' in chat
        assert "max_messages: 40" not in widget
        assert "1/40" not in widget
        assert "max_messaggi_conversazione" in widget

    def test_i_vecchi_15_e_80_diventano_60_un_valore_scelto_resta(self):
        cfg = TestConfigurazione()
        try:
            for vecchio in ("15", "80"):
                cfg._imposta(CONFIG_KEY_MAX_MESSAGES, vecchio)
                svuota_cache_configurazione()
                assert allinea_limite_messaggi_obsoleto() is True
                assert max_messaggi_per_conversazione() == 60

            cfg._imposta(CONFIG_KEY_MAX_MESSAGES, 40)
            svuota_cache_configurazione()
            assert allinea_limite_messaggi_obsoleto() is False
            assert max_messaggi_per_conversazione() == 40
        finally:
            cfg._rimuovi(CONFIG_KEY_MAX_MESSAGES)
            svuota_cache_configurazione()


class TestLimiteApplicato:
    """Il 60° messaggio passa, il 61° no. Quindici messaggi non chiudono la chat."""

    def _utenti(self):
        from app.utils.password import hash_password

        password = "password-di-prova"
        with Session(engine) as s:
            a = User(email=f"lim1-{secrets.token_hex(4)}@test.local",
                     password_md5=hash_password(password), confirmed=1, nome="Ada")
            b = User(email=f"lim2-{secrets.token_hex(4)}@test.local",
                     password_md5=hash_password(password), confirmed=1, nome="Bruno")
            s.add(a)
            s.add(b)
            s.commit()
            s.refresh(a)
            s.refresh(b)
            conv = Conversation(user1_id=min(a.id, b.id), user2_id=max(a.id, b.id))
            s.add(conv)
            s.commit()
            s.refresh(conv)
            return conv.id, a.id, b.id, a.email, password

    def _pulisci(self, ids):
        with Session(engine) as s:
            for m in s.exec(select(Message).where(Message.conversation_id == ids[0])).all():
                s.delete(m)
            conv = s.get(Conversation, ids[0])
            if conv:
                s.delete(conv)
            for uid in ids[1:3]:
                u = s.get(User, uid)
                if u:
                    s.delete(u)
            s.commit()

    def _senza_riga_di_config(self):
        TestConfigurazione()._rimuovi(CONFIG_KEY_MAX_MESSAGES)
        svuota_cache_configurazione()

    def test_quindici_messaggi_non_chiudono_la_conversazione(self, csrf_client, monkeypatch):
        """Se il tetto tornasse a 15, questo invio verrebbe rifiutato."""
        from app.utils.rate_limit import reset_rate_limit

        async def consenti(testo):
            return {"approved": True, "reason": ""}

        monkeypatch.setattr("app.routes.messages.modera_testo_chat", consenti)
        self._senza_riga_di_config()
        reset_rate_limit()
        ids = self._utenti()
        try:
            _messaggi(ids[0], ids[1], 15, datetime.utcnow())
            login = csrf_client.post("/api/login", data={"email": ids[3], "password": ids[4]})
            assert login.status_code == 200, login.text

            lettura = csrf_client.get(f"/api/messaggi/{ids[2]}")
            assert lettura.status_code == 200
            dati = lettura.json()
            assert dati["max_messages"] == 60
            assert dati["messages_left"] == 45
            assert dati["limit_reached"] is False

            inviato = csrf_client.post(f"/api/messaggi/{ids[2]}", data={"content": "il sedicesimo"})
            assert inviato.status_code == 201, inviato.text
            assert inviato.json()["max_messages"] == 60
            assert inviato.json()["messages_left"] == 44
        finally:
            csrf_client.get("/logout")
            reset_rate_limit()
            self._pulisci(ids)
            self._senza_riga_di_config()

    def test_il_sessantesimo_passa_e_il_sessantunesimo_no(self, csrf_client, monkeypatch):
        from app.utils.rate_limit import reset_rate_limit

        async def consenti(testo):
            return {"approved": True, "reason": ""}

        monkeypatch.setattr("app.routes.messages.modera_testo_chat", consenti)
        self._senza_riga_di_config()
        reset_rate_limit()
        ids = self._utenti()
        try:
            _messaggi(ids[0], ids[1], 59, datetime.utcnow())
            login = csrf_client.post("/api/login", data={"email": ids[3], "password": ids[4]})
            assert login.status_code == 200, login.text

            sessantesimo = csrf_client.post(
                f"/api/messaggi/{ids[2]}", data={"content": "il sessantesimo"}
            )
            assert sessantesimo.status_code == 201, sessantesimo.text
            corpo = sessantesimo.json()
            assert corpo["max_messages"] == 60
            assert corpo["messages_left"] == 0
            assert corpo["limit_reached"] is True

            oltre = csrf_client.post(
                f"/api/messaggi/{ids[2]}", data={"content": "il sessantunesimo"}
            )
            assert oltre.status_code == 400, oltre.text
            assert "60" in oltre.json()["error"]
            assert "1 messaggi" not in oltre.json()["error"]

            with Session(engine) as s:
                quanti = conta_messaggi_conversazione(s, s.get(Conversation, ids[0]))
            assert quanti == 60
        finally:
            csrf_client.get("/logout")
            reset_rate_limit()
            self._pulisci(ids)
            self._senza_riga_di_config()

    def test_pagina_e_api_dicono_lo_stesso_numero(self, csrf_client):
        from app.utils.rate_limit import reset_rate_limit

        self._senza_riga_di_config()
        reset_rate_limit()
        ids = self._utenti()
        try:
            login = csrf_client.post("/api/login", data={"email": ids[3], "password": ids[4]})
            assert login.status_code == 200, login.text

            pagina = csrf_client.get(f"/messaggi/{ids[2]}")
            assert pagina.status_code == 200, pagina.text
            assert "let MAX_MESSAGES = 60;" in pagina.text
            assert "max_messages: 60" in pagina.text
            assert "0/60" in pagina.text
            assert "MAX_MESSAGES = 15" not in pagina.text
            assert "limite di 15" not in pagina.text
            assert "su 15" not in pagina.text
            assert "1/40" not in pagina.text
            assert 'return "1 messaggio"' in pagina.text

            api = csrf_client.get("/api/chat-config")
            assert api.json()["max_messages"] == 60

            TestConfigurazione()._imposta(CONFIG_KEY_MAX_MESSAGES, 40)
            svuota_cache_configurazione()
            pagina_40 = csrf_client.get(f"/messaggi/{ids[2]}")
            assert "let MAX_MESSAGES = 40;" in pagina_40.text
            assert "MAX_MESSAGES = 15" not in pagina_40.text
            assert csrf_client.get("/api/chat-config").json()["max_messages"] == 40
            assert csrf_client.get(f"/api/messaggi/{ids[2]}").json()["max_messages"] == 40
        finally:
            csrf_client.get("/logout")
            reset_rate_limit()
            self._pulisci(ids)
            self._senza_riga_di_config()
