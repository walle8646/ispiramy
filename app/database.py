from sqlmodel import SQLModel, create_engine, Session
import os
from datetime import datetime
from app.logger_config import logger
from contextlib import contextmanager

# Ottieni DATABASE_URL da environment
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ispiramy.db")

# 🔥 FIX per Render: postgres:// → postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Configurazione engine
connect_args = {}
if "sqlite" in DATABASE_URL:
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    echo=False,  # Log SQL queries (metti False in produzione)
    connect_args=connect_args,
    # Verifica la connessione prima di usarla e la ricrea se è stata chiusa.
    # Una connessione rimasta ferma nel pool (es. mentre l'utente paga su
    # Stripe) può essere stata chiusa dal server o dalla rete: senza questo la
    # prima query al ritorno falliva, e verify_token trattava l'errore come
    # "utente non autenticato" — l'utente risultava sloggato.
    pool_pre_ping=True,
    pool_recycle=1800,
)

@contextmanager
def get_session():
    """Context manager per sessione database"""
    with Session(engine) as session:
        yield session

def create_db_and_tables():
    """Crea tutte le tabelle se non esistono"""
    logger.info("Creating database and tables")
    # Import modelli per registrarli
    from app.models import (
        User, Category, CategoryHierarchy,
        Consultation, 
        Conversation, Message,
        CommunityQuestion,  # ✅ Solo CommunityQuestion, senza CommunityAnswer
        CommunityReport,  # ✅ Segnalazioni delle domande
        AvailabilityBlock,  # ✅ Gestione disponibilità
        Booking,  # ✅ Gestione prenotazioni
        ConsultationOffer,  # ✅ Gestione offerte consulenze
        ConfigurationProperty,  # ✅ Configurazione globale
        FavoriteConsultant,  # ✅ Consulenti preferiti
        TransactionProblem,  # ✅ Movimenti di denaro da riprovare
    )
    
    SQLModel.metadata.create_all(engine)
    ensure_tabelle_nuove()
    ensure_added_columns()
    ensure_column_widths()
    ensure_check_constraints()

    from app.utils.notification_types import ensure_notification_types, sostituisci_marchio_helpy
    ensure_notification_types()
    # Gli oggetti già salvati (le migrazioni SQL dicevano «Helpy») non vengono
    # toccati da ensure_notification_types: si sostituisce solo il marchio.
    sostituisci_marchio_helpy()
    # Chi esisteva prima dell'obbligo di conferma email restava bloccato al login.
    conferma_account_precedenti()

    ensure_social_contents()


def ensure_social_contents(eng=None) -> int:
    """Dà un contenuto alle bozze social che non ce l'hanno ancora.

    È la migrazione al modello "un contenuto, più uscite": prima ogni bozza si
    portava il proprio media, adesso il media sta sul contenuto e le bozze sono
    le uscite sui singoli social. Una bozza vecchia diventa un contenuto con una
    sola uscita: i media già generati restano dove sono, nessuno si perde.

    Idempotente: alla seconda esecuzione non trova più bozze scoperte.
    """
    from sqlmodel import Session, select

    from app.models import SocialContent, SocialDraft

    creati = 0
    with Session(eng or engine) as session:
        scoperte = session.exec(select(SocialDraft).where(SocialDraft.content_id == None)).all()  # noqa: E711
        for bozza in scoperte:
            tipo = bozza.content_kind or ("video_completo" if bozza.platform == "tiktok" else "immagini")
            contenuto = SocialContent(
                source_question_id=bozza.source_question_id,
                source_title=bozza.source_title,
                content_kind=tipo,
                caption_base=bozza.caption or "",
                media_urls=bozza.media_urls,
                extra_content=bozza.extra_content,
            )
            session.add(contenuto)
            session.flush()
            bozza.content_id = contenuto.id
            session.add(bozza)
            creati += 1
        if creati:
            session.commit()
            logger.info(f"🛠️ Schema: create {creati} contenuti social per le bozze esistenti")
    return creati


