"""Richieste aperte sul tabellone: contatto, ricerca per tag, orari nel fuso
di chi prenota, community, avvisi di pagamento, dicitura della conferma
automatica, toast di chat e verifica per categoria."""
import io
import json
import os
import secrets
from datetime import timedelta

from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, Category, Dispute, Notification, User
from app.utils.orari import now_italy_naive
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit
from app.utils.verifica_categorie import (
    categorie_dichiarate,
    id_categorie_verificate,
    registra_verifica_iniziale,
    scrivi_categorie_verificate,
)

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _entra(client, email, password="prova-password"):
    client.get("/logout")
    reset_rate_limit()
    marker = 'name="csrf-token" content="'
    pagina = client.get("/login").text
    if marker in pagina:
        client.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    assert client.post("/api/login", data={"email": email, "password": password}).status_code == 200


def test_la_lamentela_arriva_all_amministrazione(csrf_client, monkeypatch):
    inviate = []

    def finto(dest, oggetto, corpo, **kwargs):
        inviate.append((dest, oggetto, corpo))
        return True

    monkeypatch.setattr("app.routes.pages.send_email", finto)
    reset_rate_limit()
    risposta = csrf_client.post(
        "/contact",
        data={
            "nome": "Mario Rossi",
            "email": f"mario-{secrets.token_hex(3)}@example.com",
            "tipo": "lamentela",
            "messaggio": "La consulenza non e' andata come mi aspettavo e vorrei un contatto.",
        },
        follow_redirects=False,
    )
    assert risposta.status_code == 303
    assert risposta.headers["location"].endswith("/contact?inviato=1")
    from app.routes.pages import EMAIL_AMMINISTRAZIONE
    assert inviate[0][0] == EMAIL_AMMINISTRAZIONE
    assert "Lamentela" in inviate[0][1]
    pagina = csrf_client.get("/contact").text
    assert 'action="/contact"' in pagina
    assert "Scrivi una lamentela" in pagina


def test_il_modulo_html_parte_senza_header(client, monkeypatch):
    """Il modulo e' un form normale: il token sta nel campo, non nell'header
    che il sito aggiunge solo alle chiamate fetch."""
    monkeypatch.setattr("app.routes.pages.send_email", lambda *a, **k: True)
    reset_rate_limit()
    pagina = client.get("/contact")
    marker = 'name="csrf_token" value="'
    token = pagina.text.split(marker, 1)[1].split('"', 1)[0]
    risposta = client.post(
        "/contact",
        data={
            "csrf_token": token,
            "nome": "Visitatore",
            "email": f"visita-{secrets.token_hex(3)}@example.com",
            "tipo": "lamentela",
            "messaggio": "Vorrei segnalare un problema con una consulenza gia' conclusa.",
        },
        headers={"X-CSRF-Token": ""},
        follow_redirects=False,
    )
    assert risposta.status_code == 303


def test_un_messaggio_troppo_corto_non_parte(csrf_client, monkeypatch):
    monkeypatch.setattr("app.routes.pages.send_email", lambda *a, **k: True)
    reset_rate_limit()
    risposta = csrf_client.post(
        "/contact",
        data={"nome": "A", "email": "no", "tipo": "lamentela", "messaggio": "corto"},
    )
    assert risposta.status_code == 400


def test_la_ricerca_per_tag_ignora_nome_bio_e_aree(csrf_client):
    rotta = _file("app", "routes", "consultants.py")
    inizio = rotta.index("if solo_tag:")
    ramo = rotta[inizio:rotta.index("continue", inizio)]
    for campo in ("nome", "cognome", "descrizione", "professione", "aree_interesse"):
        assert campo not in ramo, campo
    assert "User.tags" in ramo

    token = f"kivu{secrets.token_hex(3)}"
    with Session(engine) as s:
        per_nome = User(
            email=f"nome-{token}@test.local", password_md5="x", confirmed=1,
            nome=token, cognome="Rossi", descrizione=f"bio {token}",
            professione=token, aree_interesse=token, tags='["altro"]',
            is_verified=True, stripe_onboarding_complete=True, prezzo_consulenza=40,
        )
        per_tag = User(
            email=f"tag-{token}@test.local", password_md5="x", confirmed=1,
            nome="Lucia", cognome="Bianchi", tags=json.dumps([token]),
            is_verified=True, stripe_onboarding_complete=True, prezzo_consulenza=40,
        )
        s.add(per_nome)
        s.add(per_tag)
        s.commit()
        s.refresh(per_nome)
        s.refresh(per_tag)
        id_nome, id_tag = per_nome.id, per_tag.id

    try:
        pagina = csrf_client.get(f"/consultants?search={token}&solo_tag=1").text
        assert f'/user/{id_tag}"' in pagina
        assert f'/user/{id_nome}"' not in pagina
    finally:
        with Session(engine) as s:
            for uid in (id_nome, id_tag):
                utente = s.get(User, uid)
                if utente:
                    s.delete(utente)
            s.commit()


