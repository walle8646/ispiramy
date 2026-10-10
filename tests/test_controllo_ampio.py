"""Controlli del giro ampio: buchi che i test già presenti non chiudevano.

Dati finti, niente email vere, niente Stripe. Dove il sito chiama un servizio
esterno, il test si ferma prima oppure lo sostituisce.
"""
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import AvailabilityBlock, Booking, Category, CommunityQuestion, Review, User
from app.routes.auth import (
    MIN_PASSWORD_LENGTH,
    VALIDITA_CODICE_RESET,
    VALIDITA_CODICE_VERIFICA,
    corpo_email_reset,
    corpo_email_verifica,
)
from app.routes.booking import blocca_agenda, hours_until_booking, slot_gia_prenotato
from app.utils.file_immagini import tipo_immagine
from app.utils.orari import ITALY_TZ, istante_italiano, now_italy_naive, ore_reali_fino_a
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit

ROMA = ZoneInfo("Europe/Rome")
UTC = ZoneInfo("UTC")
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
HTML = b"<html><script>alert(1)</script></html>"


def _utente(session, **extra):
    dati = dict(
        email=f"qa-{secrets.token_hex(4)}@test.local",
        password_md5=hash_password("segret8a"),
        confirmed=1,
        user_type_id=1,
        nome="Prova",
        cognome="Utente",
    )
    dati.update(extra)
    utente = User(**dati)
    session.add(utente)
    session.commit()
    session.refresh(utente)
    return utente


def _prenotazione(session, cliente, consulente, giorno, inizio="10:00", fine="11:00", stato="confirmed"):
    data = datetime.strptime(giorno, "%Y-%m-%d")
    riga = Booking(
        client_user_id=cliente,
        consultant_user_id=consulente,
        booking_date=data.replace(hour=int(inizio[:2]), minute=int(inizio[3:5])),
        start_time=inizio,
        end_time=fine,
        duration_minutes=60,
        status=stato,
        price=40,
        payment_status="pending",
    )
    session.add(riga)
    session.commit()
    session.refresh(riga)
    return riga


def _login(client, email):
    reset_rate_limit()
    risposta = client.post("/api/login", data={"email": email, "password": "segret8a"})
    assert risposta.status_code == 200, risposta.text


# --------------------------------------------------------------------------- orari


def test_il_cambio_ora_di_ottobre_non_accorcia_la_prima_delle_due_230():
    """Il 25 ottobre 2026 le 2:30 ci sono due volte. Alle 4:00 mancano 2 ore e mezza
    la prima volta, un'ora e mezza la seconda. L'orologio, da solo, dice 1 ora e mezza
    in tutti e due i casi e bloccherebbe un annullamento ancora in tempo."""
    inizio = datetime(2026, 10, 25, 4, 0)
    prima = datetime(2026, 10, 25, 2, 30, tzinfo=ROMA, fold=0)
    seconda = datetime(2026, 10, 25, 2, 30, tzinfo=ROMA, fold=1)
    assert ore_reali_fino_a(inizio, prima) == pytest.approx(2.5)
    assert ore_reali_fino_a(inizio, seconda) == pytest.approx(1.5)
    # lo stesso istante detto in UTC, non in ora di Roma
    assert ore_reali_fino_a(inizio, datetime(2026, 10, 25, 0, 30, tzinfo=UTC)) == pytest.approx(2.5)
    assert ore_reali_fino_a(inizio, datetime(2026, 10, 25, 1, 30, tzinfo=UTC)) == pytest.approx(1.5)

    prenotazione = Booking(
        client_user_id=1, consultant_user_id=2,
        booking_date=datetime(2026, 10, 25), start_time="04:00", end_time="05:00",
        duration_minutes=60,
    )
    assert not (hours_until_booking(prenotazione, prima) < 2)
    assert hours_until_booking(prenotazione, seconda) < 2


def test_un_orario_che_non_esiste_a_marzo_non_viene_contato():
    """Il 29 marzo 2026 le 2:30 non esistono: l'orologio salta alle 3."""
    spostato = istante_italiano(datetime(2026, 3, 29, 2, 30))
    assert (spostato.hour, spostato.minute) == (3, 30)
    valido = istante_italiano(datetime(2026, 6, 15, 10, 0))
    assert (valido.hour, valido.minute) == (10, 0)


