"""La pagina dei Termini e condizioni.

Il testo e' un documento legale fornito da chi gestisce Ispiramy: sta nel
catalogo delle traduzioni, come il resto del sito. Le cifre pero' non si
scrivono a mano - restano segnaposto che la pagina sostituisce con i valori
presi dal codice, cosi' il giorno che cambia una regola la pagina non resta a
raccontare il numero vecchio.
"""
import io
import json
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SEGNAPOSTO = ("{min}", "{spese}", "{pct}", "{preavviso}", "{annullamento}",
              "{rilascio}", "{contestazione}", "{risposta}")


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _catalogo(lingua):
    percorso = os.path.join(RADICE, "app", "traduzioni", f"{lingua}.json")
    return json.loads(io.open(percorso, encoding="utf-8").read())


def test_ci_sono_tutti_i_sedici_articoli(client):
    pagina = client.get("/terms").text
    attesi = [
        "1. Premessa e descrizione del servizio",
        "2. Natura del servizio e ruolo di Ispiramy",
        "3. Requisiti di accesso e registrazione",
        "5. Prenotazione e svolgimento delle consulenze",
        "6. Prezzi, commissioni e pagamenti",
        "7.1 Annullamento da parte del cliente o del consulente",
        "7.3 Mancata partecipazione alla chiamata",
        "8.4 Presentazione della contestazione",
        "8.6 Effetti sui pagamenti e commissioni",
        "9. Pagamento dei consulenti",
        "11.1 Segnalazioni",
        "16. Legge applicabile, controversie e contatti",
    ]
    for titolo in attesi:
        assert titolo in pagina, f"manca l'articolo: {titolo}"


def test_nessuna_chiave_resta_in_vista(client):
    """Una chiave che non esiste si stampa com'e': su una pagina legale si
    noterebbe, ma meglio accorgersene qui."""
    pagina = client.get("/terms").text
    for chiave in ("termini.a", "termini.titolo", "legale.bozza", "legale.solo_italiano"):
        assert chiave not in pagina, f"chiave non risolta: {chiave}"


def test_le_cifre_arrivano_dal_codice(client):
    from app.utils.booking_requests import ORE_PER_RISPONDERE
    from app.utils.orari import (ORE_FINESTRA_CONTESTAZIONE, ORE_LIMITE_ANNULLAMENTO,
                                 ORE_PREAVVISO_PRENOTAZIONE, ORE_RILASCIO_SENZA_RECENSIONE)
    from app.utils.prezzi import PREZZO_ORARIO_MINIMO, SPESE_SERVIZIO

    pagina = client.get("/terms").text
    spese = f"{float(SPESE_SERVIZIO):.2f}".replace(".", ",")

    assert f"{PREZZO_ORARIO_MINIMO} € per ora" in pagina
    assert f"€ {spese}" in pagina
    assert f"{ORE_PREAVVISO_PRENOTAZIONE} ore di anticipo" in pagina
    assert f"fino a {ORE_LIMITE_ANNULLAMENTO} ore prima" in pagina
    assert f"trascorse {ORE_RILASCIO_SENZA_RECENSIONE} ore" in pagina
    assert f"entro {ORE_FINESTRA_CONTESTAZIONE} ore dalla conclusione" in pagina
    assert f"pari a {ORE_PER_RISPONDERE} ore" in pagina

    for segno in SEGNAPOSTO:
        assert segno not in pagina, f"segnaposto non sostituito: {segno}"


def test_i_segnaposto_del_catalogo_sono_quelli_che_la_pagina_sa_sostituire():
    """Un segnaposto scritto male resterebbe stampato nel testo: qui si
    controlla che nel catalogo non ne compaiano di sconosciuti."""
    import re

    catalogo = _catalogo("it")
    trovati = set()
    for chiave, testo in catalogo.items():
        if chiave.startswith("termini."):
            trovati.update(re.findall(r"\{[a-z_]+\}", testo))
    assert trovati <= set(SEGNAPOSTO), f"segnaposto sconosciuti: {sorted(trovati - set(SEGNAPOSTO))}"


def test_la_pagina_esiste_anche_in_inglese(client):
    """Le altre pagine pubbliche sono tradotte: questa non puo' fare
    eccezione, altrimenti chi legge il sito in inglese trova un muro."""
    inglese = client.get("/terms", cookies={"ispiramy_lingua": "en"}).text
    assert "1. Introduction and description of the service" in inglese
    assert "16. Governing law, disputes and contacts" in inglese
    assert "1. Premessa e descrizione del servizio" not in inglese
    # la versione italiana resta quella che vincola, e la pagina lo dice
    assert "binding one" in inglese
    italiane = {c for c in _catalogo("it") if c.startswith("termini.")}
    inglesi = {c for c in _catalogo("en") if c.startswith("termini.")}
    assert italiane == inglesi


def test_i_dati_del_gestore_restano_da_inserire(client):
    """Finche' mancano ragione sociale, sede e partita IVA la pagina lo dice
    in cima, invece di far finta di essere completa."""
    pagina = client.get("/terms").text
    for mancante in ("[RAGIONE SOCIALE]", "[SEDE LEGALE]", "[PARTITA IVA]",
                     "[FORMA GIURIDICA]", "[URL UFFICIALE]"):
        assert mancante in pagina


# ---------------------------------------------------------------------------
# Quello che segue stava gia' in questo file prima che il testo cambiasse:
# sono controlli che non dipendono da come e' scritto il documento, e valgono
# per qualunque testo ci finisca dentro.
# ---------------------------------------------------------------------------


def test_la_pagina_risponde(client):
    pagina = client.get("/terms")
    assert pagina.status_code == 200
    assert "Termini e condizioni" in pagina.text
    assert "Bozza" in pagina.text
    assert 'href="/privacy"' in pagina.text
    assert "mailto:admin@ispiramy.com" in pagina.text


def test_non_inventa_un_soggetto_giuridico(client):
    """I segnaposto restano visibili. La pagina non si inventa una societa'."""
    testo = client.get("/terms").text.lower()
    for presente in ("[ragione sociale]", "[sede legale]", "[partita iva]"):
        assert presente in testo
    for assente in ("s.r.l", "s.p.a", "foro di", "tribunale di milano", "via roma"):
        assert assente not in testo


def test_non_promette_cose_che_il_sito_non_fa(client):
    testo = client.get("/terms").text.lower()
    for assente in ("coupon", "valuta virtuale", "dmca", "abbonamento",
                    "codice amico", "14 giorni"):
        assert assente not in testo


def test_ci_si_arriva_dal_piede_e_dalla_registrazione():
    base = _file("app", "templates", "base.html")
    assert 'href="/terms"' in base
    registrazione = _file("app", "templates", "register.html")
    assert 'href="/terms"' in registrazione
    assert 'href="/privacy"' in registrazione


def test_sul_telefono_il_testo_va_a_capo():
    """Senza overflow-wrap un titolo lungo esce dallo schermo da 390px."""
    pagina = _file("app", "templates", "terms.html")
    assert "@media (max-width: 768px)" in pagina
    assert "overflow-wrap: break-word" in pagina