def test_orari_e_community_e_dicitura_nel_sorgente():
    base = _file("app", "templates", "base.html")
    assert "momentoNelTuoFuso" in base and "etichettaSlot" in base
    prenota = _file("app", "templates", "booking.html")
    assert prenota.count("etichettaFuso") >= 2
    assert "etichettaSlot" in prenota
    assert 'id="nomeFusoPrenotazione"' in prenota
    blocco_telefono = prenota.split("@media (max-width: 768px)", 1)[1].split("@media", 1)[0]
    assert "repeat(2, minmax(0, 1fr))" in blocco_telefono
    desktop = prenota.split("@media (max-width: 768px)", 1)[0]
    assert "repeat(2, minmax(0, 1fr))" not in desktop

    community = _file("app", "templates", "community.html")
    assert "margin-left: auto" in community
    assert "box-sizing: border-box" in community

    profilo = _file("app", "templates", "profile.html")
    assert "Le richieste di consulenza in arrivo si confermano da sole" in profilo
    assert "✅ Conferma automatica delle prenotazioni" not in profilo
    assert "Attiva: chi paga prenota subito." not in profilo
    assert 'id="prenotazione-${b.id}"' in profilo

    chat = _file("app", "templates", "chat_widget.html")
    assert "appenaArrivato" in chat
    assert "15 * 60 * 1000" in chat


def test_il_badge_vale_solo_per_le_categorie_segnate():
    utente = User(
        email="v@test.local", password_md5="x", is_verified=True,
        category_id=3, selected_subcategories="[5]", verified_category_ids=None,
    )
    assert id_categorie_verificate(utente) == {3, 5}
    registra_verifica_iniziale(utente)
    utente.category_id = 9
    registra_verifica_iniziale(utente)
    assert id_categorie_verificate(utente) == {3, 5}

    gia = User(
        email="g@test.local", password_md5="x", is_verified=True,
        category_id=3, verified_category_ids=None,
    )
    scrivi_categorie_verificate(gia, categorie_dichiarate(gia))
    gia.category_id = 9
    registra_verifica_iniziale(gia)
    assert 9 not in id_categorie_verificate(gia)

    senza = User(email="s@test.local", password_md5="x", is_verified=False, category_id=3)
    assert id_categorie_verificate(senza) == set()
    esplicito = User(
        email="e@test.local", password_md5="x", is_verified=True,
        category_id=3, verified_category_ids="[]",
    )
    assert id_categorie_verificate(esplicito) == set()


def test_admin_segna_la_categoria_e_il_badge_compare_solo_li(csrf_client):
    suf = secrets.token_hex(3)
    with Session(engine) as s:
        casa = Category(name=f"Casa {suf}", slug=f"casa-{suf}", icon="🏠")
        mutui = Category(name=f"Mutui {suf}", slug=f"mutui-{suf}", icon="🏦")
        s.add(casa)
        s.add(mutui)
        s.commit()
        s.refresh(casa)
        s.refresh(mutui)
        consulente = User(
            email=f"badge-{suf}@test.local", password_md5=hash_password("prova-password"),
            confirmed=1, nome="Giulia", cognome="Neri",
            is_verified=True, category_id=casa.id,
            verified_category_ids=json.dumps([mutui.id]),
            stripe_onboarding_complete=True, prezzo_consulenza=50,
        )
        admin = User(
            email=f"admin-{suf}@test.local", password_md5=hash_password("prova-password"),
            confirmed=1, nome="Ada", cognome="Min", user_type_id=2,
        )
        s.add(consulente)
        s.add(admin)
        s.commit()
        s.refresh(consulente)
        s.refresh(admin)
        ids = (casa.id, mutui.id, consulente.id, admin.id, admin.email)

    casa_id, mutui_id, consulente_id, admin_id, admin_email = ids
    try:
        scheda = csrf_client.get(f"/user/{consulente_id}").text
        assert "badge-verificato-categoria" not in scheda

        _entra(csrf_client, admin_email)
        salvato = csrf_client.post(
            f"/admin/api/users/{consulente_id}/categorie-verificate",
            json={"category_ids": [casa_id]},
        )
        assert salvato.status_code == 200
        scheda = csrf_client.get(f"/user/{consulente_id}").text
        assert "badge-verificato-categoria" in scheda
        assert f"Casa {suf}" in scheda

        csrf_client.get("/logout")
        reset_rate_limit()
        marker = 'name="csrf-token" content="'
        pagina = csrf_client.get("/login").text
        if marker in pagina:
            csrf_client.headers.update({
                "X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]
            })
        visita = csrf_client.post(
            f"/admin/api/users/{consulente_id}/categorie-verificate",
            json={"category_ids": [mutui_id]},
        )
        assert visita.status_code == 401
    finally:
        csrf_client.get("/logout")
        with Session(engine) as s:
            for modello, chiave in (
                (User, consulente_id), (User, admin_id),
                (Category, casa_id), (Category, mutui_id),
            ):
                riga = s.get(modello, chiave)
                if riga:
                    s.delete(riga)
            s.commit()


