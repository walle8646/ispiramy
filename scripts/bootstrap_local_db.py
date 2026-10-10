"""
Bootstrap del Postgres locale per sviluppo.

Idempotente: può essere eseguito più volte senza problemi.

Va eseguito DENTRO il container web (dove DATABASE_URL punta al db locale):

    docker compose exec web python scripts/bootstrap_local_db.py

Cosa fa:
1. Attende che il database sia raggiungibile (retry 30s).
2. Crea le tabelle SQLModel chiamando create_db_and_tables() — basta questo,
   le migrazioni .sql in sql_update/ non servono su un DB nuovo (sono per
   evoluzioni di DB esistenti). Le funzionalità sono già nei modelli.
3. Seed minimali per poter usare l'app: user_type, categorie principali +
   qualche sottocategoria, notification_types, limiti chat.
4. Crea un utente admin di test (admin@ispiramy.local / admin).

Reset completo: `docker compose down -v` (cancella il volume) e rilancia.
"""
import os
import sys
import time
import hashlib
from datetime import datetime
from pathlib import Path

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def wait_for_db(dsn: str, timeout: int = 30) -> None:
    start = time.time()
    while True:
        try:
            with psycopg2.connect(dsn, connect_timeout=3) as _:
                print("✅ Postgres raggiungibile.")
                return
        except psycopg2.OperationalError as e:
            if time.time() - start > timeout:
                print(f"❌ Timeout dopo {timeout}s in attesa del DB: {e}")
                sys.exit(1)
            print(f"⏳ Attendo postgres... ({e.__class__.__name__})")
            time.sleep(2)


def create_schema_via_sqlmodel() -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from app.database import create_db_and_tables  # noqa
    import app.models  # noqa: F401  — registra tutti i modelli nel metadata
    create_db_and_tables()
    print("✅ Schema SQLModel creato/verificato.")


