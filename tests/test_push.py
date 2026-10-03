"""Notifiche push: quelle che arrivano sul telefono anche col sito chiuso.

Qui non si prova la consegna vera (dipende dai server di Google e Apple), ma
tutto quello che sta dalla nostra parte: chi puo' iscriversi, cosa succede
quando un dispositivo sparisce, e che una push rotta non faccia saltare la
notifica in-app.
"""
import io
import json
import os

import pytest
from sqlmodel import Session, select

from app.database import engine
from app.models import PushSubscription
from app.utils import notifiche_push

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


ISCRIZIONE = {
    "endpoint": "https://fcm.example/invio/abc123",
    "keys": {"p256dh": "chiave-pubblica-del-dispositivo", "auth": "segreto"},
}


@pytest.fixture
def chiavi(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pubblica-finta")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "privata-finta")


@pytest.fixture(autouse=True)
def pulisci():
    yield
    with Session(engine) as session:
        for riga in session.exec(select(PushSubscription)).all():
            session.delete(riga)
        session.commit()


def test_senza_chiavi_le_push_non_partono(monkeypatch):
    """Il sito deve funzionare uguale anche dove le chiavi non sono state messe."""
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    assert notifiche_push.configurato() is False
    assert notifiche_push.invia(1, "Ciao", "Testo") == 0


def test_lo_stesso_dispositivo_non_si_duplica():
    """L'indirizzo di consegna si riscrive a ogni riavvio del browser: se ogni
    volta aggiungessimo una riga, una notifica arriverebbe cinque volte."""
    assert notifiche_push.registra(7, ISCRIZIONE, "Android")
    assert notifiche_push.registra(7, ISCRIZIONE, "Android")
    assert notifiche_push.dispositivi(7) == 1


def test_un_iscrizione_incompleta_viene_rifiutata():
    assert notifiche_push.registra(7, {"endpoint": "https://fcm.example/x"}) is False
    assert notifiche_push.dispositivi(7) == 0


def test_il_dispositivo_di_un_altro_account_cambia_padrone():
    """Sullo stesso telefono entra un'altra persona: le notifiche devono
    seguire chi ha fatto l'accesso adesso, non restare al primo."""
    notifiche_push.registra(7, ISCRIZIONE)
    notifiche_push.registra(9, ISCRIZIONE)
    assert notifiche_push.dispositivi(7) == 0
    assert notifiche_push.dispositivi(9) == 1


def test_un_dispositivo_sparito_viene_dimenticato(chiavi, monkeypatch):
    """Quando il servizio risponde 410 quell'indirizzo non esiste piu':
    tenendolo, busseremmo a vuoto a ogni notifica per sempre."""
    class RispostaFinta:
        status_code = 410

    class ErroreFinto(Exception):
        response = RispostaFinta()

    import pywebpush

    def esplode(*args, **kwargs):
        raise pywebpush.WebPushException("scaduta", response=RispostaFinta())

    monkeypatch.setattr(pywebpush, "webpush", esplode)
    notifiche_push.registra(7, ISCRIZIONE)

    notifiche_push._consegna(1, ISCRIZIONE["endpoint"], "p", "a", "{}")
    assert notifiche_push.dispositivi(7) == 0


def test_il_carico_dice_titolo_testo_e_dove_andare(chiavi, monkeypatch):
    """Il service worker legge questi campi: se cambiano nome, sul telefono
    compare una notifica vuota."""
    inviati = []
    monkeypatch.setattr(notifiche_push.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: inviati.append(args)})())
    notifiche_push.registra(7, ISCRIZIONE)
    assert notifiche_push.invia(7, "Nuovo messaggio", "Marco ti ha scritto", "/messaggi") == 1

    carico = json.loads(inviati[0][4])
    assert carico["titolo"] == "Nuovo messaggio"
    assert carico["testo"] == "Marco ti ha scritto"
    assert carico["url"] == "/messaggi"
    sw = _file("app", "static", "sw.js")
    for campo in ("dati.titolo", "dati.testo", "dati.url"):
        assert campo in sw, f"il service worker non legge {campo}"


