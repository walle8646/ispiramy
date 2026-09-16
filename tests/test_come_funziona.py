"""La pagina che spiega il sito a chi ci arriva senza sapere cos'è.

Il rischio qui non è che si rompa: è che racconti cose non più vere. Le cifre
vengono dalle costanti del codice, e questi test verificano che sia così.
"""
import io
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def test_la_pagina_risponde(client):
    r = client.get("/come-funziona")
    assert r.status_code == 200
    assert "Come funziona Ispiramy" in r.text


def test_le_cifre_sono_quelle_vere(client):
    """Se domani le spese passano a 2,49 la pagina deve dirlo da sola: un
    numero scritto a mano qui diventerebbe una promessa sbagliata."""
    from app.utils.prezzi import PREZZO_ORARIO_MINIMO, SPESE_SERVIZIO

    testo = client.get("/come-funziona").text
    spese = f"{float(SPESE_SERVIZIO):.2f}".replace(".", ",")
    assert f"{spese} €" in testo
    assert f"da {PREZZO_ORARIO_MINIMO} €" in testo
    # il totale dell'esempio è una somma, non un numero copiato
    totale = f"{50 + float(SPESE_SERVIZIO):.2f}".replace(".", ",")
    assert f"{totale} €" in testo


def test_le_cifre_non_sono_scritte_a_mano():
    pagina = _file("app", "templates", "come_funziona.html")
    assert "{{ spese_servizio_euro }}" in pagina
    assert "{{ prezzo_orario_minimo }}" in pagina
    assert "{{ ore_preavviso }}" in pagina


def test_ci_si_arriva_dal_video_e_dal_menu():
    """I due punti che ha chiesto chi l'ha commissionata: sotto al video in
    homepage e nel menu in alto. Il terzo, il footer, è dove si guarda quando
    si è già scorsa tutta la pagina."""
    home = _file("app", "templates", "home.html")
    assert 'href="/come-funziona"' in home
    # deve stare sotto al video, non in fondo alla pagina
    inizio = home.index('<div class="hero-video-container"')
    pezzo = home[inizio:home.index("</section>", inizio)]
    assert "/come-funziona" in pezzo

    base = _file("app", "templates", "base.html")
    menu = base[base.index('<div class="navbar-links">'):base.index("{% if current_user %}")]
    assert "/come-funziona" in menu, "manca la voce nel menu in alto"
    assert base.count('href="/come-funziona"') >= 2, "manca il collegamento nel footer"


def test_i_disegni_non_scaricano_immagini():
    """Sono SVG scritti a mano: niente da scaricare, nitidi su ogni schermo."""
    pagina = _file("app", "templates", "come_funziona.html")
    assert pagina.count("<svg") == 8, "un passo senza disegno"
    assert "<img" not in pagina
    for disegno in pagina.split("<svg")[1:]:
        intestazione = disegno[:disegno.index(">")]
        assert "aria-label" in intestazione, "ogni disegno va descritto a chi non lo vede"


def test_il_video_e_il_collegamento_stanno_in_colonna():
    """In riga finivano fianco a fianco e sul telefono il video si riduceva a
    un francobollo da 180px."""
    home = _file("app", "templates", "home.html")
    blocco = home[home.index(".hero-right {"):]
    blocco = blocco[:blocco.index("}")]
    assert "flex-direction: column" in blocco
