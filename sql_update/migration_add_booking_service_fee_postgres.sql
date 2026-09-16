-- Spese di servizio pagate dal cliente a ogni prenotazione (PostgreSQL).
-- Sono in aggiunta al prezzo della consulenza e restano a Ispiramy insieme
-- alla commissione: per questo stanno in una colonna a parte e non dentro
-- booking.price, su cui si calcolano commissione e pagamento al consulente.
-- Le prenotazioni già esistenti restano a NULL: non ne avevano.
ALTER TABLE booking ADD COLUMN IF NOT EXISTS service_fee NUMERIC(10, 2);