# Tabelle che esistono in prod ma non sono nei modelli SQLModel (legacy).
# Solo quelle critiche per far girare l'app — le altre sono opzionali.
EXTRA_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS user_type (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS message_counter_reset (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER,
    reset_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def create_extra_tables(dsn: str) -> None:
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute(EXTRA_TABLES_SQL)
    finally:
        conn.close()
    print("✅ Tabelle extra (user_type, message_counter_reset) create.")


# Categorie minimali per far girare la UI: 8 principali + 3 sottocategorie ciascuna.
CATEGORIES_PRINCIPAL = [
    (1, "Lavoro & Carriera", "lavoro-carriera", "💼", "#2196F3"),
    (2, "Fiscale & Tributario", "fiscale-tributario", "📋", "#4CAF50"),
    (3, "Legale", "legale", "⚖️", "#9C27B0"),
    (4, "Psicologia & Benessere", "psicologia-benessere", "🧠", "#E91E63"),
    (5, "Tecnologia & Digitale", "tecnologia-digitale", "💻", "#00BCD4"),
    (6, "Marketing & Business", "marketing-business", "📈", "#FF9800"),
    (7, "Immobiliare", "immobiliare", "🏠", "#795548"),
    (8, "Formazione & Coaching", "formazione-coaching", "🎓", "#607D8B"),
]

# id_parent -> [(id_sub, name, slug, icon, color)]
CATEGORIES_SUB = {
    1: [
        (101, "Trovare lavoro", "trovare-lavoro", "🔍", "#2196F3"),
        (102, "Cambio carriera", "cambio-carriera", "🔄", "#2196F3"),
        (103, "CV e LinkedIn", "cv-linkedin", "📄", "#2196F3"),
    ],
    2: [
        (201, "Partita IVA", "partita-iva", "🧾", "#4CAF50"),
        (202, "Dichiarazione redditi", "dichiarazione-redditi", "💶", "#4CAF50"),
        (203, "Tasse aziendali", "tasse-aziendali", "🏢", "#4CAF50"),
    ],
    3: [
        (301, "Contratti", "contratti", "📜", "#9C27B0"),
        (302, "Diritto del lavoro", "diritto-lavoro", "👔", "#9C27B0"),
        (303, "Diritto civile", "diritto-civile", "🏛️", "#9C27B0"),
    ],
    4: [
        (401, "Ansia & Stress", "ansia-stress", "😰", "#E91E63"),
        (402, "Relazioni", "relazioni", "💞", "#E91E63"),
        (403, "Crescita personale", "crescita-personale", "🌱", "#E91E63"),
    ],
    5: [
        (501, "Sviluppo Web", "sviluppo-web", "🌐", "#00BCD4"),
        (502, "Mobile & App", "mobile-app", "📱", "#00BCD4"),
        (503, "Data & AI", "data-ai", "🤖", "#00BCD4"),
    ],
    6: [
        (601, "SEO & SEM", "seo-sem", "🔍", "#FF9800"),
        (602, "Social Media", "social-media", "📲", "#FF9800"),
        (603, "Branding", "branding", "🎨", "#FF9800"),
    ],
    7: [
        (701, "Acquisto casa", "acquisto-casa", "🔑", "#795548"),
        (702, "Affitto", "affitto", "🏘️", "#795548"),
        (703, "Investimenti immobiliari", "investimenti-immobiliari", "💰", "#795548"),
    ],
    8: [
        (801, "Public speaking", "public-speaking", "🎤", "#607D8B"),
        (802, "Leadership", "leadership", "👑", "#607D8B"),
        (803, "Studio efficace", "studio-efficace", "📚", "#607D8B"),
    ],
}

# L'elenco dei tipi di notifica sta in app/utils/notification_types.py ed è
# inserito all'avvio dell'app (create_db_and_tables): una sola fonte.


def seed_user_types(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM user_type")
        if cur.fetchone()[0] > 0:
            print("   ℹ️ user_type già popolato.")
            return
        cur.execute(
            "INSERT INTO user_type (id, name) VALUES (1,'User'),(2,'Verifier'),(3,'Admin')"
        )
    print("   ✅ user_type popolato (User/Verifier/Admin).")


def seed_categories(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM category")
        if cur.fetchone()[0] > 0:
            print("   ℹ️ category già popolata.")
            return
        for (cid, name, slug, icon, color) in CATEGORIES_PRINCIPAL:
            cur.execute(
                "INSERT INTO category (id, name, slug, icon, description, color, "
                "is_principal) VALUES (%s,%s,%s,%s,%s,%s,true)",
                (cid, name, slug, icon, f"Consulenze su {name}", color),
            )
        for parent_id, subs in CATEGORIES_SUB.items():
            for (sub_id, sname, sslug, sicon, scolor) in subs:
                cur.execute(
                    "INSERT INTO category (id, name, slug, icon, description, color, "
                    "is_principal) "
                    "VALUES (%s,%s,%s,%s,%s,%s,false)",
                    (sub_id, sname, sslug, sicon, sname, scolor),
                )
        # Sync sequence dopo gli insert con ID espliciti
        cur.execute(
            "SELECT setval('category_id_seq', (SELECT MAX(id) FROM category))"
        )
    print(f"   ✅ category popolata ({len(CATEGORIES_PRINCIPAL)} principali + sottocategorie).")


def seed_category_hierarchy(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM category_hierarchy")
        if cur.fetchone()[0] > 0:
            print("   ℹ️ category_hierarchy già popolata.")
            return
        now = datetime.utcnow()
        for parent_id, subs in CATEGORIES_SUB.items():
            for position, (sub_id, *_rest) in enumerate(subs):
                cur.execute(
                    "INSERT INTO category_hierarchy "
                    "(parent_category_id, child_category_id, position, created_at) "
                    "VALUES (%s,%s,%s,%s)",
                    (parent_id, sub_id, position, now),
                )
    print("   ✅ category_hierarchy popolata.")


def seed_notification_types(conn) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from app.utils.notification_types import NOTIFICATION_TYPES

    with conn.cursor() as cur:
        cur.execute("SELECT type_key FROM notification_types")
        esistenti = {r[0] for r in cur.fetchall()}
        now = datetime.utcnow()
        nuovi = 0
        for (key, name, desc, in_app, send_email, subj, tpl) in NOTIFICATION_TYPES:
            if key in esistenti:
                continue
            cur.execute(
                "INSERT INTO notification_types "
                "(type_key, name, description, in_app, send_email, email_subject, "
                "email_template, is_active, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,true,%s,%s)",
                (key, name, desc, in_app, send_email, subj, tpl, now, now),
            )
            nuovi += 1
    print(f"   ✅ notification_types: {nuovi} tipi aggiunti ({len(NOTIFICATION_TYPES)} previsti).")


# Limiti della chat, letti a runtime da app/routes/messages.py.
# Senza queste righe l'app ripiega sui default scritti nel codice, e in locale
# non si riesce a provare l'effetto di una configurazione diversa.
CONFIGURATION_PROPERTIES = [
    ("MAX_MESSAGES_PER_CONVERSATION", "60", "Numero massimo di messaggi per conversazione"),
    ("MAX_MESSAGE_LENGTH", "1000", "Lunghezza massima in caratteri per un singolo messaggio"),
]


def seed_configuration_properties(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM configuration_property")
        if cur.fetchone()[0] > 0:
            print("   ℹ️ configuration_property già popolata.")
            return
        now = datetime.utcnow()
        for (key, value, desc) in CONFIGURATION_PROPERTIES:
            cur.execute(
                "INSERT INTO configuration_property "
                "(property_key, property_value, description, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (key, value, desc, now, now),
            )
    print(f"   ✅ configuration_property popolata ({len(CONFIGURATION_PROPERTIES)} chiavi).")


def apply_seeds(dsn: str) -> None:
    print("🌱 Applico seed (user_type / category / notification_types / configurazione)...")
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        seed_user_types(conn)
        seed_categories(conn)
        seed_category_hierarchy(conn)
        seed_notification_types(conn)
        seed_configuration_properties(conn)
    finally:
        conn.close()
    print("✅ Seed completati.")


def create_admin_user(dsn: str) -> None:
    print("👤 Creo utente admin di test...")
    email = "admin@ispiramy.local"
    # bcrypt come in produzione: l'app non accetta piu' MD5 per le nuove password
    import bcrypt
    password_md5 = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode()
    now = datetime.utcnow()
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            existing = cur.fetchone()
            if existing:
                print(f"   ℹ️ Admin già esistente (id={existing[0]}).")
                return
            cur.execute(
                'INSERT INTO "user" '
                '(email, password_md5, nome, cognome, '
                ' consulenze_vendute, consulenze_acquistate, confirmed, '
                ' is_verified, is_anonymous, notify_category_requests, user_type_id, '
                ' stripe_onboarding_complete, platform_fee_percent, created_at) '
                'VALUES (%s,%s,%s,%s, 0,0,1, true,false,true,3, false,15,%s) '
                'RETURNING id',
                (email, password_md5, "Admin", "Test", now),
            )
            uid = cur.fetchone()[0]
            print(f"   ✅ Admin creato (id={uid}, email={email}, password=admin).")
    finally:
        conn.close()


def main() -> None:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("❌ DATABASE_URL non impostato.")
        sys.exit(1)
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)
    if "render.com" in dsn or "frankfurt-postgres" in dsn:
        print("🛑 SICUREZZA: DATABASE_URL punta a Render (produzione).")
        print("   Questo script va eseguito SOLO sul DB locale.")
        sys.exit(1)

    print(f"🎯 Target DB: {dsn.split('@')[-1] if '@' in dsn else dsn}")
    wait_for_db(dsn)
    create_schema_via_sqlmodel()
    create_extra_tables(dsn)
    apply_seeds(dsn)
    create_admin_user(dsn)
    print("\n🎉 Bootstrap completato.")
    print("   Login dev: admin@ispiramy.local / admin")
    print("   App: http://localhost:10000")


if __name__ == "__main__":
    main()
