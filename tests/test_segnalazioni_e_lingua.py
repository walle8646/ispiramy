"""Inglese sul resto del sito, e le segnalazioni della community.

Le chiavi usate nei template devono esistere in italiano e in inglese.
Una domanda si segnala una volta, con un motivo, e chi verifica può
ignorarla o nasconderla. Le risposte in community non esistono: qui non
se ne aggiunge nessuna.
"""
import os
import re
import secrets

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import CommunityQuestion, CommunityReport, Notification, User
from app.utils.lingue_ui import traduci
from app.utils.notification_email import generate_email_html
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit
from app.utils.testi_notifiche import localizza_notifica
from tests.test_matrice_call_contestazioni import _login

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOTIVO = "Questa domanda contiene un insulto e non riguarda la categoria scelta."


def _file(*pezzi):
    with open(os.path.join(RADICE, *pezzi), encoding="utf-8") as f:
        return f.read()


def _catalogo(lingua):
    import json
    return json.loads(_file("app", "traduzioni", f"{lingua}.json"))


def test_ogni_chiave_usata_esiste_nelle_due_lingue():
    """Una pagina inglese che stampa la chiave cruda è una pagina non tradotta."""
    italiano, inglese = _catalogo("it"), _catalogo("en")
    assert set(italiano) == set(inglese)
    usate = set()
    schema = re.compile(r"""(?:\bt|traduci)\(\s*['\"]([a-z][a-z0-9_.]*[a-z0-9])['\"]""")
    for cartella, _, nomi in os.walk(os.path.join(RADICE, "app")):
        if "traduzioni" in cartella.split(os.sep):
            continue
        for nome in nomi:
            if not nome.endswith((".html", ".py")):
                continue
            testo = open(os.path.join(cartella, nome), encoding="utf-8").read()
            usate.update(schema.findall(testo))
    mancanti = sorted(k for k in usate if k not in italiano or not italiano[k].strip() or not inglese[k].strip())
    assert not mancanti, mancanti


def test_le_mail_in_inglese_non_ritornano_in_italiano():
    dati = {
        "consultant_name": "Ada",
        "client_name": "Bea",
        "user_name": "Bea",
        "other_user_name": "Ada",
        "date": "1 May",
        "time": "10:00",
        "duration": "60",
        "action_url": "https://example.test/profilo",
        "outcome_it": "Contestazione accolta: rimborso.",
        "outcome_en": "Dispute upheld: refund.",
    }
    italiana = generate_email_html("booking_confirmed.html", dati)
    assert "48 ore" in italiana and "48 hours" in italiana
    inglese = generate_email_html("booking_confirmed.html", dati, lingua="en")
    assert "48 hours" in inglese
    assert "Questa è un'email automatica" not in inglese
    assert "Il compenso del consulente" not in inglese
    assert 'lang="en"' in inglese
    decisione = generate_email_html("dispute_decided.html", dati, lingua="en")
    assert "Dispute upheld: refund." in decisione
    assert "Contestazione accolta" not in decisione
    # la mail italiana della decisione resta in entrambe le lingue, come prima
    decisione_it = generate_email_html("dispute_decided.html", dati)
    assert "Contestazione accolta" in decisione_it
    assert "Dispute upheld" in decisione_it


def test_la_notifica_in_app_segue_la_lingua_del_profilo():
    titolo, testo = localizza_notifica(
        "en", "booking_confirmed", "Nuova prenotazione", "Ada ha prenotato",
        {"client_name": "Bea", "date": "1 May", "time": "10:00"},
    )
    assert titolo == "New booking"
    assert "Bea" in testo and "1 May" in testo
    uguale, _ = localizza_notifica("it", "booking_confirmed", "Nuova prenotazione", "Ada ha prenotato", {})
    assert uguale == "Nuova prenotazione"


def _lingua(http, codice):
    from app.utils.lingue_ui import COOKIE
    http.cookies.delete(COOKIE, path="/")
    http.get(f"/lingua/{codice}", follow_redirects=False)


def test_le_pagine_pubbliche_in_inglese_non_hanno_i_titoli_italiani(client):
    _lingua(client, "en")
    attese = {
        "/": "You have a concrete problem. Somewhere there is a person who has already solved it.",
        "/come-funziona": "How Ispiramy works",
        "/faq": "Frequently asked questions",
        "/contact": "Write a complaint",
        "/terms": "48 hours",
        "/community": "Ask your question and get the advice you need",
    }
    vietate = {
        "/": "Trova l'esperto che ha già affrontato la tua sfida",
        "/come-funziona": "Come funziona Ispiramy",
        "/faq": "Domande Frequenti",
        "/contact": "Scrivi una lamentela",
        "/terms": "Il compenso del consulente parte quando il cliente lascia la recensione",
        "/community": "Fai la tua domanda e ottieni la consulenza che cerchi",
    }
    for percorso, frase in attese.items():
        pagina = client.get(percorso)
        assert pagina.status_code == 200, percorso
        assert 'lang="en"' in pagina.text
        assert frase in pagina.text, percorso
        if percorso == "/":
            assert "Find the expert who has already faced your challenge" in pagina.text
            assert "video_homepage_v2.mp4" not in pagina.text
    for percorso, frase in vietate.items():
        assert frase not in client.get(percorso).text, percorso
    _lingua(client, "it")


