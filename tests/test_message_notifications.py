"""La notifica di un nuovo messaggio deve portare alla conversazione.

Puntava a /messages, che non esiste: sia il click nella campanella sia il
bottone "Apri Chat" dell'email finivano su un 404. Nello stesso passaggio le
notifiche sono state unificate su notification_service (c'erano due moduli
paralleli con firme diverse).
"""
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import Conversation, Message, Notification, NotificationType, User
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit


@pytest.fixture
def due_utenti_e_tipo_notifica():
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        tipo = s.exec(select(NotificationType).where(NotificationType.type_key == "community_contact")).first()
        creato_tipo = tipo is None
        if creato_tipo:
            s.add(NotificationType(type_key="community_contact", name="Nuovo messaggio",
                                   in_app=True, send_email=False, is_active=True))
        mittente = User(email=f"mit-{secrets.token_hex(4)}@test.local",
                        password_md5=hash_password(password), confirmed=1, nome="Mario")
        destinatario = User(email=f"dest-{secrets.token_hex(4)}@test.local",
                            password_md5="x", confirmed=1, nome="Anna")
        s.add(mittente)
        s.add(destinatario)
        s.commit()
        s.refresh(mittente)
        s.refresh(destinatario)
        ids = (mittente.id, destinatario.id, mittente.email, password)

    yield ids

    reset_rate_limit()
    with Session(engine) as s:
        for n in s.exec(select(Notification).where(Notification.user_id == ids[1])).all():
            s.delete(n)
        conv = s.exec(select(Conversation).where(
            Conversation.user1_id == min(ids[0], ids[1]),
            Conversation.user2_id == max(ids[0], ids[1]),
        )).first()
        if conv:
            for m in s.exec(select(Message).where(Message.conversation_id == conv.id)).all():
                s.delete(m)
            s.delete(conv)
        for uid in ids[:2]:
            u = s.get(User, uid)
            if u:
                s.delete(u)
        if creato_tipo:
            t = s.exec(select(NotificationType).where(NotificationType.type_key == "community_contact")).first()
            if t:
                s.delete(t)
        s.commit()


class TestNotificaMessaggio:
    def test_la_notifica_apre_la_conversazione(self, csrf_client, due_utenti_e_tipo_notifica):
        mittente_id, destinatario_id, email, password = due_utenti_e_tipo_notifica
        assert csrf_client.post("/api/login", data={"email": email, "password": password}).status_code == 200
        try:
            r = csrf_client.post(f"/api/messaggi/{destinatario_id}", data={"content": "Ciao!"})
            assert r.status_code == 201, r.text

            with Session(engine) as s:
                notifica = s.exec(select(Notification).where(Notification.user_id == destinatario_id)).one()
            assert notifica.type == "community_contact"
            assert notifica.action_url == f"/messaggi/{mittente_id}"
            assert notifica.related_user_id == mittente_id
            assert "Mario" in notifica.message

            # e il link esiste davvero
            assert csrf_client.get(notifica.action_url, follow_redirects=False).status_code != 404
        finally:
            csrf_client.get("/logout")


def _login(http, email, password):
    http.get("/logout")
    reset_rate_limit()
    marker = 'name="csrf-token" content="'
    pagina = http.get("/login").text
    if marker in pagina:
        http.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    risposta = http.post("/api/login", data={"email": email, "password": password})
    assert risposta.status_code == 200, risposta.text


