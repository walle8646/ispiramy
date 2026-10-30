"""Barra del telefono: la ricerca del computer e la faccia accanto ai messaggi.

Sotto i 768px la lente non deve solo aprire la pagina Esperti: apre lo stesso
campo della barra del computer, e chi ha fatto l'accesso vede il proprio
tondo a destra dell'icona dei messaggi. Sopra i 768px la barra resta quella
di prima.
"""
import io
import os
import secrets

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _blocco_telefono():
    css = _file("app", "static", "mobile.css")
    return css[css.index("ASPETTO DA APP"):css.index("Sul computer la barra e il foglio non esistono")]


def test_sul_telefono_la_lente_apre_la_ricerca_della_barra():
    base = _file("app", "templates", "base.html")
    assert 'id="apriCercaTelefono"' in base
    assert 'id="ricercaBarra"' in base
    assert 'id="globalSearch"' in base
    assert "apriRicercaTelefono" in base
    assert "max-width: 768px" in base
    # stesso comportamento del computer: le due destinazioni e la query
    assert "function searchConsultants" in base
    assert "/consultants?search=" in base
    assert "function searchCommunity" in base
    assert "/community?search=" in base
    assert "searchQuery.length > 4" in base

    telefono = _blocco_telefono()
    assert "flex-wrap: nowrap !important;" in telefono
    aperto = telefono[telefono.index(".navbar .search-wrapper.aperto"):]
    aperto = aperto[:aperto.index("}")]
    assert "position: absolute !important;" in aperto
    assert "display: block !important;" in aperto


def test_la_faccia_sta_accanto_ai_messaggi_solo_da_loggati():
    base = _file("app", "templates", "base.html")
    inizio = base.index('class="avatar-telefono"')
    assert base.rfind('id="headerChatButton"', 0, inizio) != -1
    assert 'href="/profile"' in base[inizio - 40:inizio + 40]
    assert "avatar-iniziali" in base
    dopo = base.index("{% else %}", inizio)
    fine = base.index("{% endif %}", dopo)
    ramo_ospite = base[dopo:fine]
    assert "avatar-telefono" not in ramo_ospite
    assert 'href="/login"' in ramo_ospite
    assert "nav.registrati" in ramo_ospite
    # sul computer il menu dell'avatar resta dov'era
    assert "navbar-profile-pic" in base

    telefono = _blocco_telefono()
    faccia = telefono[telefono.index(".navbar .avatar-telefono"):]
    faccia = faccia[:faccia.index("}")]
    assert "display: inline-flex !important;" in faccia
    assert "border-radius: 50%" in faccia

    computer = _file("app", "static", "mobile.css")
    coda = computer[computer.index("Sul computer la barra e il foglio non esistono"):]
    assert ".avatar-telefono" in coda[:400]


def _entra(client, email, password):
    from app.utils.rate_limit import reset_rate_limit

    reset_rate_limit()
    marker = 'name="csrf-token" content="'
    pagina = client.get("/login").text
    if marker in pagina:
        client.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    risposta = client.post("/api/login", data={"email": email, "password": password})
    assert risposta.status_code == 200, risposta.text


def _esci(client):
    from app.utils.lingue_ui import COOKIE

    client.get("/logout")
    client.cookies.delete(COOKIE, path="/")
    client.get("/lingua/it", follow_redirects=False)


def _crea(nome, cognome, foto=None, anonimo=False):
    from sqlmodel import Session

    from app.database import engine
    from app.models import User
    from app.utils.password import hash_password

    email = f"barra-{secrets.token_hex(4)}@test.local"
    with Session(engine) as s:
        utente = User(
            email=email,
            password_md5=hash_password("prova-password"),
            confirmed=1,
            nome=nome,
            cognome=cognome,
            profile_picture=foto,
            is_anonymous=anonimo,
        )
        s.add(utente)
        s.commit()
        return email, utente.id


def _cancella(utente_id):
    from sqlmodel import Session

    from app.database import engine
    from app.models import User

    with Session(engine) as s:
        riga = s.get(User, utente_id)
        if riga:
            s.delete(riga)
            s.commit()


def test_da_loggato_si_vedono_le_iniziali_e_da_ospite_no(client):
    email, utente_id = _crea("Valerio", "Di Dio")
    try:
        ospiti = client.get("/").text
        assert 'id="apriCercaTelefono"' in ospiti
        assert 'id="globalSearch"' in ospiti
        assert 'class="avatar-telefono"' not in ospiti
        assert 'href="/login"' in ospiti

        _entra(client, email, "prova-password")
        pagina = client.get("/").text
        assert 'class="avatar-telefono"' in pagina
        assert 'href="/profile"' in pagina
        assert 'class="avatar-iniziali">VD<' in pagina
        assert "navbar-profile-pic" in pagina
    finally:
        _esci(client)
        _cancella(utente_id)


def test_con_la_foto_le_iniziali_restano_di_riserva(client):
    email, utente_id = _crea("Ada", "Lovelace", foto="https://example.com/volto.png")
    try:
        _entra(client, email, "prova-password")
        pagina = client.get("/").text
        assert 'src="https://example.com/volto.png"' in pagina
        assert 'class="avatar-iniziali" hidden>AL<' in pagina
    finally:
        _esci(client)
        _cancella(utente_id)


def test_senza_nome_e_in_inglese_la_barra_resta_chiara(client):
    from app.utils.lingue_ui import COOKIE

    email, utente_id = _crea(None, None)
    try:
        _entra(client, email, "prova-password")
        pagina = client.get("/").text
        iniziale = email[:1].upper()
        assert f'class="avatar-iniziali">{iniziale}<' in pagina

        client.cookies.delete(COOKIE, path="/")
        client.get("/lingua/en", follow_redirects=False)
        inglese = client.get("/").text
        assert 'lang="en"' in inglese
        assert "Search for an expert or a topic..." in inglese
        assert "Search the experts" in inglese
        assert 'aria-label="Search"' in inglese
        assert 'class="avatar-telefono"' in inglese
        assert "Cerca un Ispiramy" not in inglese
    finally:
        _esci(client)
        _cancella(utente_id)