# Colonne aggiunte ai modelli DOPO che le tabelle esistevano già in produzione.
#
# create_all() crea le tabelle mancanti ma non aggiunge colonne a quelle
# esistenti. Senza migrazione, SQLModel include comunque la colonna in ogni
# SELECT e ogni query sulla tabella fallisce con "column does not exist".
# Lo staging si deploya da solo a ogni push su develop, quindi una colonna nuova
# nel modello rompeva l'app finché qualcuno non lanciava la migrazione a mano.
#
# Ogni voce corrisponde a una migrazione in sql_update/: qui ci sono solo le
# aggiunte di colonna, sicure da eseguire all'avvio (su PostgreSQL un ADD
# COLUMN nullable o con default costante è istantaneo e non riscrive la
# tabella). Indici e dati restano nei file SQL.
COLONNE_AGGIUNTE = [
    # (tabella, colonna, definizione SQL)                  migrazione
    ("booking", "review_token", "VARCHAR(64)"),             # migration_add_booking_review_token
    ("social_drafts", "publish_attempt", "INTEGER NOT NULL DEFAULT 0"),  # migration_add_social_publish_attempt
    ("social_drafts", "content_kind", "VARCHAR(20)"),        # migration_add_social_content_kind
    ("social_drafts", "content_id", "INTEGER"),              # migration_add_social_contents
    ("user", "confirmation_code_created_at", "TIMESTAMP"),     # migration_add_confirmation_code_created_at
    ("user", "auto_accept_bookings", "BOOLEAN NOT NULL DEFAULT TRUE"),  # migration_add_booking_acceptance
    ("booking", "acceptance_deadline", "TIMESTAMP"),         # migration_add_booking_acceptance
    ("booking", "paypal_authorization_id", "VARCHAR(64)"),   # migration_add_booking_acceptance
    ("booking", "service_fee", "NUMERIC(10, 2)"),            # migration_add_booking_service_fee
    ("user", "verified_category_ids", "TEXT"),               # migration_add_verified_categories
    ("user", "ui_language", "VARCHAR(5) DEFAULT 'it'"),      # lingua del sito, per email e notifiche
]


# Valori ammessi dai CHECK sulla tabella booking in PostgreSQL (creati dalle
# migrazioni in sql_update/). Un valore nuovo nel codice ma non nel vincolo fa
# fallire ogni UPDATE che lo usa: il vincolo va allineato insieme al codice.
VINCOLI_BOOKING = {
    "chk_booking_status": ("status", [
        "pending", "pending_payment", "awaiting_acceptance", "confirmed",
        "completed", "cancelled", "no_show",
    ]),
    "chk_payment_status": ("payment_status", [
        "pending", "authorized", "held", "paid", "released", "refunded",
        "partially_refunded", "voided", "failed",
    ]),
}


# Colonne troppo corte per i valori che il codice ci scrive oggi.
# (tabella, colonna, lunghezza minima richiesta)
#
# password_md5 nasceva come hash MD5, 32 caratteri. Da quando le password sono
# protette con bcrypt l'hash e' lungo 60: su PostgreSQL l'UPDATE falliva con
# "value too long", quindi reset password e registrazione rispondevano 500.
# SQLite non applica la lunghezza, percio' in sviluppo e nei test non si vedeva.
COLONNE_DA_ALLARGARE = [
    ("user", "password_md5", 255),      # migration_widen_password_hash
    # 'awaiting_acceptance' e' lungo 19 e la colonna e' VARCHAR(20): ci sta per
    # un pelo. Meglio dare spazio prima che uno stato nuovo rompa le prenotazioni.
    ("booking", "status", 30),          # migration_widen_password_hash
    ("booking", "payment_status", 30),  # migration_widen_password_hash
]


