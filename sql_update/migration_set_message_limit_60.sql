-- Il limite di una conversazione è 60 messaggi.
-- Prima il banner della chat diceva 15 e il default nel database era 80:
-- due numeri diversi, nessuno dei due quello deciso.
-- Un valore diverso da 15 e da 80 (scelto di proposito) non si tocca.
-- Stessa istruzione su SQLite e PostgreSQL.

UPDATE configuration_property
SET property_value = '60',
    updated_at = CURRENT_TIMESTAMP
WHERE property_key = 'MAX_MESSAGES_PER_CONVERSATION'
  AND property_value IN ('15', '80');