def test_a_due_ore_esatte_si_puo_ancora_annullare():
    inizio = datetime(2026, 11, 2, 12, 0)
    adesso = datetime(2026, 11, 2, 10, 0, tzinfo=ITALY_TZ)
    un_secondo_dopo = datetime(2026, 11, 2, 10, 0, 1, tzinfo=ITALY_TZ)
    prenotazione = Booking(
        client_user_id=1, consultant_user_id=2,
        booking_date=inizio, start_time="12:00", end_time="13:00", duration_minutes=60,
    )
    assert hours_until_booking(prenotazione, adesso) == pytest.approx(2.0)
    assert not (hours_until_booking(prenotazione, adesso) < 2)
    assert hours_until_booking(prenotazione, un_secondo_dopo) < 2


def test_annullare_dentro_e_fuori_dalle_due_ore(csrf_client):
    reset_rate_limit()
    with Session(engine) as s:
        cliente = _utente(s)
        consulente = _utente(s, stripe_onboarding_complete=True, prezzo_consulenza=40)
        fra_poco = now_italy_naive() + timedelta(minutes=90)
        fra_tre_ore = now_italy_naive() + timedelta(hours=3)
        vicina = _prenotazione(
            s, cliente.id, consulente.id, fra_poco.strftime("%Y-%m-%d"),
            fra_poco.strftime("%H:%M"), (fra_poco + timedelta(hours=1)).strftime("%H:%M"),
        )
        lontana = _prenotazione(
            s, cliente.id, consulente.id, fra_tre_ore.strftime("%Y-%m-%d"),
            fra_tre_ore.strftime("%H:%M"), (fra_tre_ore + timedelta(hours=1)).strftime("%H:%M"),
        )
        email, vicina_id, lontana_id = cliente.email, vicina.id, lontana.id
    try:
        _login(csrf_client, email)
        rifiutata = csrf_client.delete(f"/api/booking/cancel/{vicina_id}")
        assert rifiutata.status_code == 400
        assert "2" in rifiutata.json()["detail"]
        accettata = csrf_client.delete(f"/api/booking/cancel/{lontana_id}")
        assert accettata.status_code == 200, accettata.text
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


# --------------------------------------------------------------------------- doppia prenotazione


def test_un_orario_che_si_accavalla_e_gia_preso():
    giorno = "2031-04-10"
    with Session(engine) as s:
        cliente = _utente(s)
        altro = _utente(s)
        consulente = _utente(s)
        _prenotazione(s, cliente.id, consulente.id, giorno, "10:00", "11:00", "confirmed")
        _prenotazione(s, cliente.id, consulente.id, giorno, "15:00", "16:00", "completed")
        _prenotazione(s, cliente.id, consulente.id, giorno, "18:00", "19:00", "cancelled")
        consulente_id = consulente.id
        altro_id = altro.id
    with Session(engine) as s:
        assert slot_gia_prenotato(s, consulente_id, giorno, "10:15", "11:15")
        assert slot_gia_prenotato(s, consulente_id, giorno, "15:30", "16:30")
        assert not slot_gia_prenotato(s, consulente_id, giorno, "18:00", "19:00")
        assert not slot_gia_prenotato(s, consulente_id, giorno, "11:00", "12:00")
        # il bordo delle 11:00 non entra nella consulenza finita alle 11:00
        assert not slot_gia_prenotato(s, altro_id, giorno, "10:00", "11:00")


def test_due_richieste_insieme_ne_lasciano_una_sola():
    giorno = "2031-05-02"
    with Session(engine) as s:
        uno = _utente(s)
        due = _utente(s)
        consulente = _utente(s)
        ids = (uno.id, due.id, consulente.id)

    def prova(cliente_id):
        with Session(engine) as s:
            blocca_agenda(s, ids[2])
            if slot_gia_prenotato(s, ids[2], giorno, "10:00", "11:00"):
                return False
            s.add(Booking(
                client_user_id=cliente_id, consultant_user_id=ids[2],
                booking_date=datetime(2031, 5, 2, 10, 0),
                start_time="10:00", end_time="11:00", duration_minutes=60,
                status="pending_payment", price=40,
            ))
            s.commit()
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        esiti = list(pool.map(prova, ids[:2]))
    assert esiti.count(True) == 1
    with Session(engine) as s:
        prese = s.exec(select(Booking).where(Booking.consultant_user_id == ids[2])).all()
    assert len(prese) == 1


