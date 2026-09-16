-- Tipo di contenuto di una bozza social (SQLite).
-- 'immagini' = post con foto o carosello, 'video_slide' = video fatto con le
-- nostre slide, 'video_completo' = video con le clip di repertorio. Decide
-- come si genera il media e in quale scheda dell'admin compare la bozza.
-- Vuoto = il tipo di partenza della piattaforma (TikTok video, gli altri
-- immagini), quindi le righe già esistenti restano dove sono.
ALTER TABLE social_drafts ADD COLUMN content_kind VARCHAR(20);