def ensure_column_widths(eng=None) -> list[str]:
    """Allarga le colonne di COLONNE_DA_ALLARGARE troppo corte (solo PostgreSQL).

    Idempotente: guarda la lunghezza dichiarata e agisce solo se serve.
    Allargare un VARCHAR non riscrive la tabella ed e' immediato.
    """
    from sqlalchemy import text

    eng = eng or engine
    if eng.dialect.name != "postgresql":
        return []

    allargate = []
    for tabella, colonna, minimo in COLONNE_DA_ALLARGARE:
        try:
            with eng.begin() as conn:
                lunghezza = conn.execute(text(
                    "SELECT character_maximum_length FROM information_schema.columns "
                    "WHERE table_name = :tabella AND column_name = :colonna"
                ), {"tabella": tabella, "colonna": colonna}).scalar()
                if lunghezza is None or lunghezza >= minimo:
                    continue  # colonna assente, senza limite, o gia' abbastanza larga
                conn.execute(text(
                    f'ALTER TABLE "{tabella}" ALTER COLUMN {colonna} TYPE VARCHAR({minimo})'
                ))
            allargate.append(f"{tabella}.{colonna}")
            logger.warning(
                f"🛠️ Schema: colonna {tabella}.{colonna} allargata a {minimo} caratteri "
                f"(era {lunghezza}, troppo corta per l'hash bcrypt)"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"❌ Schema: impossibile allargare {tabella}.{colonna} ({e}). "
                "Applicare a mano sql_update/migration_widen_password_hash_postgres.sql."
            )
    return allargate


def ensure_check_constraints(eng=None) -> list[str]:
    """Allinea i CHECK di booking ai valori usati dal codice (solo PostgreSQL).

    Tocca solo i vincoli che esistono già e a cui manca qualche valore. Il
    vincolo nuovo è aggiunto NOT VALID: vale per le scritture da qui in poi e
    non ricontrolla le righe esistenti, quindi non può fallire su dati vecchi.
    Ritorna i nomi dei vincoli aggiornati.
    """
    from sqlalchemy import text

    eng = eng or engine
    if eng.dialect.name != "postgresql":
        return []

    aggiornati = []
    for nome, (colonna, valori) in VINCOLI_BOOKING.items():
        try:
            with eng.begin() as conn:
                definizione = conn.execute(text(
                    "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'booking' AND c.conname = :nome"
                ), {"nome": nome}).scalar()
                if definizione is None or all(f"'{v}'" in definizione for v in valori):
                    continue
                elenco = ", ".join(f"'{v}'" for v in valori)
                conn.execute(text(f"ALTER TABLE booking DROP CONSTRAINT {nome}"))
                conn.execute(text(
                    f"ALTER TABLE booking ADD CONSTRAINT {nome} CHECK ({colonna} IN ({elenco})) NOT VALID"
                ))
            aggiornati.append(nome)
            logger.warning(f"🛠️ Schema: vincolo {nome} aggiornato con i nuovi valori")
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"❌ Schema: impossibile aggiornare il vincolo {nome} ({e}). "
                "Applicare a mano sql_update/migration_add_booking_acceptance_postgres.sql."
            )
    return aggiornati


def ensure_tabelle_nuove(eng=None) -> list[str]:
    """Crea le tabelle arrivate dopo che il database era già in produzione.

    create_all() le crea, ma non lascia traccia: se non lo fa, la segnalazione
    risponde errore e il conteggio nel menu admin resta a zero senza che si
    capisca perché. Se la tabella manca ancora, la creiamo e lo scriviamo nel log.
    """
    from sqlalchemy import inspect

    from app.models import CommunityReport, PushSubscription

    eng = eng or engine
    presenti = set(inspect(eng).get_table_names())
    creati = []
    for modello in (CommunityReport, PushSubscription):
        nome = modello.__tablename__
        if nome in presenti:
            continue
        modello.__table__.create(eng, checkfirst=True)
        logger.warning(
            f"🛠️ Schema: creata la tabella mancante {nome} "
            "(create_all non l'aveva aggiunta al database già esistente)"
        )
        creati.append(nome)
    return creati