def test_la_prenotazione_rifiuta_uno_slot_che_si_accavalla(csrf_client):
    giorno = (now_italy_naive() + timedelta(days=5)).strftime("%Y-%m-%d")
    with Session(engine) as s:
        cliente = _utente(s)
        consulente = _utente(s, stripe_onboarding_complete=True, prezzo_consulenza=60,
                             is_verified=True)
        _prenotazione(s, cliente.id, consulente.id, giorno, "10:00", "11:00")
        email, consulente_id = cliente.email, consulente.id
    try:
        _login(csrf_client, email)
        risposta = csrf_client.post("/api/booking/create", json={
            "consultant_user_id": consulente_id,
            "booking_date": giorno,
            "start_time": "10:15",
            "end_time": "11:15",
            "duration_minutes": 60,
            "price": 60,
            "description": "Vorrei un parere",
        })
        assert risposta.status_code == 409, risposta.text
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


# --------------------------------------------------------------------------- disponibilità


def test_non_si_toglie_una_fascia_con_una_prenotazione(csrf_client):
    giorno = (now_italy_naive() + timedelta(days=6)).strftime("%Y-%m-%d")
    with Session(engine) as s:
        cliente = _utente(s)
        consulente = _utente(s)
        blocco = AvailabilityBlock(
            user_id=consulente.id,
            date=datetime.strptime(giorno, "%Y-%m-%d"),
            start_time=datetime.strptime("10:00", "%H:%M").time(),
            end_time=datetime.strptime("12:00", "%H:%M").time(),
            total_minutes=120,
        )
        s.add(blocco)
        s.commit()
        s.refresh(blocco)
        _prenotazione(s, cliente.id, consulente.id, giorno, "10:00", "11:00")
        email, blocco_id, consulente_id = consulente.email, blocco.id, consulente.id
    try:
        _login(csrf_client, email)
        rifiutato = csrf_client.post("/api/availability/save", data={
            "date": giorno,
            "blocks": '[{"start_time":"14:00","end_time":"16:00"}]',
        })
        assert rifiutato.status_code == 409, rifiutato.text
        with Session(engine) as s:
            rimasti = s.exec(select(AvailabilityBlock).where(AvailabilityBlock.user_id == consulente_id)).all()
            prenotata = s.exec(select(Booking).where(Booking.consultant_user_id == consulente_id)).one()
        assert len(rimasti) == 1
        assert prenotata.status == "confirmed"
        cancellato = csrf_client.delete(f"/api/availability/block/{blocco_id}")
        assert cancellato.status_code == 409
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


# --------------------------------------------------------------------------- file e sicurezza


def test_i_byte_decidono_se_un_file_e_una_foto():
    assert tipo_immagine(JPEG)[0] == "jpg"
    assert tipo_immagine(PNG)[0] == "png"
    assert tipo_immagine(b"GIF89a" + b"x" * 8)[0] == "gif"
    assert tipo_immagine(HTML) is None


def test_una_foto_finta_non_arriva_alla_moderazione(csrf_client, monkeypatch):
    def non_chiamare(*args, **kwargs):
        raise AssertionError("la moderazione non deve vedere un file che non è una foto")

    monkeypatch.setattr("app.routes.user_profile.modera_immagine", non_chiamare)
    monkeypatch.setattr("app.routes.community.modera_immagine", non_chiamare)
    with Session(engine) as s:
        utente = _utente(s)
        email = utente.email
    try:
        _login(csrf_client, email)
        profilo = csrf_client.post(
            "/api/upload-profile-picture",
            files={"file": ("evil.html", HTML, "image/jpeg")},
        )
        assert profilo.status_code == 400
        community = csrf_client.post(
            "/api/community/upload-image",
            files={"file": ("foto.jpg", HTML, "image/jpeg")},
        )
        assert community.status_code == 400
        gif = csrf_client.post(
            "/api/community/upload-image",
            files={"file": ("anim.gif", b"GIF89a" + b"x" * 20, "image/gif")},
        )
        assert gif.status_code == 400
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


