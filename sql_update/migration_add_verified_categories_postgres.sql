-- Badge verificato per categoria (PostgreSQL).
-- La colonna viene aggiunta anche in automatico all'avvio
-- (app/database.py, ensure_added_columns).
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS verified_category_ids TEXT;