def ensure_added_columns(eng=None) -> list[str]:
    """Aggiunge le colonne di COLONNE_AGGIUNTE che mancano nel database.

    Idempotente: controlla lo schema reale prima di ogni ALTER. Ritorna
    l'elenco delle colonne aggiunte (vuoto se era già tutto a posto).
    """
    from sqlalchemy import inspect, text

    eng = eng or engine
    tabelle = set(inspect(eng).get_table_names())
    aggiunte = []

    for tabella, colonna, definizione in COLONNE_AGGIUNTE:
        if tabella not in tabelle:
            continue  # appena creata da create_all, ha già tutte le colonne
        if colonna in {c["name"] for c in inspect(eng).get_columns(tabella)}:
            continue
        try:
            # Una transazione per colonna: su PostgreSQL un errore annulla
            # l'intera transazione e bloccherebbe anche le aggiunte successive.
            with eng.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{tabella}" ADD COLUMN {colonna} {definizione}'))
            aggiunte.append(f"{tabella}.{colonna}")
        except Exception as e:  # noqa: BLE001
            # Due processi avviati insieme possono provarci entrambi: se ora la
            # colonna c'è, l'ha aggiunta l'altro e va bene così.
            if colonna in {c["name"] for c in inspect(eng).get_columns(tabella)}:
                continue
            logger.error(
                f"❌ Schema: impossibile aggiungere {tabella}.{colonna} ({e}). "
                "Le query su questa tabella falliranno: applicare a mano la migrazione in sql_update/."
            )

    for nome in aggiunte:
        logger.warning(
            f"🛠️ Schema: aggiunta la colonna mancante {nome} "
            "(la migrazione in sql_update/ non era stata applicata)"
        )
    return aggiunte


# Dal 10 ottobre 2026 (messa in linea della conferma email, PR #12) un account
# nuovo resta fuori finché non inserisce il codice. Chi esisteva prima va
# considerato già confermato, anche se ha un codice di verifica rimasto in sospeso.
DATA_OBBLIGO_CONFERMA_EMAIL = datetime(2026, 10, 10)


def account_gia_esistente(created_at, google_id=None) -> bool:
    """True se l'account è nato prima dell'obbligo, o se l'email l'ha già verificata Google.

    created_at assente conta come precedente: i nuovi iscritti ricevono sempre la data.
    """
    if google_id:
        return True
    if created_at is None:
        return True
    if getattr(created_at, "tzinfo", None) is not None:
        created_at = created_at.replace(tzinfo=None)
    return created_at < DATA_OBBLIGO_CONFERMA_EMAIL


def conferma_account_precedenti(eng=None) -> int:
    """Segna come confermati gli account nati prima dell'obbligo di verifica.

    La colonna confirmed è nata a 0 per tutti, anche per chi usava già il sito
    e aveva già un codice in sospeso: quel codice non deve tenerli fuori.
    Chi si registra dal 10 ottobre 2026 in poi resta in attesa del codice.
    Chi entra con Google ha l'email già verificata dal provider, qualunque sia la data.

    Il codice in sospeso viene cancellato insieme alla conferma.
    Idempotente: alla seconda esecuzione non c'è più nessuno da aggiornare.
    """
    from sqlalchemy import inspect, text

    eng = eng or engine
    if "user" not in set(inspect(eng).get_table_names()):
        return 0
    colonne = {c["name"] for c in inspect(eng).get_columns("user")}
    if "confirmed" not in colonne:
        return 0

    assegnazioni = ["confirmed = 1"]
    if "confirmation_code" in colonne:
        assegnazioni.append("confirmation_code = NULL")
    if "confirmation_code_created_at" in colonne:
        assegnazioni.append("confirmation_code_created_at = NULL")

    motivi = []
    if "created_at" in colonne:
        motivi.append("(created_at IS NULL OR created_at < :limite)")
    else:
        motivi.append("1 = 1")
    if "google_id" in colonne:
        motivi.append("(google_id IS NOT NULL AND google_id != '')")

    sql = text(
        f'UPDATE "user" SET {", ".join(assegnazioni)} '
        f'WHERE (confirmed IS NULL OR confirmed != 1) AND ({" OR ".join(motivi)})'
    )
    try:
        with eng.begin() as conn:
            n = conn.execute(
                sql, {"limite": DATA_OBBLIGO_CONFERMA_EMAIL.strftime("%Y-%m-%d %H:%M:%S")}
            ).rowcount or 0
    except Exception as e:  # noqa: BLE001
        logger.error(f"❌ Conferma account precedenti non applicata ({e})")
        return 0
    if n:
        logger.warning(f"🛠️ Account già esistenti segnati come email confermata: {n}")
    return n