def test_un_allegato_html_rinominato_non_entra_in_chat(csrf_client):
    with Session(engine) as s:
        cliente = _utente(s)
        consulente = _utente(s)
        prenotata = _prenotazione(
            s, cliente.id, consulente.id, "2031-06-01", "10:00", "11:00",
        )
        email, prenotata_id = cliente.email, prenotata.id
    try:
        _login(csrf_client, email)
        risposta = csrf_client.post(
            f"/api/booking/{prenotata_id}/chat/upload-attachment",
            files={"file": ("appunti.jpg", HTML, "text/html")},
        )
        assert risposta.status_code == 400
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


def test_non_si_entra_nelle_cose_di_un_altro_cambiando_id(csrf_client):
    with Session(engine) as s:
        proprietario = _utente(s)
        estraneo = _utente(s, user_type_id=1)
        consulente = _utente(s)
        prenotata = _prenotazione(s, proprietario.id, consulente.id, "2031-07-01")
        domanda = CommunityQuestion(
            user_id=proprietario.id, title="Domanda mia", description="Testo abbastanza lungo",
        )
        s.add(domanda)
        s.commit()
        s.refresh(domanda)
        email, prenotata_id, domanda_id = estraneo.email, prenotata.id, domanda.id
    try:
        _login(csrf_client, email)
        assert csrf_client.delete(f"/api/booking/cancel/{prenotata_id}").status_code == 403
        assert csrf_client.get(f"/api/booking/{prenotata_id}/recording").status_code == 403
        assert csrf_client.get(f"/api/booking/{prenotata_id}/chat/messages").status_code == 403
        modifica = csrf_client.put(
            f"/api/community/question/{domanda_id}",
            json={"title": "Rubata", "description": "Non è la mia domanda"},
        )
        assert modifica.status_code == 403
        pagina = csrf_client.get("/admin/", follow_redirects=False)
        assert pagina.status_code == 302
        assert pagina.headers["location"].endswith("/login")
        api = csrf_client.post("/admin/api/disputes/1/status", json={"status": "closed"})
        assert api.status_code == 401
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


def test_senza_csrf_il_login_non_passa(client):
    client.get("/login")
    salvato = client.headers.pop("X-CSRF-Token", None)
    try:
        risposta = client.post("/api/login", data={"email": "nessuno@test.local", "password": "segret8a"})
        assert risposta.status_code == 403
    finally:
        if salvato:
            client.headers["X-CSRF-Token"] = salvato


def test_il_contatto_non_manda_html_e_si_ferma_al_sesto(csrf_client, monkeypatch):
    corpi = []

    def finto(destinatario, oggetto, corpo):
        corpi.append(corpo)
        return True

    monkeypatch.setattr("app.routes.pages.send_email", finto)
    reset_rate_limit()
    email = f"contatto-{secrets.token_hex(3)}@test.local"
    messaggio = "<script>alert(1)</script> vorrei un chiarimento sul servizio"
    for _ in range(5):
        ok = csrf_client.post("/contact", data={
            "nome": "Ada", "email": email, "tipo": "domanda", "messaggio": messaggio,
        }, follow_redirects=False)
        assert ok.status_code == 303
    bloccato = csrf_client.post("/contact", data={
        "nome": "Ada", "email": email, "tipo": "domanda", "messaggio": messaggio,
    })
    assert bloccato.status_code == 429
    assert "<script>" not in corpi[0]
    assert "&lt;script&gt;" in corpi[0]
    reset_rate_limit()


def test_la_descrizione_del_profilo_non_diventa_codice(client):
    with Session(engine) as s:
        utente = _utente(s, descrizione="<script>alert(1)</script>")
        uid = utente.id
    pagina = client.get(f"/user/{uid}")
    assert pagina.status_code == 200
    assert "<script>alert(1)</script>" not in pagina.text


