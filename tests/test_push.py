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
