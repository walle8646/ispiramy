"""La community deve stare dentro il monitor che ha davanti chi la guarda.

Le tre colonne avevano percentuali scritte in riga (20/63/17) dentro un
contenitore fermo a 1400px: su un monitor da 1920 restavano 500px di bianco ai
lati, su uno da 2560 piu' di mille, e a 1100px la colonna dei Top Ispiramyers
si riduceva a 184px.
"""
import io
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _community():
    percorso = os.path.join(RADICE, "app", "templates", "community.html")
    return io.open(percorso, encoding="utf-8").read()


def test_le_colonne_non_hanno_piu_misure_scritte_in_riga():
    """Una percentuale scritta nell'attributo style non si puo' cambiare da
    una media query senza !important: e' li' che nasceva il problema."""
    testo = _community()
    for misura in ('flex: 0 0 20%', 'flex: 0 0 63%', 'flex: 0 0 17%'):
        assert misura not in testo, f"{misura}: misura fissa nel marcatore"


def test_lo_spazio_che_avanza_va_alle_domande():
    """Le colonne laterali hanno un minimo e un massimo in pixel, la colonna
    centrale si prende il resto."""
    testo = _community()
    assert "grid-template-columns: clamp(200px, 18%, 280px) minmax(0, 1fr) clamp(200px, 19%, 320px)" in testo
    assert "@media (min-width: 1200px)" in testo
    assert ".contenitore-community" in testo
    assert "max-width: 1400px" not in testo, "il contenitore non si ferma piu' a 1400"


def test_sotto_i_1200_i_top_ispiramyers_passano_sotto():
    """Tre colonne sotto i 1200px vogliono dire una colonna da 200px scarsi:
    meglio due colonne e i Top Ispiramyers in fila sotto le domande."""
    testo = _community()
    assert "@media (min-width: 769px) and (max-width: 1199px) {" in testo
    assert "@media (min-width: 769px) and (max-width: 1024px) {" not in testo