# --------------------------------------------------------------------------- account


def test_registrazione_conferma_e_password_corta(csrf_client):
    reset_rate_limit()
    email = f"nuovo-{secrets.token_hex(4)}@test.local"
    try:
        corta = csrf_client.post("/api/register", data={
            "email": email, "password": "abcde6", "nome": "Ada",
        })
        assert corta.status_code == 400
        assert str(MIN_PASSWORD_LENGTH) in corta.json()["error"]
        solo_numeri = csrf_client.post("/api/register", data={
            "email": email, "password": "12345678", "nome": "Ada",
        })
        assert solo_numeri.status_code == 400

        creata = csrf_client.post("/api/register", data={
            "email": email, "password": "segret8a", "nome": "Ada",
        })
        assert creata.status_code == 201, creata.text
        prima = csrf_client.post("/api/login", data={"email": email, "password": "segret8a"})
        assert prima.status_code == 403

        with Session(engine) as s:
            codice = s.exec(select(User).where(User.email == email)).one().confirmation_code
        verifica = csrf_client.post("/api/verify-email", data={"email": email, "code": codice})
        assert verifica.status_code == 200, verifica.text
        dopo = csrf_client.post("/api/login", data={"email": email, "password": "segret8a"})
        assert dopo.status_code == 200
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()
        with Session(engine) as s:
            utente = s.exec(select(User).where(User.email == email)).first()
            if utente:
                s.delete(utente)
                s.commit()


def test_il_reset_password_usa_il_codice_e_dieci_minuti(csrf_client, monkeypatch):
    reset_rate_limit()
    monkeypatch.setattr("app.routes.auth.generate_verification_code", lambda: "654321")
    with Session(engine) as s:
        utente = _utente(s)
        email = utente.email
    try:
        chiesto = csrf_client.post("/api/request-password-reset", data={"email": email})
        assert chiesto.status_code == 200
        debole = csrf_client.post("/api/reset-password", data={
            "email": email, "code": "654321", "new_password": "12345678",
        })
        assert debole.status_code == 400
        fatto = csrf_client.post("/api/reset-password", data={
            "email": email, "code": "654321", "new_password": "nuova9xy",
        })
        assert fatto.status_code == 200, fatto.text
        csrf_client.get("/logout")
        pagina = csrf_client.get("/login")
        marker = 'name="csrf-token" content="'
        if marker in pagina.text:
            csrf_client.headers["X-CSRF-Token"] = pagina.text.split(marker, 1)[1].split('"', 1)[0]
        entra = csrf_client.post("/api/login", data={"email": email, "password": "nuova9xy"})
        assert entra.status_code == 200
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