@pytest.fixture
def scena(client):
    reset_rate_limit()
    password = secrets.token_urlsafe(12)
    with Session(engine) as s:
        def utente(ruolo, tipo):
            u = User(
                email=f"seg-{ruolo}-{secrets.token_hex(4)}@test.local",
                password_md5=hash_password(password),
                confirmed=1,
                nome=ruolo.capitalize(),
                cognome="Prova",
                user_type_id=tipo,
                platform_fee_percent=20,
                prezzo_consulenza=40,
            )
            s.add(u)
            return u

        autore = utente("autore", 1)
        altro = utente("altro", 1)
        estraneo = utente("estraneo", 1)
        verificatore = utente("verificatore", 2)
        s.commit()
        for u in (autore, altro, estraneo, verificatore):
            s.refresh(u)
        domande = []
        for i in range(6):
            d = CommunityQuestion(
                user_id=autore.id,
                title=f"Domanda segnalabile {secrets.token_hex(3)} {i}",
                description="Testo abbastanza lungo da essere una domanda vera della community, senza insulti.",
                validation=True,
            )
            s.add(d)
            domande.append(d)
        s.commit()
        for d in domande:
            s.refresh(d)
        dati = {
            "password": password,
            "autore": autore.id,
            "altro": altro.id,
            "estraneo": estraneo.id,
            "verificatore": verificatore.id,
            "email_autore": autore.email,
            "email_altro": altro.email,
            "email_estraneo": estraneo.email,
            "email_verificatore": verificatore.email,
            "domande": [d.id for d in domande],
            "titoli": {d.id: d.title for d in domande},
        }
    yield dati
    reset_rate_limit()
    ids = [dati["autore"], dati["altro"], dati["estraneo"], dati["verificatore"]]
    with Session(engine) as s:
        for riga in s.exec(select(CommunityReport).where(CommunityReport.question_id.in_(dati["domande"]))).all():
            s.delete(riga)
        s.commit()
        for d in s.exec(select(CommunityQuestion).where(CommunityQuestion.id.in_(dati["domande"]))).all():
            s.delete(d)
        for n in s.exec(select(Notification).where(Notification.user_id.in_(ids))).all():
            s.delete(n)
        s.commit()
        for uid in ids:
            u = s.get(User, uid)
            if u:
                s.delete(u)
        s.commit()


def _segnala(http, domanda, motivo=MOTIVO):
    return http.post(f"/api/community/{domanda}/report", json={"reason": motivo})


def test_si_segnala_una_volta_e_chi_verifica_puo_nascondere(scena, csrf_client):
    http = csrf_client
    domande = scena["domande"]

    http.get("/logout")
    reset_rate_limit()
    pagina = http.get("/login").text
    marker = 'name="csrf-token" content="'
    if marker in pagina:
        http.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    assert _segnala(http, domande[0]).status_code == 401

    _login(http, scena["email_altro"], scena["password"])
    assert _segnala(http, domande[0], motivo="corto").status_code == 400
    assert _segnala(http, 999999999).status_code == 404
    prima = _segnala(http, domande[0])
    assert prima.status_code == 201, prima.text
    assert _segnala(http, domande[0]).status_code == 409
    for i in range(1, 5):
        risposta = _segnala(http, domande[i])
        assert risposta.status_code == 201, (i, risposta.text)
    sesta = _segnala(http, domande[5])
    assert sesta.status_code == 429, sesta.text

    _login(http, scena["email_autore"], scena["password"])
    # l'autore può segnalare una domanda diversa: sulla prima ha già il limite dell'altro
    propria = _segnala(http, domande[5])
    assert propria.status_code == 201, propria.text

    _login(http, scena["email_estraneo"], scena["password"])
    _lingua(http, "it")
    pagina = http.get("/community")
    assert 'class="btn-report"' in pagina.text
    assert "Segnala" in pagina.text

    assert http.get("/admin/reports", follow_redirects=False).status_code == 302
    assert http.post(f"/admin/api/reports/1/hide").status_code == 401

    _login(http, scena["email_verificatore"], scena["password"])
    lista = http.get("/admin/reports")
    assert lista.status_code == 200
    assert MOTIVO in lista.text
    assert scena["titoli"][domande[0]] in lista.text

    with Session(engine) as s:
        da_ignorare = s.exec(select(CommunityReport).where(
            CommunityReport.question_id == domande[0],
            CommunityReport.status == "open",
        )).first()
        da_nascondere = s.exec(select(CommunityReport).where(
            CommunityReport.question_id == domande[5],
            CommunityReport.status == "open",
        )).first()
        id_ignora, id_nascondi = da_ignorare.id, da_nascondere.id

    assert http.post(f"/admin/api/reports/{id_ignora}/dismiss").status_code == 200
    with Session(engine) as s:
        assert s.get(CommunityQuestion, domande[0]).validation is True
        assert s.get(CommunityReport, id_ignora).status == "dismissed"

    assert http.post(f"/admin/api/reports/{id_nascondi}/hide").status_code == 200
    with Session(engine) as s:
        assert s.get(CommunityQuestion, domande[5]).validation is False
        assert s.get(CommunityReport, id_nascondi).status == "hidden"

    pubblica = http.get("/community").text
    assert scena["titoli"][domande[0]] in pubblica
    assert scena["titoli"][domande[5]] not in pubblica
    _lingua(http, "it")


def test_la_lingua_scelta_resta_sul_profilo(scena, csrf_client):
    http = csrf_client
    _login(http, scena["email_altro"], scena["password"])
    _lingua(http, "en")
    with Session(engine) as s:
        assert s.get(User, scena["altro"]).ui_language == "en"
    profilo = http.get("/profile")
    assert profilo.status_code == 200
    assert "Your profile" in profilo.text
    assert "No consultation scheduled." in profilo.text
    prenota = http.get(f"/book/{scena['autore']}")
    assert prenota.status_code == 200
    assert "Book with" in prenota.text
    assert 'id="nomeFusoPrenotazione">Times in Italian time.</span>' in prenota.text
    _lingua(http, "it")
    with Session(engine) as s:
        assert s.get(User, scena["altro"]).ui_language == "it"
