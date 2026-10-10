-- Badge verificato per categoria (SQLite).
-- La colonna viene aggiunta anche in automatico all'avvio
-- (app/database.py, ensure_added_columns). NULL significa «non ancora
-- distinto»: il badge resta sulle categorie dichiarate dal profilo.
ALTER TABLE "user" ADD COLUMN verified_category_ids TEXT;
