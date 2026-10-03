"""La pagina dei termini deve raccontare le regole che il codice applica davvero.

Le cifre (spese, ore, commissione) arrivano dalle costanti. Se domani cambiano
e la pagina resta ferma, il test si accorge che stiamo promettendo un'altra cosa.
"""
import io
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def test_la_pagina_risponde(client):
    r = client.get("/terms")
    assert r.status_code == 200
    assert "Termini e condizioni" in r.text
    assert "Bozza" in r.text
    assert 'href="/privacy"' in r.text
    assert "mailto:admin@ispiramy.com" in r.text


def test_le_cifre_sono_quelle_del_codice(client):
    from app.models import User
    from app.utils.booking_requests import ORE_PER_RISPONDERE
    from app.utils.notification_email import NOTA_RIMBORSO
    from app.utils.orari import ORE_LIMITE_ANNULLAMENTO, ORE_PREAVVISO_PRENOTAZIONE
    from app.utils.prezzi import PREZZO_ORARIO_MINIMO, SPESE_SERVIZIO

    testo = client.get("/terms").text
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

    # Finestra contestazione e rilascio del compenso: 48 ore, scritte nel codice.
    scheduler = _file("app", "scheduler.py")
    prenotazioni = _file("app", "routes", "booking.py")
    assert "timedelta(hours=48)" in scheduler
    assert "hours_since_end <= 48" in prenotazioni
    assert "entro 48 ore" in testo
    assert "48 ore dopo la fine" in testo


def test_le_cifre_non_sono_scritte_a_mano():
    pagina = _file("app", "templates", "terms.html")
    assert "{{ spese_servizio_euro }}" in pagina
    assert "{{ prezzo_orario_minimo }}" in pagina
    assert "{{ ore_preavviso }}" in pagina
    assert "{{ ore_limite_annullamento }}" in pagina
    assert "{{ ore_per_rispondere }}" in pagina
    assert "{{ commissione_percentuale }}" in pagina


def test_non_inventa_un_soggetto_giuridico(client):
    """Senza ragione sociale, partita IVA e sede, la pagina non se le inventa."""
    testo = client.get("/terms").text.lower()
    for assente in ("partita iva", "s.r.l", "s.p.a", "foro di", "tribunale di milano"):
        assert assente not in testo


def test_non_promette_cose_che_il_sito_non_fa(client):
    testo = client.get("/terms").text.lower()
    for assente in (
        "coupon",
        "valuta virtuale",
        "dmca",
        "abbonamento",
        "codice amico",
        "14 giorni",
    ):
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
