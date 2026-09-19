"""La ricerca dei consulenti deve trovare chi sa fare una cosa.

Il caso da cui nasce questo file: cercando "viaggi all'estero" usciva un
consulente commerciale, perché nella bio aveva scritto "amante dei viaggi".
La descrizione non era (e non è) fra i campi cercati: quelle parole erano
finite nei tag che l'AI deduce dal profilo, e i tag pesavano quanto le
competenze dichiarate.
"""
import io
import os

from app.models import User
# testo_competenze si importa con un altro nome: pytest raccoglie come test
# qualsiasi funzione che cominci per 'test'
from app.routes.consultants import (
    calculate_relevance_score,
    clean_search_query,
    testo_competenze as competenze_dichiarate,
)

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _consulente(**campi) -> User:
    base = dict(email="x@y.z", password_md5="x", nome="Prova", cognome="Prova",
                consulenze_vendute=0)
    base.update(campi)
    return User(**base)


def test_la_descrizione_non_entra_nella_ricerca():
    """Chi *nomina* un argomento non è chi lo sa trattare."""
    commerciale = _consulente(
        descrizione="Sono un commerciale amante dei viaggi, juventino e noliano doc.",
        aree_interesse="Vendite, Negoziazione",
        professione="Sales manager",
    )
    testo = competenze_dichiarate(commerciale, "Lavoro & Carriera")
    for parola in ("viaggi", "juventino", "noliano", "commerciale"):
        assert parola not in testo or parola in "vendite negoziazione lavoro carriera", \
            f"'{parola}' non deve arrivare dalla descrizione"


def test_chi_lo_ha_scritto_fra_le_competenze_viene_prima():
    """Le aree le scrive il consulente, i tag li deduce l'AI: a parità di
    parola trovata, vale di più quello che ha dichiarato lui."""
    parole = clean_search_query("viaggi all'estero")

    esperto = _consulente(aree_interesse="Trasferimenti all'estero, Viaggi di lavoro")
    per_caso = _consulente(tags='["viaggi", "juventino", "vendite"]')

    punti_esperto = calculate_relevance_score(esperto, parole, parole, "Vita all'Estero")
    punti_per_caso = calculate_relevance_score(per_caso, parole, parole, "Lavoro & Carriera")

    assert punti_esperto > punti_per_caso, "chi se ne occupa davvero deve stare sopra"


def test_un_tag_dedotto_vale_meno_della_categoria():
    parole = ["estero"]
    per_categoria = _consulente(tags='["vendite"]')
    per_tag = _consulente(tags='["estero"]')

    assert calculate_relevance_score(per_categoria, parole, parole, "Vita all'Estero") > \
        calculate_relevance_score(per_tag, parole, parole, "Lavoro & Carriera")


def test_il_prompt_dei_tag_esclude_la_vita_privata():
    """I tag li legge la ricerca: hobby e squadra del cuore non c'entrano."""
    prompt = _file("app", "utils", "ai_service.py")
    pezzo = prompt[prompt.index("async def genera_tags"):prompt.index("def ai_configurata")]
    for regola in ("hobby", "squadra del cuore", "citta'"):
        assert regola in pezzo, f"il prompt non esclude: {regola}"
    assert "viaggi" in pezzo, "manca l'esempio concreto che ha fatto nascere la regola"


def test_c_e_come_rifare_i_tag_gia_salvati():
    """Cambiare il prompt non tocca i profili gia' esistenti."""
    script = _file("scripts", "rigenera_tag_consulenti.py")
    assert "genera_tags" in script
    assert "--prova" in script, "serve poter guardare prima di scrivere"
