-- Dispositivi iscritti alle notifiche push (PostgreSQL).
-- L'endpoint è l'indirizzo di consegna che il servizio del browser ci dà: è
-- unico perché la stessa installazione, riscrivendosi, deve aggiornare la
-- riga e non aggiungerne una seconda. Le righe si cancellano da sole quando
-- il servizio risponde 404 o 410 (vedi notifiche_push.py).
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    endpoint VARCHAR(800) NOT NULL,
    p256dh VARCHAR(200) NOT NULL,
    auth VARCHAR(100) NOT NULL,
    dispositivo VARCHAR(300),
    created_at TIMESTAMP,
    last_used_at TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_push_subscriptions_endpoint ON push_subscriptions (endpoint);
CREATE INDEX IF NOT EXISTS ix_push_subscriptions_user_id ON push_subscriptions (user_id);
