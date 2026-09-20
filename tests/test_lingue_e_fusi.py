"""Due cose che servono appena qualcuno arriva da fuori Italia.

Chi cerca aiuto in inglese ha bisogno di un consulente che l'inglese lo parli,
e di sapere in che fuso è l'orario che sta prenotando. Sono i due passi che
precedono una vera traduzione del sito: senza, la versione inglese
prometterebbe quello che non può mantenere.
"""
import io
import os

from app.models import User
from app.routes.consultants import (
    CODICI_LINGUA,
    LINGUE,
    lingue_parlate,
    lingue_per_la_ricerca,
)
from app.utils.orari import con_fuso

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _utente(**campi) -> User:
    base = dict(email="x@y.z", password_md5="x", nome="Prova")
    base.update(campi)
    return User(**base)


def test_le_lingue_si_leggono_comunque_siano_salvate():
    """Il profilo salva {"codes": [...]}, ma una lista secca deve funzionare
    lo stesso: un profilo vecchio non deve sparire dai filtri."""
    assert lingue_parlate(_utente(languages='{"codes": ["it", "en"], "other": ""}')) == ["it", "en"]
    assert lingue_parlate(_utente(languages='["it", "fr"]')) == ["it", "fr"]
    assert lingue_parlate(_utente(languages=None)) == []
    assert lingue_parlate(_utente(languages="non e' json")) == []


def test_il_filtro_lingua_esiste_ed_e_prudente(client):
    rotta = _file("app", "routes", "consultants.py")
    assert "lingua in CODICI_LINGUA" in rotta, "solo i codici che conosciamo"
    assert client.get("/consultants?lingua=en").status_code == 200
    # un valore inventato non deve rompere la pagina
    assert client.get("/consultants?lingua=klingon").status_code == 200


def test_le_lingue_si_vedono_sulla_scheda():
    """Chi non parla italiano deve capirlo prima di aprire il profilo."""
    pagina = _file("app", "templates", "consultants.html")
    assert "consultant-lingue" in pagina
    assert "lingue_disponibili" in pagina, "manca il filtro nella colonna dei filtri"
    assert {c for c, _, _ in LINGUE} == CODICI_LINGUA


def test_i_collegamenti_dei_filtri_non_perdono_la_ricerca():
    """Prima ogni filtro ricostruiva l'indirizzo a mano con una catena di
    condizioni: aggiungerne uno significava riscriverle tutte."""
    principale = _file("app", "main.py")
    assert "def con_parametro" in principale
    assert 'parametri.pop("page", None)' in principale, "cambiando filtro si riparte da pagina 1"


def test_ogni_orario_scritto_dice_di_che_fuso_e():
    assert con_fuso("15:00") == "15:00 (ora italiana)"
    assert con_fuso("") == ""


def test_i_messaggi_con_un_orario_lo_dicono():
    """Email e notifiche arrivano a chi puo' stare ovunque."""
    for modulo, quante in (
        ("app/routes/booking.py", 1),
        ("app/routes/stripe_webhook.py", 2),
        ("app/scheduler.py", 1),
        ("app/utils/booking_requests.py", 1),
    ):
        testo = _file(*modulo.split("/"))
        assert testo.count("con_fuso(") >= quante, f"{modulo}: orari senza fuso"


def test_a_schermo_si_vede_anche_l_ora_di_chi_guarda():
    """Il fuso si ricalcola per ogni data: l'ora legale non cambia lo stesso
    giorno in Italia e negli Stati Uniti, e per due settimane la differenza
    non e' quella solita."""
    base = _file("app", "templates", "base.html")
    assert "etichettaFuso" in base and "oraNelTuoFuso" in base
    blocco = base[base.index("const FUSO_SITO"):base.index("<!-- JavaScript Globale -->")]
    assert "function scarto(data)" in blocco, "lo scarto va calcolato sulla data, non fisso"
    assert "fusoAttuale()" in blocco, "il fuso si rilegge, non si congela al caricamento"

    prenota = _file("app", "templates", "booking.html")
    assert prenota.count("etichettaFuso") >= 2, "orario scelto e riepilogo"


def test_chi_non_dichiara_niente_si_da_per_italiano():
    """Quasi nessuno ha spuntato le lingue: senza questa ipotesi il filtro
    "Italiano" mostrerebbe tre profili su cinquanta."""
    assert lingue_per_la_ricerca(_utente(languages=None)) == ["it"]
    assert lingue_per_la_ricerca(_utente(languages='{"codes": [], "other": ""}')) == ["it"]
    # chi ha dichiarato vale quello che ha detto: niente italiano d'ufficio
    assert lingue_per_la_ricerca(_utente(languages='{"codes": ["en"]}')) == ["en"]


def test_l_ipotesi_non_finisce_sulla_scheda():
    """Sulla scheda si scrivono solo le lingue dichiarate: dare per scontato
    l'italiano aiuta a farsi trovare, ma non e' una cosa che ha detto lui."""
    rotta = _file("app", "routes", "consultants.py")
    pezzo = rotta[rotta.index("'lingue': ["):rotta.index("'category': None")]
    assert "lingue_parlate(user)" in pezzo
    assert "lingue_per_la_ricerca" not in pezzo


def test_il_consulente_deve_dire_in_che_lingua_lavora():
    """Senza lingua dichiarata il profilo non compare in nessun filtro: il
    controllo sta nel modulo e anche nella rotta, perche' il primo si aggira."""
    modulo = _file("app", "templates", "profile.html")
    assert "Lingue Parlate *" in modulo
    assert "Scegli almeno una lingua" in modulo, "manca il blocco prima di inviare"
    # a chi non ha mai scelto, l'italiano arriva gia' spuntato
    assert "{% if 'it' in user_languages or not user_languages %}checked{% endif %}" in modulo

    rotta = _file("app", "routes", "user_profile.py")
    assert "Scegli almeno una lingua" in rotta, "il controllo deve esserci anche sul server"
    pezzo = rotta[rotta.index("Le lingue sono obbligatorie"):]
    assert "user_type_id >= 2" in pezzo[:400], "vale per chi offre consulenze"
