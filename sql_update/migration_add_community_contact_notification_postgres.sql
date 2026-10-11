-- Migration: Aggiunge tipo notifica per nuovi messaggi
-- PostgreSQL version
-- La notifica viene inviata quando:
--   1. Viene scritto il PRIMO messaggio in una conversazione
--   2. Viene scritto un messaggio dopo >30 minuti dall'ultimo messaggio

INSERT INTO notification_types (type_key, name, description, in_app, send_email, email_subject, email_template) VALUES
(
    'community_contact',
    'Nuovo Messaggio',
    'Notifica quando ricevi un nuovo messaggio o quando una conversazione viene riattivata dopo 30+ minuti',
    TRUE,  -- in_app: SI
    TRUE,  -- send_email: SI
    '💬 Hai ricevuto un nuovo messaggio su Ispiramy!',
    'community_contact.html'
)
ON CONFLICT (type_key) DO NOTHING;
