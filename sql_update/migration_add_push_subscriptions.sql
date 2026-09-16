-- Dispositivi iscritti alle notifiche push (SQLite).
-- L'endpoint è l'indirizzo di consegna che il servizio del browser ci dà: è
-- unico perché la stessa installazione, riscrivendosi, deve aggiornare la
-- riga e non aggiungerne una seconda. Le righe si cancellano da sole quando
-- il servizio risponde 404 o 410 (vedi notifiche_push.py).
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    endpoint VARCHAR(800) NOT NULL,
    p256dh VARCHAR(200) NOT NULL,
    auth VARCHAR(100) NOT NULL,
    dispositivo VARCHAR(300),
    created_at DATETIME,
    last_used_at DATETIME
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_push_subscriptions_endpoint ON push_subscriptions (endpoint);
CREATE INDEX IF NOT EXISTS ix_push_subscriptions_user_id ON push_subscriptions (user_id);