def test_avviso_pagamento_si_apre_e_sparisce_quando_risolto(csrf_client):
    suf = secrets.token_hex(3)
    with Session(engine) as s:
        cliente = User(email=f"cli-{suf}@test.local", password_md5="x", confirmed=1, nome="Chiara")
        consulente = User(
            email=f"con-{suf}@test.local", password_md5=hash_password("prova-password"),
            confirmed=1, nome="Paolo",
        )
        s.add(cliente)
        s.add(consulente)
        s.commit()
        s.refresh(cliente)
        s.refresh(consulente)
        giorno = (now_italy_naive() - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
        prenotazione = Booking(
            client_user_id=cliente.id, consultant_user_id=consulente.id,
            booking_date=giorno, start_time="10:00", end_time="11:00",
            duration_minutes=60, price=60, status="completed",
            payment_status="held", payment_method="stripe",
        )
        s.add(prenotazione)
        s.commit()
        s.refresh(prenotazione)
        contestazione = Dispute(
            booking_id=prenotazione.id, client_user_id=cliente.id,
            consultant_user_id=consulente.id,
            description="Non e' andata come previsto", status="open",
        )
        s.add(contestazione)
        vera = Notification(
            user_id=consulente.id, type="booking_confirmed",
            title="Prenotazione confermata", message="Una prenotazione vera",
        )
        s.add(vera)
        s.commit()
        dati = (consulente.id, consulente.email, prenotazione.id, contestazione.id, cliente.id)

    consulente_id, email, prenotazione_id, contestazione_id, cliente_id = dati
    try:
        from app.scheduler import release_booking_payment
        release_booking_payment(prenotazione_id)
        with Session(engine) as s:
            creata = s.exec(select(Notification).where(
                Notification.user_id == consulente_id,
                Notification.type == "payment_hold",
                Notification.related_booking_id == prenotazione_id,
            )).all()
            assert len(creata) == 1
            assert creata[0].action_url == f"/profile#prenotazione-{prenotazione_id}"
            # Una riga vecchia, salvata prima che l'indirizzo ci fosse.
            creata[0].action_url = None
            s.add(creata[0])
            s.commit()
            avviso_id = creata[0].id

        _entra(csrf_client, email)
        elenco = csrf_client.get("/api/notifications").json()
        pagamenti = [n for n in elenco if n["type"] == "payment_hold"]
        assert pagamenti
        assert pagamenti[0]["action_url"] == f"/profile#prenotazione-{prenotazione_id}"
        assert any(n["type"] == "booking_confirmed" for n in elenco)
        assert csrf_client.get("/api/notifications/unread/count").json()["count"] >= 1

        with Session(engine) as s:
            disputa = s.get(Dispute, contestazione_id)
            disputa.status = "resolved"
            s.add(disputa)
            s.commit()

        elenco = csrf_client.get("/api/notifications").json()
        assert all(n["type"] != "payment_hold" for n in elenco)
        assert any(n["type"] == "booking_confirmed" for n in elenco)
        with Session(engine) as s:
            riga = s.get(Notification, avviso_id)
            assert riga.is_read
        conteggio = csrf_client.get("/api/notifications/unread/count").json()["count"]
        assert conteggio == 1  # resta la conferma, non il pagamento risolto
    finally:
        csrf_client.get("/logout")
        with Session(engine) as s:
            for notifica in s.exec(select(Notification).where(
                Notification.user_id.in_([consulente_id, cliente_id])
            )).all():
                s.delete(notifica)
            s.commit()
            disputa = s.get(Dispute, contestazione_id)
            if disputa:
                s.delete(disputa)
            prenotazione = s.get(Booking, prenotazione_id)
            if prenotazione:
                s.delete(prenotazione)
            s.commit()
            for uid in (consulente_id, cliente_id):
                utente = s.get(User, uid)
                if utente:
                    s.delete(utente)
            s.commit()