def test_il_service_worker_mostra_sempre_qualcosa():
    """Chrome esige che a ogni push corrisponda una notifica visibile: se non
    la mostriamo noi, ne mostra una sua del tipo 'sito aggiornato in background'."""
    sw = _file("app", "static", "sw.js")
    assert "showNotification" in sw
    assert "notificationclick" in sw


def test_le_notifiche_in_app_fanno_partire_anche_la_push():
    """L'aggancio sta nel servizio centrale: cosi' ogni notifica nuova arriva
    anche sul telefono senza doverselo ricordare caso per caso."""
    servizio = _file("app", "utils", "notification_service.py")
    assert "notifiche_push" in servizio
    dopo = servizio[servizio.index("notifiche_push"):]
    assert "except Exception" in dopo, "una push che non parte non deve far fallire la notifica"


def test_le_chiavi_non_sono_nel_repo():
    """La chiave privata firma le notifiche a nome nostro: sta solo fra le
    variabili d'ambiente, mai nel codice (il repository e' pubblico)."""
    for modello in (("app", "utils", "notifiche_push.py"), ("app", "routes", "push.py")):
        testo = _file(*modello)
        assert "VAPID_PRIVATE_KEY" not in testo.replace('os.getenv("VAPID_PRIVATE_KEY", "")', "")


def test_chi_non_ha_fatto_l_accesso_non_si_iscrive(client):
    """Un indirizzo di consegna e' legato a una persona: senza accesso non si
    puo' iscrivere un dispositivo alle notifiche di qualcun altro."""
    # 403 arriva prima, dal controllo anti-falsificazione delle richieste
    assert client.post("/api/push/iscrizione", json={"iscrizione": ISCRIZIONE}).status_code in (401, 403)
    assert client.get("/api/push/stato").status_code == 401


def _blocco_push():
    """Lo script dell'interruttore, fino all'invito a installare l'app."""
    base = _file("app", "templates", "base.html")
    inizio = base.index("Notifiche push.")
    return base[inizio:base.index("const RICORDA", inizio)]


def test_sul_telefono_il_controllo_sta_sopra_agli_appuntamenti():
    """In fondo alla scheda Account non lo trovava nessuno. Sul telefono sta
    sopra i prossimi appuntamenti; sul computer resta dov'era."""
    profilo = _file("app", "templates", "profile.html")
    alto = profilo.index('id="pushInAltoProfilo"')
    appuntamenti = profilo.index('id="upcomingAppointmentsSection"')
    assert alto < appuntamenti, "il controllo deve stare prima degli appuntamenti"
    fascia = profilo[alto:appuntamenti]
    assert "interruttore-push" in fascia
    assert "Attiva notifiche" in fascia

    account = profilo[profilo.index(">Account</h3>"):]
    assert 'id="interruttorePush"' in account[:account.index("voceInstallaProfilo")]
    assert "interruttore-push" in account[:account.index("voceInstallaProfilo")]


def test_la_home_del_telefono_ha_lo_stesso_controllo():
    """Le prossime cose restano la parte importante: il controllo sta sopra,
    non al posto loro. La home promozionale del computer non lo contiene."""
    home = _file("app", "templates", "home.html")
    blocco = home[home.index('id="miaHome"'):home.index('id="mieCose"')]
    assert 'id="pushInAltoHome"' in blocco
    assert "interruttore-push" in blocco
    assert blocco.index("pushInAltoHome") < blocco.index("mia-saluto")
    # mia-home di suo non si vede: si accende solo sotto i 768px
    nascosta = home[home.index(".mia-home {"):home.index(".mia-saluto")]
    assert "display: none;" in nascosta


def test_sul_computer_controllo_e_banner_restano_spenti():
    """Da 769px in su il profilo e la home restano come prima: niente striscia
    in alto e niente banner. Il doppione in Account si spegne solo sul telefono."""
    base = _file("app", "templates", "base.html")
    inizio = base.index("Sul computer il controllo in alto e il banner non esistono")
    blocco = base[base.rfind("@media", 0, inizio):base.index("}", base.index(".avviso-push", inizio)) + 1]
    assert "min-width: 769px" in blocco
    assert ".push-in-alto" in blocco and ".avviso-push" in blocco
    assert "display: none !important;" in blocco

    telefono = base[base.index("@media (max-width: 768px)"):base.index(".invito-app {")]
    assert "#interruttorePush" in telefono
    assert "display: none !important;" in telefono
    # la striscia in alto, spenta di suo, si accende in quel blocco
    assert ".push-in-alto" in telefono and "display: block;" in telefono


