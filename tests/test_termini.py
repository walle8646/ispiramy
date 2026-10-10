"""La pagina dei termini deve raccontare le regole che il codice applica davvero.

Le cifre (spese, ore, commissione) arrivano dalle costanti. Se domani cambiano
e la pagina resta ferma, il test si accorge che stiamo promettendo un'altra cosa.
Il testo esiste in italiano e in inglese. Ragione sociale, sede e partita IVA
restano segnaposto, non dati inventati.
"""
import io
import os
import re

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _lingua(http, codice):
    from app.utils.lingue_ui import COOKIE
    http.cookies.delete(COOKIE, path="/")
    http.get(f"/lingua/{codice}", follow_redirects=False)


def test_la_pagina_risponde(client):
    _lingua(client, "it")
    r = client.get("/terms")
    assert r.status_code == 200
    assert "Termini e condizioni" in r.text
    assert "Bozza" in r.text
    assert 'href="/privacy"' in r.text
    assert "mailto:admin@ispiramy.com" in r.text
    assert "[RAGIONE SOCIALE]" in r.text
    assert "[SEDE LEGALE]" in r.text
    assert "[PARTITA IVA]" in r.text


def test_le_cifre_sono_quelle_del_codice(client):
    from app.models import User
    from app.utils.booking_requests import ORE_PER_RISPONDERE
    from app.utils.notification_email import NOTA_RIMBORSO
    from app.utils.orari import (
        ORE_FINESTRA_CONTESTAZIONE,
        ORE_LIMITE_ANNULLAMENTO,
        ORE_PREAVVISO_PRENOTAZIONE,
        ORE_RILASCIO_SENZA_RECENSIONE,
    )
    from app.utils.prezzi import PREZZO_ORARIO_MINIMO, SPESE_SERVIZIO

    _lingua(client, "it")
    testo = re.sub(r"\s+", " ", client.get("/terms").text)
    spese = f"{float(SPESE_SERVIZIO):.2f}".replace(".", ",")
    assert f"{spese} €" in testo
    assert f"almeno {PREZZO_ORARIO_MINIMO} €" in testo
    assert f"almeno {ORE_PREAVVISO_PRENOTAZIONE} ore" in testo
    assert f"fino a {ORE_LIMITE_ANNULLAMENTO} ore" in testo
    assert f"entro {ORE_PER_RISPONDERE} ore" in testo

    commissione = User.model_fields["platform_fee_percent"].default
    assert f"del {commissione}%" in testo

    assert "5-10" in NOTA_RIMBORSO
    assert "5-10 giorni lavorativi" in testo

    # Finestra contestazione e rilascio del compenso: le ore del codice.
    orari = _file("app", "utils", "orari.py")
    scheduler = _file("app", "scheduler.py")
    contestazioni = _file("app", "routes", "dispute.py")
    assert "return ore <= ORE_FINESTRA_CONTESTAZIONE" in orari
    assert "timedelta(hours=ORE_RILASCIO_SENZA_RECENSIONE)" in scheduler
    assert "Solo il cliente può aprire una contestazione" in contestazioni
    assert "not booking.recording_requested" in contestazioni
    assert f"entro {ORE_FINESTRA_CONTESTAZIONE} ore" in testo
    assert f"{ORE_RILASCIO_SENZA_RECENSIONE} ore dopo la fine" in testo
    assert "lascia la recensione" in testo
    assert "contestazione è aperta, il compenso resta fermo" in testo
    assert "solo il cliente" in testo
    assert "Senza registrazione la contestazione non si apre" in testo
    assert "Chi verifica i profili o amministra il sito" in testo
    assert "spese di servizio comprese" in testo
    assert "pulsante Segnala" in testo
    assert "non c'è un pulsante" not in testo.lower()


def test_in_inglese_le_stesse_regole(client):
    from app.models import User
    from app.utils.orari import ORE_FINESTRA_CONTESTAZIONE, ORE_RILASCIO_SENZA_RECENSIONE
    from app.utils.prezzi import COMMISSIONE_DEFAULT

    _lingua(client, "en")
    testo = re.sub(r"\s+", " ", client.get("/terms").text)
    assert 'lang="en"' in testo
    assert "Terms and conditions" in testo
    assert "[RAGIONE SOCIALE]" in testo
    assert "[SEDE LEGALE]" in testo
    assert "[PARTITA IVA]" in testo
    assert "when the client leaves a review" in testo
    assert f"{ORE_RILASCIO_SENZA_RECENSIONE} hours after the end" in testo
    assert "While a dispute is open, the payout stays on hold" in testo
    assert "only the client who booked" in testo
    assert "without a recording the dispute does not open" in testo
    assert f"within {ORE_FINESTRA_CONTESTAZIONE} hours" in testo
    assert "including the service fee" in testo
    assert "verify profiles or administer the site" in testo
    assert "Report button" in testo
    assert f"normally {User.model_fields['platform_fee_percent'].default}%" in testo
    assert COMMISSIONE_DEFAULT == User.model_fields["platform_fee_percent"].default
    assert "Il compenso del consulente parte quando il cliente lascia la recensione" not in testo
    assert "suspend" not in testo.lower()
    _lingua(client, "it")


def test_le_cifre_non_sono_scritte_a_mano():
    pagina = _file("app", "templates", "terms.html")
    assert "{{ spese_servizio_euro }}" in pagina
    assert "{{ prezzo_orario_minimo }}" in pagina
    assert "{{ ore_preavviso }}" in pagina
    assert "{{ ore_limite_annullamento }}" in pagina
    assert "{{ ore_per_rispondere }}" in pagina
    assert "{{ commissione_percentuale }}" in pagina
    assert "{{ ore_rilascio }}" in pagina
    assert "{{ ore_contestazione }}" in pagina


def test_non_inventa_un_soggetto_giuridico(client):
    """I segnaposto restano visibili. La pagina non si inventa una società."""
    _lingua(client, "it")
    testo = client.get("/terms").text.lower()
    for presente in ("[ragione sociale]", "[sede legale]", "[partita iva]"):
        assert presente in testo
    for assente in ("s.r.l", "s.p.a", "foro di", "tribunale di milano", "via roma"):
        assert assente not in testo


def test_non_promette_cose_che_il_sito_non_fa(client):
    _lingua(client, "it")
    testo = client.get("/terms").text.lower()
    for assente in (
        "coupon",
        "valuta virtuale",
        "dmca",
        "abbonamento",
        "codice amico",
        "14 giorni",
        "può sospendere",
    ):
        assert assente not in testo


def test_ci_si_arriva_dal_piede_e_dalla_registrazione():
    base = _file("app", "templates", "base.html")
    assert 'href="/terms"' in base
    registrazione = _file("app", "templates", "register.html")
    assert 'href="/terms"' in registrazione
    assert 'href="/privacy"' in registrazione
    assert "Termini e condizioni" not in registrazione
    assert "t('registrazione.termini_link')" in registrazione


def test_sul_telefono_il_testo_va_a_capo():
    """Senza overflow-wrap un titolo lungo esce dallo schermo da 390px."""
    pagina = _file("app", "templates", "terms.html")
    assert "@media (max-width: 768px)" in pagina
    assert "overflow-wrap: break-word" in pagina
