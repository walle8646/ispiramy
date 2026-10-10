"""Titolo e testo delle notifiche in app, nella lingua di chi le riceve.

Chi chiama send_notification passa ancora le frasi italiane. Se la persona
ha il sito in inglese, qui si ricostruisce la stessa notifica in inglese
a partire dal tipo e dai dati già preparati per la mail.
"""
from typing import Dict, Optional, Tuple


def localizza_notifica(
    lingua: str,
    type_key: str,
    title: str,
    message: str,
    data: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    if lingua != "en":
        return title, message
    dati = data or {}
    titolo, testo = _inglese(type_key, title, message, dati)
    return titolo or title, testo or message


def _campo(dati: Dict[str, str], *chiavi, vuoto=""):
    for chiave in chiavi:
        valore = dati.get(chiave)
        if valore:
            return valore
    return vuoto


def _inglese(type_key: str, title: str, message: str, dati: Dict[str, str]) -> Tuple[str, str]:
    data = _campo(dati, "date")
    ora = _campo(dati, "time")
    cliente = _campo(dati, "client_name", "contact_name")
    consulente = _campo(dati, "consultant_name", "other_name", "other_user_name")

    if type_key == "booking_confirmed":
        return (
            "New booking",
            f"{cliente} booked a consultation on {data} at {ora}".strip(),
        )
    if type_key == "booking_confirmed_client":
        return (
            "Consultation confirmed",
            f"Your consultation with {consulente} on {data} at {ora} is confirmed".strip(),
        )
    if type_key == "booking_refused":
        return (
            "Consultation declined",
            f"{consulente} declined your consultation on {data} at {ora}".strip(),
        )
    if type_key == "booking_cancelled":
        return (
            "Consultation cancelled",
            f"{consulente} cancelled the consultation on {data} at {ora}".strip(),
        )
    if type_key == "booking_request":
        scadenza = _campo(dati, "deadline")
        return (
            "New consultation request",
            f"{cliente} asked for a consultation on {data} at {ora}. Accept or decline by {scadenza}.".strip(),
        )
    if type_key == "booking_accepted":
        return (
            "Request accepted",
            f"{consulente} accepted your consultation on {data} at {ora}".strip(),
        )
    if type_key == "booking_request_expired":
        return (
            "Request expired",
            f"{consulente} did not reply in time to your request on {data}".strip(),
        )
    if type_key == "reminder_1h":
        altro = _campo(dati, "other_user_name", "consultant_name")
        return (
            "Consultation reminder",
            f"Your consultation with {altro} starts in 1 hour (at {ora})".strip(),
        )
    if type_key == "reminder_10min":
        altro = _campo(dati, "other_user_name", "consultant_name")
        return (
            "Your consultation is starting",
            f"Your consultation with {altro} starts in 10 minutes. Get ready to confirm you are there.".strip(),
        )
    if type_key == "review_request":
        return (
            "Leave a review",
            f"Write a review for your consultation with {consulente}".strip(),
        )
    if type_key == "review_reminder":
        return (
            "Remember to leave a review",
            f"You have not reviewed your consultation with {consulente} yet".strip(),
        )
    if type_key == "review_received":
        voto = _campo(dati, "rating")
        chi = _campo(dati, "reviewer_name", "client_name")
        extra = f" ({voto}/5)" if voto else ""
        return (
            "You received a review",
            f"{chi} left you a review{extra} for the consultation on {data}.".strip(),
        )
    if type_key == "dispute_opened":
        return (
            "Dispute opened",
            f"{cliente} opened a dispute about the consultation on {data}".strip(),
        )
    if type_key == "dispute_decided":
        esito = _campo(dati, "outcome_en") or message
        titoli = {
            "Contestazione non accolta": "Dispute not upheld",
            "Contestazione accolta": "Dispute upheld",
            "Contestazione chiusa": "Dispute closed",
        }
        return titoli.get(title, "Decision on the dispute"), esito
    if type_key == "community_contact":
        chi = _campo(dati, "contact_name")
        return "New message", f"{chi} wants to contact you!" if chi else "You have a new message."
    if type_key == "payment_hold":
        if "The payout" in message:
            return "Payment on hold", message[message.index("The payout"):].strip()
        return "Payment on hold", message
    if type_key == "payment_released":
        return _pagamento(message)
    if type_key == "booking_noshow":
        return _assenza(title, message)
    return title, message


def _pagamento(message: str) -> Tuple[str, str]:
    import re
    trovato = re.search(r"€\s*([\d.,]+).*?#(\d+)", message)
    importo = trovato.group(1) if trovato else ""
    numero = trovato.group(2) if trovato else ""
    pezzo = f"€{importo} for consultation #{numero}".strip()
    if "PayPal" in message:
        return "Payment received", f"The payment of {pezzo} was transferred to your PayPal account."
    if message.startswith("Hai ricevuto"):
        return "Partial payment received", f"You received {pezzo}."
    return "Payment received", f"The payment of {pezzo} was transferred to your account."


def _assenza(title: str, message: str) -> Tuple[str, str]:
    import re
    numero = ""
    trovato = re.search(r"#(\d+)", message)
    if trovato:
        numero = trovato.group(1)
    frasi = {
        "Consulente assente": (
            "Consultant absent",
            f"The consultant did not attend consultation #{numero}. The refund was issued automatically.",
        ),
        "Consulenza persa": (
            "Missed consultation",
            f"You did not attend consultation #{numero}. The client was refunded automatically.",
        ),
        "Cliente assente": (
            "Client absent",
            f"The client did not attend consultation #{numero}. The payment goes ahead as usual.",
        ),
        "Consulenza non avvenuta": (
            "Consultation did not take place",
            f"Consultation #{numero} did not take place. The refund was issued automatically.",
        ),
    }
    return frasi.get(title, ("Missed consultation", message))
