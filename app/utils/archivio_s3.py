"""Immagini e allegati degli utenti, sullo stesso bucket delle registrazioni.

Le registrazioni delle call stanno nella cartella ``booking_<id>/`` (è il
fileNamePrefix che Agora usa in agora_recording). Le immagini non ci finiscono:
restano nelle cartelle che il sito usava già, distinte dai video.

- ``profile-pictures/`` foto profilo
- ``community-images/`` immagini allegate a una domanda della community
- ``call-attachments/`` file della chat durante la call

Credenziali, bucket e regione sono gli stessi delle registrazioni. Si leggono
a ogni chiamata, così un test può cambiarli. Nel database si salva l'URL
stabile, senza firma: la firma (7 giorni, come i video) si genera quando la
pagina deve mostrare il file. Il bucket delle registrazioni è privato.
"""
import os
from typing import Optional
from urllib.parse import unquote, urlparse

from loguru import logger

# Stessi nomi che legge agora_recording per i video.
NOME_CHIAVE = "AWS_ACCESS_KEY_ID"
NOME_SEGRETO = "AWS_SECRET_ACCESS_KEY"
NOME_BUCKET = "AWS_S3_BUCKET_NAME"
NOME_REGIONE = "AWS_S3_REGION"
REGIONE_DEFAULT = "eu-south-1"
NOMI_OBBLIGATORI = (NOME_CHIAVE, NOME_SEGRETO, NOME_BUCKET)

# Cartelle immagini già presenti nel codice. Non sono il prefisso dei video.
PREFISSO_PROFILO = "profile-pictures/"
PREFISSO_COMMUNITY = "community-images/"
PREFISSO_CHAT = "call-attachments/"
PREFISSI_IMMAGINI = (PREFISSO_PROFILO, PREFISSO_COMMUNITY, PREFISSO_CHAT)

# Bucket su cui finivano le immagini prima di usare quello delle registrazioni.
# Serve solo per riconoscere gli URL già salvati, non per i caricamenti nuovi.
BUCKET_IMMAGINI_STORICO = "ispiramy-images"

SCADENZA_URL = 604800  # 7 giorni, come get_recording_url
SEGNAPOSTO_IMMAGINE = "/static/immagine-mancante.svg"

_config_gia_segnalata = False
_locali_gia_segnalati = set()


def regione() -> str:
    return os.getenv(NOME_REGIONE) or REGIONE_DEFAULT


def bucket() -> Optional[str]:
    nome = (os.getenv(NOME_BUCKET) or "").strip()
    return nome or None


def mancanti() -> list:
    """Nomi delle variabili obbligatorie che non hanno un valore."""
    return [nome for nome in NOMI_OBBLIGATORI if not (os.getenv(nome) or "").strip()]


def segnala_mancanti(sempre: bool = False) -> list:
    """Scrive in errore i nomi mancanti. Mai i valori."""
    global _config_gia_segnalata
    assenti = mancanti()
    if not assenti:
        return []
    if sempre or not _config_gia_segnalata:
        _config_gia_segnalata = True
        logger.error(
            "S3 NON CONFIGURATO per immagini e allegati degli utenti. "
            "Variabili mancanti: " + ", ".join(assenti)
        )
    return assenti


def client(regione_override: Optional[str] = None):
    """Client boto3 del bucket delle registrazioni. None se manca la config."""
    if segnala_mancanti():
        return None
    import boto3

    return boto3.client(
        "s3",
        region_name=regione_override or regione(),
        aws_access_key_id=os.getenv(NOME_CHIAVE),
        aws_secret_access_key=os.getenv(NOME_SEGRETO),
    )


def riferimento(chiave: str, bucket_nome: Optional[str] = None, regione_nome: Optional[str] = None) -> str:
    """URL stabile, senza firma: è quello che si salva nel database."""
    nome = bucket_nome or bucket()
    zona = regione_nome or regione()
    return f"https://{nome}.s3.{zona}.amazonaws.com/{chiave}"


def carica(chiave: str, corpo: bytes, content_type: str, **extra) -> str:
    """Mette l'oggetto nel bucket delle registrazioni, sotto la chiave data.

    La chiave deve già essere una cartella immagini (profile-pictures/,
    community-images/, call-attachments/), non booking_<id>/.
    """
    if segnala_mancanti(sempre=True):
        raise RuntimeError("S3 non configurato: " + ", ".join(mancanti()))
    s3 = client()
    if s3 is None:
        raise RuntimeError("S3 non configurato: " + ", ".join(mancanti()))
    s3.put_object(
        Bucket=bucket(),
        Key=chiave,
        Body=corpo,
        ContentType=content_type,
        **extra,
    )
    logger.info(f"Caricato su S3 {bucket()}/{chiave}")
    return riferimento(chiave)


