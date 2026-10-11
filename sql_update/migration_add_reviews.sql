-- Migration: Aggiunge tabella reviews per le recensioni post-consulenza (SQLite)
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES booking(id),
    reviewer_user_id INTEGER NOT NULL REFERENCES user(id),
    consultant_user_id INTEGER NOT NULL REFERENCES user(id),
    rating_helpful INTEGER NOT NULL,
    rating_prepared INTEGER NOT NULL,
    rating_communication INTEGER NOT NULL,
    comment TEXT,
    review_token TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_reviews_booking_id ON reviews(booking_id);
CREATE INDEX IF NOT EXISTS ix_reviews_reviewer_user_id ON reviews(reviewer_user_id);
CREATE INDEX IF NOT EXISTS ix_reviews_consultant_user_id ON reviews(consultant_user_id);
CREATE INDEX IF NOT EXISTS ix_reviews_review_token ON reviews(review_token);

-- Notification types per review
INSERT OR IGNORE INTO notification_types (type_key, name, description, in_app, send_email, email_subject, email_template, is_active)
VALUES 
    ('review_request', 'Richiesta Recensione', 'Email per votare la consulenza', 0, 1, 'Lascia una recensione per la tua consulenza su Ispiramy', 'review_request.html', 1),
    ('review_reminder', 'Promemoria Recensione', 'Promemoria per votare la consulenza dopo 24h', 0, 1, 'Ricordati di lasciare una recensione su Ispiramy', 'review_reminder.html', 1);
