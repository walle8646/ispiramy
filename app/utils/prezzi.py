"""
Prezzi delle consulenze e spese di servizio.

Il cliente paga la consulenza piu' le **spese di servizio**, che restano a
Ispiramy insieme alla commissione sul valore della consulenza. Sono due cose
distinte e vanno tenute separate ovunque:

- `booking.price` e' il valore della consulenza. Su quello si calcolano la
  commissione e il pagamento al consulente.
- `booking.service_fee` e' quanto il cliente ha pagato in piu'. Non entra mai
  nel conto del consulente.

Le spese vengono salvate sulla prenotazione al momento dell'incasso: se un
domani cambiano, le prenotazioni gia' fatte restano quelle che erano.
"""
from decimal import ROUND_HALF_UP, Decimal

# Spese di servizio pagate dal cliente a ogni prenotazione
SPESE_SERVIZIO = Decimal("1.99")

# Tariffa oraria minima che un consulente puo' impostare
PREZZO_ORARIO_MINIMO = 20

CENTESIMO = Decimal("0.01")


def _decimale(valore) -> Decimal:
    return Decimal(str(valore or 0)).quantize(CENTESIMO, rounding=ROUND_HALF_UP)


def spese_servizio() -> Decimal:
    """Le spese di servizio da applicare a una nuova prenotazione."""
    return SPESE_SERVIZIO


def totale_cliente(prezzo) -> Decimal:
    """Quanto paga il cliente: consulenza piu' spese di servizio."""
    return _decimale(_decimale(prezzo) + SPESE_SERVIZIO)


def totale_pagato(booking) -> Decimal:
    """Quanto ha pagato il cliente per questa prenotazione.

    Le prenotazioni fatte prima delle spese di servizio non ne hanno: per
    quelle il totale e' il solo prezzo della consulenza.
    """
    return _decimale(_decimale(getattr(booking, "price", 0)) + _decimale(getattr(booking, "service_fee", 0)))


def centesimi(importo) -> int:
    """L'importo in centesimi, come lo vogliono Stripe e i calcoli interni."""
    return int(_decimale(importo) * 100)
