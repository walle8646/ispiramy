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


def test_le_schede_contenuto_sono_a_tutta_larghezza():
    """Staccate, con ombra e angoli tondi, sembrano finestrelle su un foglio:
    le app usano schede piene separate da una riga."""
    css = _file("app", "static", "mobile.css")
    stile = css[css.index("CONTENUTI IN STILE APP"):]
    assert ".question-card" in stile and ".consultant-card" in stile
    assert "box-shadow: none !important;" in stile
    # e qualcosa deve succedere quando si tocca
    assert ":active" in stile


def test_la_chat_occupa_tutto_lo_schermo():
    """Una conversazione non e' una pagina da scorrere: la casella di
    scrittura sta sopra le schede e i messaggi scorrono in mezzo."""
    css = _file("app", "static", "mobile.css")
    chat = css[css.index("CHAT A TUTTO SCHERMO"):]
    # dvh e non vh: con vh la casella finiva sotto la barra del browser
    assert "100dvh" in chat
    assert ".message-input-container" in chat and ".messages-area" in chat


def test_dentro_l_app_si_tiene_conto_della_tacca():
    css = _file("app", "static", "mobile.css")
    assert "display-mode: standalone" in css
    dentro = css[css.index("display-mode: standalone"):]
    assert "env(safe-area-inset-top)" in dentro


def test_il_video_della_home_non_si_scarica_da_solo_sul_telefono():
    """Sono 2,4 MB che partirebbero anche sotto rete mobile."""
    home = _file("app", "templates", "home.html")
    assert "video.preload = 'none'" in home
    assert "max-width: 768px" in home


def test_chi_e_gia_dentro_vede_le_sue_cose(client):
    """Per chi ha gia' un account la vetrina non serve: in cima al telefono ci
    vanno la prossima consulenza, le richieste da rispondere e la community."""
    home = _file("app", "templates", "home.html")
    assert 'id="miaHome"' in home
    assert "{% if current_user %}" in home
    assert "/api/booking/upcoming" in home, "le cose arrivano dalle API gia' esistenti"
    # e per chi arriva la prima volta la presentazione resta
    assert "ha-mia-home" in home
    assert client.get("/").status_code == 200


def test_la_vetrina_resta_a_chi_arriva_la_prima_volta():
    """La home promozionale sparisce solo sul telefono e solo dopo l'accesso:
    sul computer, e per chi non e' registrato, resta quella di prima."""
    home = _file("app", "templates", "home.html")
    blocco = home[home.index(".mia-home {"):home.index(".mia-saluto")]
    assert "display: none;" in blocco, "di suo il blocco non si vede"
    dentro = home[home.index("body.ha-mia-home"):]
    assert ".hero-main" in dentro[:200] and ".cta-section" in dentro[:200]


def test_la_home_porta_le_domande_recenti():
    rotta = _file("app", "routes", "home.py")
    assert "domande_recenti" in rotta
    assert "CommunityQuestion" in rotta
    # solo per chi ha fatto l'accesso: agli altri non servono
    pezzo = rotta[rotta.index("domande_recenti = []"):rotta.index("logger.info(f\"Home page loaded")]
    assert "if current_user" in pezzo
