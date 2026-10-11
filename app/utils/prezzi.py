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

# Commissione della piattaforma sul valore della consulenza, se il consulente
# non ne ha una sua. Le spese di servizio non c'entrano: restano a Ispiramy
# per intero, oltre a questa percentuale.
COMMISSIONE_DEFAULT = 20

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


def centesimi_rimborso_contestazione(booking, refund_pct: int) -> int:
    """Percentuale del solo prezzo della consulenza, in centesimi.

    In una contestazione accolta le spese di servizio restano a Ispiramy:
    il 100% rende il prezzo della consulenza, non il totale pagato. Una
    percentuale intermedia si applica a quello stesso prezzo.

    Annullamento in tempo e assenza del consulente non passano di qui:
    quelli rimborsano l'intero incasso, spese comprese.
    """
    pct = max(0, min(100, int(refund_pct)))
    return (centesimi(getattr(booking, "price", 0)) * pct) // 100


def percentuale_commissione(consulente, default: int = COMMISSIONE_DEFAULT) -> int:
    """Percentuale trattenuta da Ispiramy. Se manca, il 20%.

    Zero è una percentuale vera (nessuna commissione): non va scambiata per
    "non impostata", altrimenti un consulente a commissione zero pagherebbe
    comunque il 20%.
    """
    valore = getattr(consulente, "platform_fee_percent", None) if consulente is not None else None
    if valore is None:
        return default
    return int(valore)


def compenso_consulente_centesimi(prezzo, percentuale: int = COMMISSIONE_DEFAULT) -> int:
    """Quanto trasferire al consulente, in centesimi.

    Si calcola sul valore della consulenza, mai sulle spese di servizio.
    I centesimi passano da Decimal: `int(float(19.99) * 100)` vale 1998 e
    il consulente si ritrovava un centesimo in meno a ogni compenso.
    """
    lordo = centesimi(prezzo)
    commissione = (lordo * int(percentuale)) // 100
    return lordo - commissione
