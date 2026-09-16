from sqlmodel import SQLModel, create_engine, Session
import os
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
        AvailabilityBlock,  # ✅ Gestione disponibilità
        Booking,  # ✅ Gestione prenotazioni
        ConsultationOffer,  # ✅ Gestione offerte consulenze
        ConfigurationProperty,  # ✅ Configurazione globale
        FavoriteConsultant  # ✅ Consulenti preferiti
    )
    
    SQLModel.metadata.create_all(engine)
    ensure_added_columns()
    ensure_column_widths()
    ensure_check_constraints()

    from app.utils.notification_types import ensure_notification_types
    ensure_notification_types()

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
