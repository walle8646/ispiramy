"""L'aspetto da app sul telefono: la barra delle schede in basso.

Il menu a panino nascondeva tutto dietro due tocchi ciechi. Ora le
destinazioni stanno sempre sotto il pollice, dove le cerca chi usa le app.
Queste sono le regole che, se saltano, riportano il sito a sembrare un sito.
"""
import io
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def test_le_schede_portano_dove_si_va_davvero():
    base = _file("app", "templates", "base.html")
    schede = base[base.index('<nav class="schede"'):base.index("</nav>", base.index('<nav class="schede"'))]
    for dove in ('href="/"', 'href="/consultants"', 'href="/community"'):
        assert dove in schede, f"manca la scheda {dove}"
    # chi ha fatto l'accesso ha i messaggi, gli altri l'accesso
    assert 'href="/messaggi"' in schede and 'href="/login"' in schede
    assert "apriMenuTelefono()" in schede, "la quinta scheda apre il resto"


def test_la_scheda_di_dove_sei_e_evidenziata():
    """Senza, non si capisce in che sezione si e': e' la prima cosa che
    un'app comunica."""
    base = _file("app", "templates", "base.html")
    assert "request.url.path" in base
    css = _file("app", "static", "mobile.css")
    assert ".scheda.attiva" in css


def test_la_barra_non_copre_il_fondo_delle_pagine():
    """La barra sta sopra il contenuto: senza spazio in fondo, l'ultima riga
    di ogni pagina resta sotto e non si legge."""
    css = _file("app", "static", "mobile.css")
    telefono = css[css.index("ASPETTO DA APP"):]
    assert "padding-bottom: calc(62px" in telefono
    # e quello che prima stava incollato in fondo deve salire
    assert ".ask-button-sticky-bottom" in telefono
    assert ".chat-widget" in telefono


def test_sul_computer_non_cambia_niente():
    css = _file("app", "static", "mobile.css")
    coda = css[css.index("Sul computer la barra e il foglio non esistono"):]
    assert "@media (min-width: 769px)" in css
    for regola in (".schede", ".foglio"):
        assert regola in coda


def test_il_menu_a_panino_lascia_il_posto_alle_schede():
    css = _file("app", "static", "mobile.css")
    telefono = css[css.index("ASPETTO DA APP"):]
    assert ".navbar .hamburger-btn" in telefono
    # in alto restano campanella e chat: le notifiche non si raggiungono altrove
    assert ".navbar .navbar-links" in telefono


def test_ogni_pagina_sa_chi_sei(client):
    """Meta' delle rotte non passava current_user al template: su messaggi,
    "Come funziona" e le pagine informative la barra diceva "Accedi" a chi
    l'accesso lo aveva gia' fatto."""
    from app.main import templates

    nomi = [p.__name__ for p in templates.context_processors]
    assert "_utente_in_ogni_pagina" in nomi

    # la pagina non passa current_user: lo deve mettere il processore
    assert client.get("/come-funziona").status_code == 200


def test_le_conversazioni_si_leggono():
    """L'API risponde {conversations: [...]}, non una lista: letta come lista
    la pagina dei messaggi diceva sempre "Errore di connessione"."""
    pagina = _file("app", "templates", "messages_inbox.html")
    assert "risposta.conversations" in pagina
