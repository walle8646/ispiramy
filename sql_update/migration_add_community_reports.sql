-- Segnalazioni delle domande della community
CREATE TABLE IF NOT EXISTS community_reports (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    reason VARCHAR(1000) NOT NULL,
    status VARCHAR(32) DEFAULT 'open',
    created_at DATETIME,
    resolved_at DATETIME,
    resolved_by INTEGER,
    UNIQUE (question_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_community_reports_question_id ON community_reports (question_id);
CREATE INDEX IF NOT EXISTS ix_community_reports_user_id ON community_reports (user_id);
CREATE INDEX IF NOT EXISTS ix_community_reports_status ON community_reports (status);
