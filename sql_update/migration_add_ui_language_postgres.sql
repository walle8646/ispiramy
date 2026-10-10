-- Lingua del sito salvata sul profilo, per email e notifiche
ALTER TABLE "user" ADD COLUMN IF NOT EXISTS ui_language VARCHAR(5) DEFAULT 'it';