def firma(
    chiave: str,
    scadenza: int = SCADENZA_URL,
    bucket_nome: Optional[str] = None,
    regione_override: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """URL firmato per leggere un oggetto. Vuoto se S3 non è configurato."""
    if not chiave:
        return ""
    nome = bucket_nome or bucket()
    if not nome or segnala_mancanti():
        return ""
    s3 = client(regione_override=regione_override)
    if s3 is None:
        return ""
    params = {"Bucket": nome, "Key": chiave}
    if extra:
        params.update(extra)
    try:
        return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=scadenza)
    except Exception as errore:
        logger.error(
            f"S3: URL firmato non generato per {chiave} ({type(errore).__name__})"
        )
        return ""


def _bucket_leggibili() -> list:
    """Bucket di cui accettiamo un URL già salvato.

    I caricamenti nuovi vanno solo su AWS_S3_BUCKET_NAME. Questi altri nomi
    servono a ritrovare le immagini messe lì prima, se ci sono ancora.
    """
    nomi = []
    principale = bucket()
    if principale:
        nomi.append(principale)
    storico = (os.getenv("S3_BUCKET_NAME") or "").strip()
    if storico and storico not in nomi:
        nomi.append(storico)
    if BUCKET_IMMAGINI_STORICO not in nomi:
        nomi.append(BUCKET_IMMAGINI_STORICO)
    return nomi


def _regione_nel_host(host: str, nome_bucket: str) -> Optional[str]:
    prefisso = f"{nome_bucket}.s3."
    if not host.startswith(prefisso):
        return None
    resto = host[len(prefisso):]
    if resto == "amazonaws.com":
        return None
    if resto.endswith(".amazonaws.com"):
        return resto[: -len(".amazonaws.com")] or None
    return None


def riconosci(url: str):
    """(bucket, chiave, regione) se l'URL è un oggetto di un bucket noto.

    La query (la firma) si ignora: conta il percorso.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    chiave = unquote(parsed.path.lstrip("/"))
    if not chiave or ".." in chiave.split("/"):
        return None
    for nome in _bucket_leggibili():
        if parsed.netloc.startswith(f"{nome}.s3."):
            return nome, chiave, _regione_nel_host(parsed.netloc, nome)
    return None


def stabilizza(url: str, prefisso: str) -> Optional[str]:
    """URL senza firma, solo se la chiave sta nella cartella immagini indicata."""
    trovato = riconosci(url)
    if not trovato:
        return None
    nome, chiave, zona = trovato
    if not chiave.startswith(prefisso):
        return None
    return riferimento(chiave, bucket_nome=nome, regione_nome=zona or regione())


def _e_locale(valore: str) -> bool:
    return valore.startswith("/uploads/") or "/uploads/" in valore


def _avvisa_locale(percorso: str) -> None:
    if percorso in _locali_gia_segnalati:
        return
    _locali_gia_segnalati.add(percorso)
    logger.warning(
        "Immagine sul disco locale non più disponibile "
        f"(il disco del server si cancella a ogni deploy): {percorso}"
    )


def servi(valore, segnaposto: str = "") -> str:
    """URL che il browser può aprire.

    Un percorso /uploads/ è un file che stava sul disco del container: non
    c'è più, si mostra il segnaposto. Un oggetto sul nostro bucket si firma.
    Un altro indirizzo (avatar esterno, bucket sconosciuto) si lascia com'è.
    """
    if not isinstance(valore, str) or not valore.strip():
        return segnaposto
    valore = valore.strip()
    if _e_locale(valore):
        _avvisa_locale(valore)
        return segnaposto
    trovato = riconosci(valore)
    if not trovato:
        return valore
    nome, chiave, zona = trovato
    if not any(chiave.startswith(prefisso) for prefisso in PREFISSI_IMMAGINI):
        # Non è una nostra immagine (per esempio un video in booking_<id>/).
        return valore
    firmato = firma(chiave, bucket_nome=nome, regione_override=zona)
    if firmato:
        return firmato
    logger.warning(f"Immagine S3 non servibile: {chiave}")
    return segnaposto or valore