def test_il_banner_e_solo_per_chi_ha_fatto_l_accesso(client):
    """Senza accesso non c'e' niente da attivare, e la frase e' la stessa
    dell'interruttore."""
    base = _file("app", "templates", "base.html")
    intorno = base[base.index('id="avvisoPush"') - 250:base.index('id="avvisoPush"')]
    assert "{% if current_user %}" in intorno
    assert "Ti avvisiamo di messaggi e prenotazioni anche col sito chiuso." in base
    assert 'id="avvisoPush"' not in client.get("/").text
    assert 'id="avvisoPush"' not in client.get("/login").text


def test_il_banner_non_compare_se_non_si_puo_o_e_gia_attivo():
    """Niente chiave, gia' attive, chiuso di recente, chiamata in corso o
    schermo largo: il banner sta zitto. Su iPhone il controllo resta, spento."""
    script = _blocco_push()
    assert "if (!stato.attivabile || !stato.chiave_pubblica) { return; }" in script
    decisione = script[script.index("if (!stato.attivabile"):]
    gia_attive = decisione[decisione.index("Disattiva notifiche"):decisione.index("apriAvviso()")]
    assert "return" in gia_attive, "se sono gia' attive non si apre il banner"
    assert "rimandato()" in script
    assert "ispiramy-avviso-push" in script
    assert "GIORNI_SENZA_AVVISO = 3" in script
    assert "localStorage.setItem(MEMORIA_PUSH" in script
    assert "max-width: 768px" in script, "sul computer il banner non si apre"
    assert "/booking/call/" in script, "durante una chiamata non scende"
    # un solo flusso di iscrizione: il banner chiama attiva(), non un'altra API
    assert script.count("pushManager.subscribe") == 1
    assert "avvisoPushAttiva" in script
    assert "addEventListener('click', attiva)" in script

    prima = script[:script.index("if (!stato.attivabile")]
    iphone = prima[prima.index("Su iPhone servono dopo aver aggiunto Ispiramy alla schermata Home."):]
    assert "disabled = true" in iphone
    assert "return" in iphone
    assert "apriAvviso" not in iphone


def test_chi_e_dentro_vede_il_banner_e_il_controllo_in_pagina(client):
    """Il markup c'e' solo dopo l'accesso: profilo (sopra gli appuntamenti),
    home del telefono, e il banner su una pagina qualunque."""
    import secrets

    from sqlmodel import Session

    from app.database import engine
    from app.models import User
    from app.utils.password import hash_password
    from app.utils.rate_limit import reset_rate_limit

    email = f"push-{secrets.token_hex(4)}@test.local"
    with Session(engine) as s:
        utente = User(
            email=email,
            password_md5=hash_password("prova-password"),
            confirmed=1, nome="Valerio", cognome="Di Dio",
        )
        s.add(utente)
        s.commit()
        utente_id = utente.id

    try:
        reset_rate_limit()
        marker = 'name="csrf-token" content="'
        pagina = client.get("/login").text
        if marker in pagina:
            client.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
        assert client.post("/api/login", data={"email": email, "password": "prova-password"}).status_code == 200

        home = client.get("/").text
        assert 'id="avvisoPush"' in home
        assert 'id="pushInAltoHome"' in home
        assert home.index("pushInAltoHome") < home.index('id="mieCose"')

        profilo = client.get("/profile").text
        assert 'id="pushInAltoProfilo"' in profilo
        assert profilo.index("pushInAltoProfilo") < profilo.index("upcomingAppointmentsSection")
        assert 'id="interruttorePush"' in profilo

        # un'altra pagina, non solo profilo e home
        community = client.get("/community").text
        assert 'id="avvisoPush"' in community
        assert "pushInAlto" not in community
    finally:
        client.get("/logout")
        reset_rate_limit()
        with Session(engine) as s:
            riga = s.get(User, utente_id)
            if riga:
                s.delete(riga)
                s.commit()
