"""La barra in alto sul computer.

Il campo di ricerca finiva sopra "Trova Consulenti": l'imbottitura (16px a
destra, 36 a sinistra per la lente) si sommava al 100% di larghezza, quindi il
campo era sempre 52px piu' largo del posto che aveva. E il posto glielo dava
il flex togliendolo alle voci, che non si stringono perche' il testo non va a
capo: da 1600px in giu' il campo entrava nelle voci.
"""
import io
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def test_il_campo_di_ricerca_sta_dentro_al_suo_spazio():
    base = _file("app", "templates", "base.html")
    pezzo = base[base.index(".search-input {"):base.index(".search-input {") + 300]
    assert "box-sizing: border-box;" in pezzo, "l'imbottitura torna a sommarsi alla larghezza"


def test_le_voci_del_menu_non_si_lasciano_schiacciare():
    """Il campo si stringe, le voci no: il contrario le fa sbordare."""
    base = _file("app", "templates", "base.html")
    ricerca = base[base.index(".search-wrapper {"):base.index(".search-wrapper {") + 200]
    assert "min-width: 0;" in ricerca
    voci = base[base.index("        .navbar-links {"):base.index("        .navbar-links {") + 120]
    assert "flex: 0 0 auto;" in voci


def test_sotto_i_1200_il_campo_lascia_il_posto_alla_lente():
    """Sotto quella misura resterebbe un campo in cui non si scrive niente:
    meglio la lente, che porta alla ricerca vera della pagina Esperti."""
    css = _file("app", "static", "mobile.css")
    assert "@media (min-width: 769px) and (max-width: 1200px) {" in css
    pezzo = css[css.index("@media (min-width: 769px) and (max-width: 1200px) {"):]
    pezzo = pezzo[:pezzo.index("\n}\n")]
    assert ".navbar .search-wrapper" in pezzo and "display: none !important;" in pezzo
    # e la lente, che sul computer grande resta nascosta, qui si vede
    assert "@media (min-width: 1201px) {" in css