class TestRispostaAvvisa:
    def test_una_risposta_avvisa_anche_se_l_altro_ha_scritto_da_poco(self, csrf_client):
        """Prima si guardava l'ultimo messaggio di chiunque: una risposta entro
        30 minuti non creava né la notifica in-app, né l'email, né la push."""
        reset_rate_limit()
        password = secrets.token_urlsafe(12)
        with Session(engine) as s:
            tipo = s.exec(select(NotificationType).where(NotificationType.type_key == "community_contact")).first()
            if tipo is None:
                s.add(NotificationType(type_key="community_contact", name="Nuovo messaggio",
                                       in_app=True, send_email=False, is_active=True))
                s.commit()
            mario = User(email=f"rep-m-{secrets.token_hex(4)}@test.local",
                         password_md5=hash_password(password), confirmed=1, nome="Mario")
            anna = User(email=f"rep-a-{secrets.token_hex(4)}@test.local",
                        password_md5=hash_password(password), confirmed=1, nome="Anna")
            s.add(mario)
            s.add(anna)
            s.commit()
            s.refresh(mario)
            s.refresh(anna)
            ids = (mario.id, anna.id, mario.email, anna.email)

        def quante(utente_id):
            with Session(engine) as s:
                return len(s.exec(select(Notification).where(
                    Notification.user_id == utente_id,
                    Notification.type == "community_contact",
                )).all())

        try:
            _login(csrf_client, ids[2], password)
            assert csrf_client.post(f"/api/messaggi/{ids[1]}", data={"content": "Ciao Anna"}).status_code == 201
            assert quante(ids[1]) == 1

            _login(csrf_client, ids[3], password)
            risposta = csrf_client.post(f"/api/messaggi/{ids[0]}", data={"content": "Ciao Mario"})
            assert risposta.status_code == 201, risposta.text
            assert quante(ids[0]) == 1, "la risposta deve avvisare anche se l'altro ha scritto un attimo prima"

            seconda = csrf_client.post(f"/api/messaggi/{ids[0]}", data={"content": "Ci sei?"})
            assert seconda.status_code == 201, seconda.text
            assert quante(ids[0]) == 1, "due messaggi ravvicinati dello stesso mittente non riavvisano"

            with Session(engine) as s:
                conv = s.exec(select(Conversation).where(
                    Conversation.user1_id == min(ids[0], ids[1]),
                    Conversation.user2_id == max(ids[0], ids[1]),
                )).one()
                for messaggio in s.exec(select(Message).where(
                    Message.conversation_id == conv.id,
                    Message.sender_id == ids[1],
                )).all():
                    messaggio.created_at = datetime.utcnow() - timedelta(minutes=31)
                    s.add(messaggio)
                s.commit()

            assert csrf_client.post(f"/api/messaggi/{ids[0]}", data={"content": "Riprovo"}).status_code == 201
            assert quante(ids[0]) == 2
        finally:
            csrf_client.get("/logout")
            reset_rate_limit()
            with Session(engine) as s:
                for uid in ids[:2]:
                    for n in s.exec(select(Notification).where(Notification.user_id == uid)).all():
                        s.delete(n)
                conv = s.exec(select(Conversation).where(
                    Conversation.user1_id == min(ids[0], ids[1]),
                    Conversation.user2_id == max(ids[0], ids[1]),
                )).first()
                if conv:
                    for m in s.exec(select(Message).where(Message.conversation_id == conv.id)).all():
                        s.delete(m)
                    s.delete(conv)
                for uid in ids[:2]:
                    u = s.get(User, uid)
                    if u:
                        s.delete(u)
                s.commit()


class TestNessunLinkAPagineInesistenti:
    def test_nessun_link_a_slash_messages(self):
        """La pagina messaggi è /messaggi: /messages risponde 404."""
        radice = Path(__file__).resolve().parent.parent / "app"
        schema = re.compile(r"""["'`(]/messages(?:["'`/?#)]|$)""")
        trovati = []
        for f in list(radice.rglob("*.html")) + list(radice.rglob("*.py")):
            for n, riga in enumerate(f.read_text(encoding="utf-8-sig").splitlines(), 1):
                if schema.search(riga):
                    trovati.append(f"{f.relative_to(radice.parent)}:{n}")
        assert not trovati, "link a /messages (404): " + ", ".join(trovati)

    def test_slash_messages_non_esiste_davvero(self, client):
        assert client.get("/messages", follow_redirects=False).status_code == 404
