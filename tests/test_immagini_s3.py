"""Immagini degli utenti sullo stesso bucket delle registrazioni.

Le cartelle restano quelle già usate (profile-pictures, community-images,
call-attachments). I video stanno sotto booking_<id>/ e non si mescolano.
Se manca la config, il log dice i nomi delle variabili e non si scrive su disco.
"""
import secrets
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlmodel import Session, select

from app.database import engine
from app.models import Booking, CommunityQuestion, User
from app.routes import booking as booking_routes
from app.utils import archivio_s3
from app.utils.password import hash_password
from app.utils.rate_limit import reset_rate_limit

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
CARTELLA_LOCALE = Path("uploads/profile_pictures")


def _lingua(http, codice):
    from app.utils.lingue_ui import COOKIE
    http.cookies.delete(COOKIE, path="/")
    http.get(f"/lingua/{codice}", follow_redirects=False)


def _login(http, email):
    http.get("/logout")
    reset_rate_limit()
    marker = 'name="csrf-token" content="'
    pagina = http.get("/login").text
    if marker in pagina:
        http.headers.update({"X-CSRF-Token": pagina.split(marker, 1)[1].split('"', 1)[0]})
    assert http.post("/api/login", data={"email": email, "password": "segret8a"}).status_code == 200


def _utente(session):
    utente = User(
        email=f"img-{secrets.token_hex(4)}@test.local",
        password_md5=hash_password("segret8a"),
        confirmed=1,
        nome="Prova",
        cognome="Immagine",
    )
    session.add(utente)
    session.commit()
    session.refresh(utente)
    return utente


def _senza_s3(monkeypatch):
    for nome in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET_NAME"):
        monkeypatch.delenv(nome, raising=False)


def _con_s3(monkeypatch, bucket="bucket-registrazioni"):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "segreto-di-test")
    monkeypatch.setenv("AWS_S3_BUCKET_NAME", bucket)
    monkeypatch.setenv("AWS_S3_REGION", "eu-south-1")


class _S3:
    def __init__(self):
        self.messi = None
        self.firma = None

    def put_object(self, **kwargs):
        self.messi = kwargs

    def generate_presigned_url(self, op, Params, ExpiresIn):
        assert op == "get_object"
        self.firma = (Params, ExpiresIn)
        return f"https://firmato.example/{Params['Key']}?t={ExpiresIn}"


def test_senza_config_il_log_elenca_i_nomi_e_non_scrive_su_disco(csrf_client, monkeypatch):
    _senza_s3(monkeypatch)

    async def approvata(*args, **kwargs):
        return {"approved": True, "reason": ""}

    monkeypatch.setattr("app.routes.community.modera_immagine", approvata)
    monkeypatch.setattr("app.routes.user_profile.modera_immagine", approvata)
    prima = set(CARTELLA_LOCALE.iterdir()) if CARTELLA_LOCALE.exists() else set()
    raccolti = []
    sink = logger.add(lambda messaggio: raccolti.append(str(messaggio)), level="ERROR")
    with Session(engine) as s:
        utente = _utente(s)
        email = utente.email
    try:
        _login(csrf_client, email)
        community = csrf_client.post(
            "/api/community/upload-image",
            files={"file": ("foto.jpg", JPEG, "image/jpeg")},
        )
        profilo = csrf_client.post(
            "/api/upload-profile-picture",
            files={"file": ("foto.jpg", JPEG, "image/jpeg")},
        )
        assert community.status_code == 500
        assert profilo.status_code == 500
        testo = "\n".join(raccolti)
        for nome in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_BUCKET_NAME"):
            assert nome in testo
        assert "segreto-di-test" not in testo
        dopo = set(CARTELLA_LOCALE.iterdir()) if CARTELLA_LOCALE.exists() else set()
        assert dopo == prima
        with Session(engine) as s:
            salvato = s.exec(select(User).where(User.email == email)).one()
            assert not (salvato.profile_picture or "").startswith("/uploads/")
    finally:
        logger.remove(sink)
        csrf_client.get("/logout")
        reset_rate_limit()


def test_le_immagini_vanno_nelle_cartelle_gia_previste_non_in_quella_dei_video(csrf_client, monkeypatch):
    _con_s3(monkeypatch)
    s3 = _S3()

    async def approvata(*args, **kwargs):
        return {"approved": True, "reason": ""}

    monkeypatch.setattr("app.routes.community.modera_immagine", approvata)
    monkeypatch.setattr("app.routes.user_profile.modera_immagine", approvata)
    monkeypatch.setattr(archivio_s3, "client", lambda regione_override=None: s3)

    with Session(engine) as s:
        utente = _utente(s)
        email, utente_id = utente.email, utente.id
    try:
        _login(csrf_client, email)
        community = csrf_client.post(
            "/api/community/upload-image",
            files={"file": ("foto.jpg", JPEG, "image/jpeg")},
        )
        assert community.status_code == 200, community.text
        assert s3.messi["Bucket"] == "bucket-registrazioni"
        assert s3.messi["Key"].startswith(f"community-images/{utente_id}_")
        assert not s3.messi["Key"].startswith("booking_")
        assert community.json()["url"].startswith("https://firmato.example/community-images/")
        assert s3.firma[1] == 604800

        profilo = csrf_client.post(
            "/api/upload-profile-picture",
            files={"file": ("foto.jpg", JPEG, "image/jpeg")},
        )
        assert profilo.status_code == 200, profilo.text
        assert s3.messi["Key"].startswith(f"profile-pictures/{utente_id}_")
        assert not s3.messi["Key"].startswith("booking_")
        with Session(engine) as s:
            salvato = s.get(User, utente_id)
            assert "/profile-pictures/" in salvato.profile_picture
            assert "X-Amz-" not in salvato.profile_picture
            assert salvato.profile_picture.startswith("https://bucket-registrazioni.s3.")
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


