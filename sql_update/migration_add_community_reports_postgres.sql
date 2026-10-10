-- Segnalazioni delle domande della community
CREATE TABLE IF NOT EXISTS community_reports (
    id SERIAL PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES community_questions(id),
    user_id INTEGER NOT NULL REFERENCES "user"(id),
    reason VARCHAR(1000) NOT NULL,
    status VARCHAR(32) DEFAULT 'open',
    created_at TIMESTAMP,
    resolved_at TIMESTAMP,
    resolved_by INTEGER REFERENCES "user"(id),
    UNIQUE (question_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_community_reports_question_id ON community_reports (question_id);
CREATE INDEX IF NOT EXISTS ix_community_reports_user_id ON community_reports (user_id);
CREATE INDEX IF NOT EXISTS ix_community_reports_status ON community_reports (status);
