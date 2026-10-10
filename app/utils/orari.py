"""Orari: una sola convenzione per tutto quello che finisce nel database.

Le colonne datetime dei modelli sono TIMESTAMP WITHOUT TIME ZONE e contengono
**ora italiana senza fuso**, come gli orari delle prenotazioni.

Salvarci un datetime CON fuso (`datetime.now(ITALY_TZ)`) è un errore sottile:
psycopg2 lo manda a PostgreSQL come timestamptz, e PostgreSQL, per farlo
entrare in una colonna senza fuso, lo converte nel fuso della sessione. Con un
database in UTC (il default di Render) le 15:30 italiane diventano 13:30.
SQLite invece toglie semplicemente il fuso: in locale e nei test il problema
non si vede.
"""
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

ITALY_TZ = ZoneInfo("Europe/Rome")

# ===== Regole di preavviso delle consulenze =====
# Erano scritte a mano in cinque punti diversi (calcolo degli slot, creazione
# della prenotazione su Stripe e su PayPal, annullamento, rifiuto) piu' tre nei
# template: cambiarle significava trovarle tutte.

# Quanto prima va prenotata una consulenza: gli slot piu' vicini di cosi' non
# compaiono nemmeno nel calendario.
ORE_PREAVVISO_PRENOTAZIONE = 2

# Fino a quando cliente e consulente possono annullare una consulenza gia'
# confermata (dopo, chi non si presenta viene gestito dal controllo assenze).
ORE_LIMITE_ANNULLAMENTO = 2

# La videochiamata si apre 10 minuti prima dell'orario prenotato e resta
# aperta fino a 5 minuti dopo la fine: lo stesso margine con cui la call si
# chiude da sola. Fuori da questa fascia non si entra e non si segna la
# presenza (è quella che decide rimborsi e compenso).
MINUTI_ANTICIPO_INGRESSO = 10
MINUTI_MARGINE_FINE_CALL = 5

# Se il cliente non lascia la recensione, il compenso del consulente parte
# comunque dopo queste ore dalla fine della consulenza. Una contestazione
# aperta lo ferma in ogni caso, finché non viene chiusa. È l'unico punto in
# cui è scritto questo numero: prenotazioni, job e testi lo leggono da qui.
ORE_RILASCIO_SENZA_RECENSIONE = 48


# Come si scrive un orario a chi lo legge. Il sito lavora in ora italiana:
# finche' e' cosi', va detto ogni volta che un orario finisce in un messaggio
# o in un'email, perche' chi vive in un altro fuso non ha modo di saperlo.
FUSO_MOSTRATO = "ora italiana"


def con_fuso(ora: str) -> str:
    """Un orario scritto per una persona: "15:00 (ora italiana)"."""
    if not ora:
        return ""
    return f"{ora} ({FUSO_MOSTRATO})"


def data_consulenza(valore) -> date:
    """La data di una prenotazione, comunque arrivi dal database.

    booking.booking_date e' DATE su PostgreSQL e DATETIME su SQLite: il modello
    la dichiara datetime, quindi in produzione arriva un `date` e chiamarci
    `.date()` sopra solleva AttributeError. E' quello che faceva fallire con un
    500 la richiesta del token della call ("il server ha risposto con un errore
    (500)"): in sviluppo e nei test, su SQLite, non si vedeva.
    """
    if isinstance(valore, str):
        valore = datetime.fromisoformat(valore.split()[0])
    if isinstance(valore, datetime):
        return valore.date()
    return valore


def _come_orario(valore) -> time:
    """Un orario di prenotazione, che dal database può arrivare stringa o time."""
    if isinstance(valore, time):
        return valore.replace(microsecond=0)
    testo = str(valore).strip()
    for formato in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(testo, formato).time()
        except ValueError:
            continue
    raise ValueError(f"orario non valido: {valore}")


def inizio_consulenza(booking) -> datetime:
    """Inizio della consulenza, in ora italiana senza fuso."""
    return datetime.combine(data_consulenza(booking.booking_date), _come_orario(booking.start_time))


def fine_consulenza(booking) -> datetime:
    """Fine della consulenza. Se l'orario di fine è dopo la mezzanotte, è il giorno dopo."""
    if getattr(booking, "end_time", None):
        inizio = inizio_consulenza(booking)
        fine = datetime.combine(data_consulenza(booking.booking_date), _come_orario(booking.end_time))
        if fine <= inizio:
            fine += timedelta(days=1)
        return fine
    return inizio_consulenza(booking) + timedelta(minutes=int(getattr(booking, "duration_minutes", None) or 60))


def finestra_ingresso(booking, adesso: Optional[datetime] = None) -> str:
    """Quando si può entrare in videochiamata: 'presto', 'aperta' o 'chiusa'.

    Aperta da 10 minuti prima dell'inizio fino a 5 minuti dopo la fine.
    """
    adesso = adesso or now_italy_naive()
    if adesso.tzinfo is not None:
        adesso = adesso.astimezone(ITALY_TZ).replace(tzinfo=None)
    inizio = inizio_consulenza(booking)
    fine = fine_consulenza(booking)
    if adesso < inizio - timedelta(minutes=MINUTI_ANTICIPO_INGRESSO):
        return "presto"
    if adesso >= fine + timedelta(minutes=MINUTI_MARGINE_FINE_CALL):
        return "chiusa"
    return "aperta"


def scadenza_compenso_senza_recensione(fine: datetime) -> datetime:
    """Quando il compenso parte se il cliente non ha lasciato la recensione."""
    return fine + timedelta(hours=ORE_RILASCIO_SENZA_RECENSIONE)


def now_italy_naive() -> datetime:
    """Ora corrente italiana, senza fuso: il valore da salvare nel database."""
    return datetime.now(ITALY_TZ).replace(tzinfo=None)


def iso_ora_italiana(dt: Optional[datetime]) -> Optional[str]:
    """ISO 8601 con il fuso esplicito, per mandare al browser un orario salvato.

    Senza offset ("2026-09-11T15:30:00") il browser interpreta l'orario nel
    proprio fuso; con l'offset il risultato è lo stesso istante ovunque.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ITALY_TZ)
    return dt.isoformat()