def test_un_allegato_di_chat_usa_call_attachments_sul_bucket_delle_registrazioni(csrf_client, monkeypatch):
    _con_s3(monkeypatch)
    s3 = _S3()
    monkeypatch.setattr(booking_routes, "_get_chat_s3_client", lambda: s3)

    async def approvata(*args, **kwargs):
        return {"approved": True, "reason": ""}

    monkeypatch.setattr("app.utils.ai_service.modera_immagine_chat", approvata)
    with Session(engine) as s:
        cliente = _utente(s)
        consulente = _utente(s)
        prenotata = Booking(
            client_user_id=cliente.id,
            consultant_user_id=consulente.id,
            booking_date=datetime(2031, 8, 1, 10, 0),
            start_time="10:00",
            end_time="11:00",
            duration_minutes=60,
            status="confirmed",
            price=40,
            payment_status="pending",
        )
        s.add(prenotata)
        s.commit()
        s.refresh(prenotata)
        email, prenotata_id = cliente.email, prenotata.id
    try:
        _login(csrf_client, email)
        risposta = csrf_client.post(
            f"/api/booking/{prenotata_id}/chat/upload-attachment",
            files={"file": ("foto.jpg", JPEG, "image/jpeg")},
        )
        assert risposta.status_code == 200, risposta.text
        assert s3.messi["Bucket"] == "bucket-registrazioni"
        assert s3.messi["Key"].startswith(f"call-attachments/{prenotata_id}/")
        assert not s3.messi["Key"].startswith("booking_")
        assert "bucket-registrazioni.s3." in risposta.json()["url"]
    finally:
        csrf_client.get("/logout")
        reset_rate_limit()


def test_un_file_locale_diventa_segnaposto_e_un_video_non_si_traveste_da_immagine(monkeypatch):
    _con_s3(monkeypatch)
    s3 = _S3()
    monkeypatch.setattr(archivio_s3, "client", lambda regione_override=None: s3)

    assert archivio_s3.servi("/uploads/profile_pictures/vecchia.jpg") == ""
    vista = archivio_s3.servi(
        "/uploads/community/persa.jpg",
        segnaposto=archivio_s3.SEGNAPOSTO_IMMAGINE,
    )
    assert vista == "/static/immagine-mancante.svg"
    assert "uploads" not in vista

    community = archivio_s3.servi(
        "https://bucket-registrazioni.s3.eu-south-1.amazonaws.com/community-images/7_x.jpg"
    )
    assert community.startswith("https://firmato.example/community-images/")
    assert s3.firma[0]["Bucket"] == "bucket-registrazioni"
    assert s3.firma[1] == 604800

    video = "https://bucket-registrazioni.s3.eu-south-1.amazonaws.com/booking_52/call.mp4"
    assert archivio_s3.stabilizza(video, archivio_s3.PREFISSO_COMMUNITY) is None
    assert archivio_s3.servi(video) == video


def test_la_community_mostra_il_segnaposto_se_l_immagine_era_sul_disco(client):
    titolo = f"Immagine persa {secrets.token_hex(4)}"
    with Session(engine) as s:
        autore = _utente(s)
        domanda = CommunityQuestion(
            user_id=autore.id,
            title=titolo,
            description="Una domanda con una foto che stava solo sul disco del server.",
            validation=True,
            images='["/uploads/community/marzo.jpg"]',
        )
        s.add(domanda)
        s.commit()
        domanda_id = domanda.id
    try:
        pagina = client.get("/community", params={"search": titolo})
        assert pagina.status_code == 200
        assert titolo in pagina.text
        assert 'alt="Immagine allegata"' in pagina.text
        assert "/static/immagine-mancante.svg" in pagina.text
        assert "/uploads/community/marzo.jpg" not in pagina.text
    finally:
        with Session(engine) as s:
            riga = s.get(CommunityQuestion, domanda_id)
            if riga:
                s.delete(riga)
                s.commit()


def test_come_funziona_e_faq_spiegano_registrazione_e_analisi(client):
    _lingua(client, "it")
    for percorso in ("/come-funziona", "/faq"):
        testo = client.get(percorso).text
        assert "analisi automatica" in testo
        assert "verifica i profili" in testo
        assert "Senza registrazione" in testo
    assert "48 ore" in client.get("/faq").text

    _lingua(client, "en")
    for percorso in ("/come-funziona", "/faq"):
        testo = client.get(percorso).text
        assert "automatic analysis" in testo
        assert "verify profiles" in testo
        assert "Without a recording" in testo
        assert "Come funziona Ispiramy" not in testo
    _lingua(client, "it")
