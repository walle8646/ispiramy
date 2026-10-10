-- Migration: Add configuration_property table
-- Description: Create a table to store application configuration properties

CREATE TABLE IF NOT EXISTS configuration_property (
    id INT AUTO_INCREMENT PRIMARY KEY,
    property_key VARCHAR(100) NOT NULL UNIQUE,
    property_value VARCHAR(500) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Insert default configuration values
INSERT INTO configuration_property (property_key, property_value, description) VALUES
('MAX_MESSAGES_PER_CONVERSATION', '60', 'Numero massimo di messaggi per conversazione'),
('MAX_MESSAGE_LENGTH', '1000', 'Lunghezza massima in caratteri per un singolo messaggio');