def test_le_email_di_account_dicono_i_minuti_giusti_e_parlano_inglese():
    minuti_verifica = int(VALIDITA_CODICE_VERIFICA.total_seconds() // 60)
    minuti_reset = int(VALIDITA_CODICE_RESET.total_seconds() // 60)
    italiano, _ = corpo_email_verifica("Ada", "123456", "it")
    inglese, oggetto = corpo_email_verifica("Ada", "123456", "en")
    assert f"{minuti_verifica} minuti" in italiano
    assert "10 minuti" not in italiano
    assert f"{minuti_verifica} minutes" in inglese
    assert "Ciao" not in inglese
    assert "verification" in oggetto.lower()
    reset_it, _ = corpo_email_reset("Ada", "123456", "it")
    reset_en, titolo = corpo_email_reset("Ada", "123456", "en")
    assert f"{minuti_reset} minuti" in reset_it
    assert f"{minuti_reset} minutes" in reset_en
    assert "Hai richiesto" not in reset_en
    assert "password" in titolo.lower()


def test_google_senza_chiavi_torna_al_login(client):
    risposta = client.get("/auth/google", follow_redirects=False)
    assert risposta.status_code == 302
    assert risposta.headers["location"].endswith("/login")


def test_la_sessione_del_browser_dura_un_giorno():
    from pathlib import Path
    radice = Path(__file__).resolve().parent.parent
    main = (radice / "app" / "main.py").read_text(encoding="utf-8")
    auth = (radice / "app" / "routes" / "auth.py").read_text(encoding="utf-8")
    assert "max_age=86400" in main
    assert "timedelta(days=7)" in auth


# --------------------------------------------------------------------------- inglese e pagine pesanti


def test_accesso_registrazione_e_reset_in_inglese(client):
    client.get("/lingua/en")
    try:
        login = client.get("/login").text
        assert "Log in to Ispiramy" in login
        assert "Forgot your password?" in login
        assert "Accedi a Ispiramy" not in login
        assert "Password dimenticata" not in login
        registra = client.get("/register").text
        assert "Sign up for Ispiramy" in registra
        assert "At least 8" in registra
        assert "Minimo 6" not in registra
        assert "Registrati su Ispiramy" not in registra
        reset = client.get("/reset-password").text
        assert "Reset your password" in reset
        assert "Send reset code" in reset
        assert "Reimposta Password" not in reset
    finally:
        client.get("/lingua/it")


def test_home_e_ricerca_non_ripetono_la_query_per_ogni_consulente(client):
    from sqlalchemy import event

    with Session(engine) as s:
        categoria = Category(name=f"Qa {secrets.token_hex(3)}", slug=f"qa-{secrets.token_hex(3)}")
        s.add(categoria)
        s.commit()
        s.refresh(categoria)
        cliente = _utente(s)
        consulenti = []
        for i in range(4):
            consulenti.append(_utente(
                s, category_id=categoria.id, is_verified=True,
                stripe_onboarding_complete=True, prezzo_consulenza=40,
                consulenze_vendute=10**8 + i, nome=f"Vetrina{i}",
            ))
        for consulente in consulenti:
            prenotata = _prenotazione(s, cliente.id, consulente.id, "2031-08-01")
            for n in range(3):
                s.add(Review(
                    booking_id=prenotata.id, reviewer_user_id=cliente.id,
                    consultant_user_id=consulente.id,
                    rating_helpful=5, rating_prepared=4, rating_communication=5,
                    review_token=secrets.token_hex(8),
                ))
        s.commit()
        ids = [u.id for u in consulenti] + [cliente.id, categoria.id]

    catturate = []

    def prima(conn, cursor, statement, parameters, context, executemany):
        catturate.append(statement)

    event.listen(engine, "before_cursor_execute", prima)
    try:
        assert client.get("/").status_code == 200
        recensioni = [sql for sql in catturate if "reviews" in sql.lower()]
        assert len(recensioni) == 1
        assert "group by" in recensioni[0].lower()
        assert not any("category.id =" in sql.lower() or "category.id=" in sql.lower() for sql in catturate)

        catturate.clear()
        assert client.get("/consultants").status_code == 200
        assert not any("category.id =" in sql.lower() or "category.id=" in sql.lower() for sql in catturate)
    finally:
        event.remove(engine, "before_cursor_execute", prima)
        with Session(engine) as s:
            for riga in s.exec(select(Review).where(Review.consultant_user_id.in_(ids))).all():
                s.delete(riga)
            for riga in s.exec(select(Booking).where(Booking.consultant_user_id.in_(ids))).all():
                s.delete(riga)
            for riga in s.exec(select(User).where(User.id.in_(ids))).all():
                s.delete(riga)
            cat = s.get(Category, ids[-1])
            if cat:
                s.delete(cat)
            s.commit()


def test_la_ricerca_regge_molti_consulenti(client):
    with Session(engine) as s:
        creati = []
        for i in range(80):
            creati.append(User(
                email=f"massa-{i}-{secrets.token_hex(3)}@test.local",
                password_md5="x",
                confirmed=1,
                nome=f"Massa{i}",
                is_verified=True,
                stripe_onboarding_complete=True,
                prezzo_consulenza=30 + (i % 5),
            ))
        s.add_all(creati)
        s.commit()
        ids = [u.id for u in creati]
    try:
        pagina = client.get("/consultants")
        assert pagina.status_code == 200
        assert "Massa0" in pagina.text or "Massa1" in pagina.text or pagina.text.count("consultant") > 0
    finally:
        with Session(engine) as s:
            for utente in s.exec(select(User).where(User.id.in_(ids))).all():
                s.delete(utente)
            s.commit()
