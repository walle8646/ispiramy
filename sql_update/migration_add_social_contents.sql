-- Contenuti social: un'idea con il suo media, pubblicabile su più social (SQLite).
-- Le righe di social_drafts diventano le uscite: platform, orario e testo di
-- quella specifica pubblicazione, tutte collegate allo stesso contenuto.
-- Il collegamento delle bozze già esistenti lo fa l'app all'avvio
-- (ensure_social_contents): una bozza diventa un contenuto con una sola uscita.
CREATE TABLE IF NOT EXISTS social_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_question_id INTEGER,
    source_title VARCHAR(500),
    content_kind VARCHAR(20) NOT NULL DEFAULT 'immagini',
    caption_base VARCHAR(5000) NOT NULL DEFAULT '',
    captions VARCHAR(8000),
    media_urls VARCHAR(3000),
    extra_content VARCHAR(8000),
    created_at DATETIME,
    updated_at DATETIME
);
CREATE INDEX IF NOT EXISTS ix_social_contents_source_question_id ON social_contents (source_question_id);
CREATE INDEX IF NOT EXISTS ix_social_contents_content_kind ON social_contents (content_kind);

ALTER TABLE social_drafts ADD COLUMN content_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_social_drafts_content_id ON social_drafts (content_id);
