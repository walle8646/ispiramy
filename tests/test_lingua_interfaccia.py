"""La lingua dell'interfaccia: la scelta, la memoria, le parole.

Il sito è nato in italiano e la maggior parte delle pagine lo è ancora: qui
si prova l'ossatura che permette di tradurle una per volta senza che, nel
frattempo, una pagina resti con un buco al posto di un pulsante.
"""
import io
import json
import os

from app.utils.lingue_ui import (
    CODICI_UI,
    COOKIE,
    PREDEFINITA,
    lingua_del_browser,
    lingua_valida,
    traduci,
)

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _catalogo(lingua):
    return json.loads(_file("app", "traduzioni", f"{lingua}.json"))


def test_le_lingue_hanno_le_stesse_parole():
    """Una chiave tradotta solo a metà lascia una scritta italiana in mezzo
    all'inglese, ed è il genere di cosa che nessuno segnala."""
    italiano, inglese = _catalogo("it"), _catalogo("en")
    assert set(italiano) == set(inglese), (
        f"solo in italiano: {sorted(set(italiano) - set(inglese))}\n"
        f"solo in inglese: {sorted(set(inglese) - set(italiano))}"
    )
    assert not [c for c, parola in inglese.items() if not parola.strip()]


def test_una_parola_che_manca_non_lascia_un_buco():
    """Meglio una scritta strana, che si nota e si corregge, di un pulsante
    senza testo."""
    assert traduci("nav.community", "en") == "Community"
    assert traduci("chiave.inventata", "en") == "chiave.inventata"


def test_la_lingua_del_browser_si_capisce_dall_intestazione():
    assert lingua_del_browser("en-GB,en;q=0.9,it;q=0.8") == "en"
    assert lingua_del_browser("it-IT,it;q=0.9") == "it"
    # una lingua che non parliamo non deve diventare la lingua del sito
    assert lingua_del_browser("ja-JP,ja;q=0.9") is None
    assert lingua_del_browser("") is None


def test_solo_le_lingue_che_esistono():
    assert lingua_valida("en") == "en"
    assert lingua_valida("klingon") is None
    assert PREDEFINITA in CODICI_UI


def test_la_scelta_resta(client):
    """Chi sceglie l'inglese non deve rifarlo a ogni pagina."""
    risposta = client.get("/lingua/en", follow_redirects=False)
    assert risposta.status_code == 303
    assert COOKIE in risposta.cookies or "ispiramy_lingua=en" in risposta.headers.get("set-cookie", "")

    pagina = client.get("/consultants")
    assert 'lang="en"' in pagina.text
    client.get("/lingua/it")  # si rimette com'era per gli altri test


def test_il_ritorno_resta_dentro_casa(client):
    """Il referer arriva da fuori: senza controllo, un indirizzo confezionato
    ad arte userebbe il nostro sito per mandare la gente altrove."""
    risposta = client.get("/lingua/en", follow_redirects=False,
                          headers={"referer": "https://sito-cattivo.example/pagina"})
    assert risposta.headers["location"] == "/"
    client.get("/lingua/it")


def test_la_lingua_si_sceglie_dall_intestazione():
    base = _file("app", "templates", "base.html")
    assert '<html lang="{{ lingua_ui }}">' in base
    assert "scelta-lingua" in base, "manca il selettore in alto"
    assert "foglio-lingua" in base, "manca la scelta nel menu del telefono"
    assert base.count("/lingua/{{ codice }}") >= 2


def test_la_ricerca_segue_la_lingua_scelta():
    """Chi ha messo il sito in inglese sta cercando in inglese: il filtro dei
    consulenti parte da li'."""
    rotta = _file("app", "routes", "consultants.py")
    pezzo = rotta[rotta.index("FILTRO PER LINGUA"):]
    assert "lingua_di(request)" in pezzo[:800]
    assert 'scelta_interfaccia != "it"' in pezzo[:800], "in italiano non si filtra niente"


def test_le_pagine_dell_app_hanno_le_parole_tradotte():
    """Ossatura, esperti, community, messaggi e home: sono le pagine che si
    vedono usando l'app tutti i giorni."""
    attese = {
        "app/templates/base.html": 25,
        "app/templates/consultants.html": 25,
        "app/templates/community.html": 12,
        "app/templates/messages_inbox.html": 4,
        "app/templates/home.html": 15,
    }
    for percorso, minimo in attese.items():
        testo = _file(*percorso.split("/"))
        quante = testo.count("{{ t(")
        assert quante >= minimo, f"{percorso}: solo {quante} scritte tradotte"


def test_una_lingua_senza_consulenti_non_svuota_la_pagina():
    """Il filtro per lingua lo mettiamo noi quando il sito e' in inglese: se
    nessuno parla quella lingua, una pagina vuota sembra un sito rotto."""
    rotta = _file("app", "routes", "consultants.py")
    pezzo = rotta[rotta.index("FILTRO PER LINGUA"):rotta.index("SCORING E ORDINAMENTO")]
    assert "filtro_automatico" in pezzo
    assert "nessuno_in_quella_lingua = True" in pezzo
    # e lo si dice a chi guarda, invece di far finta di niente
    assert "esperti.nessuno_in_lingua" in _file("app", "templates", "consultants.html")
