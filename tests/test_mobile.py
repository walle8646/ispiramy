"""Regole che tengono il sito usabile su telefono.

Verificate sull'app vera a 375x812 (iPhone) con un'ispezione del DOM: qui
restano come guardie, perché sono correzioni che si perdono facilmente al
primo ritocco di stile.
"""
import io
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def test_il_calendario_della_prenotazione_sta_nello_schermo():
    """Con grid-template-columns: 1fr la colonna non scende sotto il contenuto
    più largo: il calendario usciva di 21px e la domenica restava tagliata."""
    html = _file("app", "templates", "booking.html")
    assert "grid-template-columns: minmax(0, 1fr);" in html
    assert ".calendar-day {" in html and "min-width: 0;" in html


def test_le_aree_toccabili_hanno_una_misura_minima():
    """I link del footer erano alti 20px, la ricerca 34: sotto i 40 si sbaglia bersaglio."""
    css = _file("app", "static", "mobile.css")
    assert "AREE TOCCABILI" in css
    for regola in (".footer-section a", ".search-input", ".history-toggle", ".category-btn"):
        assert regola in css, f"manca la misura minima per {regola}"
    assert "min-height: 44px" in css


def _selettori_di_primo_livello(css: str):
    """I selettori dei blocchi non annidati, commenti esclusi."""
    import re

    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    profondita = 0
    inizio = 0
    selettore = ""
    for i, carattere in enumerate(css):
        if carattere == "{":
            if profondita == 0:
                selettore = css[inizio:i].strip()
            profondita += 1
        elif carattere == "}":
            profondita -= 1
            if profondita == 0:
                yield selettore
                inizio = i + 1


def test_le_correzioni_mobile_non_toccano_il_desktop():
    """Ogni regola sta dentro una media query: sopra i 768px il sito resta com'era."""
    fuori = [s for s in _selettori_di_primo_livello(_file("app", "static", "mobile.css"))
             if not s.startswith("@media")]
    assert not fuori, ("regole fuori da una media query: cambierebbero anche il desktop\n"
                       + "\n".join(fuori[:5]))


def test_i_campi_non_fanno_ingrandire_la_pagina_su_iphone():
    """Sotto i 16px Safari ingrandisce la pagina al primo tocco su un campo."""
    css = _file("app", "static", "mobile.css")
    assert "font-size: 16px !important; /* Previene zoom iOS */" in css


def test_il_tablet_ha_le_sue_regole():
    """A 820px (iPad in verticale) il menu sforava di 3px e le schede a due
    colonne tagliavano i pulsanti: da 769 a 1024 il sito ha un assetto suo."""
    css = _file("app", "static", "mobile.css")
    assert "@media (min-width: 769px) and (max-width: 1024px)" in css
    for regola in (".consultants-grid", ".search-wrapper", ".navbar-links"):
        assert regola in css, f"manca la regola tablet per {regola}"


def test_sul_tablet_la_barra_non_copre_il_titolo():
    """La barra va a capo e diventa alta 117-132px, ma il corpo lascia spazio
    per 80: senza questa regola il titolo della pagina finiva sotto al menu."""
    css = _file("app", "static", "mobile.css")
    coda = css[css.index("BARRA SUL TABLET"):]
    assert "position: relative !important;" in coda
    assert "padding-top: 0 !important;" in coda


def test_la_community_sta_nello_schermo():
    """Le tre colonne sommano al 100% ma l'imbottitura si aggiungeva alla
    larghezza: la pagina scorreva di lato di 45px, anche sul desktop."""
    html = _file("app", "templates", "community.html")
    assert '.row > [class^="col-md-"]' in html and "box-sizing: border-box;" in html
    assert "@media (min-width: 769px) and (max-width: 1024px)" in html


def test_i_suggerimenti_al_passaggio_del_mouse_spariscono_al_tocco():
    """Il fumetto restava invisibile ma largo 240px e faceva scorrere la
    pagina di lato; col dito non si vede comunque."""
    html = _file("app", "templates", "community.html")
    dopo = html[html.index("@media (max-width: 1024px)"):]
    assert "[data-tip]::after" in dopo and "display: none !important;" in dopo
