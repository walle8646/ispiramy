"""Tipi di notifica usati dal codice, e il loro inserimento nel database.

send_notification scarta in silenzio le notifiche il cui tipo non è in
notification_types: un tipo nuovo nel codice ma non nel database significa
un'email che non parte, senza nessun errore visibile. Per questo all'avvio
si aggiungono i tipi mancanti (senza toccare quelli esistenti, che l'admin
può aver personalizzato).

I nomi dei template devono combaciare con quelli gestiti in
notification_email.generate_email_html, altrimenti l'email non parte.
"""
from datetime import datetime

from app.logger_config import logger

# (type_key, nome, descrizione, in_app, send_email, oggetto email, template)
NOTIFICATION_TYPES = [
    ("community_contact", "Nuovo messaggio dalla community",
     "Quando ricevi un messaggio o contatto", True, True,
     "💬 Hai un nuovo messaggio su Ispiramy", "community_contact.html"),
    ("booking_confirmed", "Prenotazione confermata",
     "Quando una prenotazione viene confermata", True, True,
     "✅ Prenotazione confermata", "booking_confirmed.html"),
    ("booking_refused", "Prenotazione rifiutata",
     "Quando una prenotazione viene rifiutata", True, True,
     "❌ Prenotazione rifiutata", "booking_refused.html"),
    ("booking_request", "Richiesta di consulenza da accettare",
     "Al consulente che conferma a mano: una nuova richiesta da accettare o rifiutare", True, True,
     "📩 Nuova richiesta di consulenza da confermare", "booking_request.html"),
    ("booking_accepted", "Richiesta di consulenza accettata",
     "Al cliente quando il consulente accetta la richiesta", True, True,
     "✅ Il consulente ha accettato la tua richiesta", "booking_accepted.html"),
    ("booking_request_expired", "Richiesta di consulenza scaduta",
     "Al cliente quando il consulente non risponde entro la scadenza", True, True,
     "⌛ Il consulente non ha risposto alla tua richiesta", "booking_request_expired.html"),
    ("booking_confirmed_client", "Consulenza confermata (cliente)",
     "Al cliente quando la prenotazione è confermata e pagata", True, True,
     "✅ La tua consulenza è confermata", "booking_confirmed_client.html"),
    ("booking_cancelled", "Consulenza annullata",
     "All'altro partecipante quando una consulenza viene annullata", True, True,
     "❌ Consulenza annullata", "booking_cancelled.html"),
    ("dispute_opened", "Contestazione aperta",
     "Al consulente quando il cliente apre una contestazione su una consulenza", True, True,
     "⚠️ È stata aperta una contestazione su una tua consulenza", "dispute_opened.html"),
    ("dispute_decided", "Decisione sulla contestazione",
     "A cliente e consulente quando un amministratore chiude una contestazione", True, True,
     "⚖️ Decisione sulla contestazione", "dispute_decided.html"),
    ("reminder_1h", "Promemoria 1 ora prima",
     "Promemoria 1 ora prima della consulenza", True, True,
     "⏰ La tua consulenza è tra 1 ora", "reminder_1h.html"),
    ("reminder_10min", "Promemoria 10 minuti prima",
     "Promemoria 10 minuti prima della consulenza", True, False,
     "⏰ La tua consulenza è tra 10 minuti", "reminder_10min.html"),
    ("review_request", "Richiesta recensione",
     "Email per votare la consulenza", False, True,
     "Lascia una recensione su Ispiramy", "review_request.html"),
    ("review_received", "Recensione ricevuta",
     "Notifica al consulente quando riceve una recensione", True, True,
     "Hai ricevuto una recensione su Ispiramy", "review_received.html"),
    ("review_reminder", "Promemoria recensione",
     "Sollecito 24h dopo se la recensione non è stata lasciata", False, True,
     "Non dimenticare la recensione", "review_reminder.html"),
    # Tipi usati dallo scheduler: senza queste righe send_notification scarta
    # la notifica in silenzio e il consulente non sa nemmeno di essere stato pagato.
    ("payment_released", "Pagamento rilasciato",
     "Il compenso della consulenza è stato trasferito al consulente", True, False,
     None, None),
    ("payment_hold", "Pagamento sospeso",
     "Il pagamento resta bloccato per una contestazione in corso", True, False,
     None, None),
    ("booking_noshow", "Assenza alla consulenza",
     "Uno dei partecipanti non si è presentato", True, False,
     None, None),
]


def ensure_notification_types(eng=None) -> list[str]:
    """Inserisce i tipi di NOTIFICATION_TYPES che mancano. Ritorna le chiavi aggiunte."""
    from sqlmodel import Session, select

    from app.database import engine
    from app.models import NotificationType

    aggiunti = []
    try:
        with Session(eng or engine) as session:
            esistenti = set(session.exec(select(NotificationType.type_key)).all())
            adesso = datetime.utcnow()
            for key, nome, descr, in_app, email, oggetto, template in NOTIFICATION_TYPES:
                if key in esistenti:
                    continue
                session.add(NotificationType(
                    type_key=key, name=nome, description=descr,
                    in_app=in_app, send_email=email,
                    email_subject=oggetto, email_template=template,
                    is_active=True, created_at=adesso, updated_at=adesso,
                ))
                aggiunti.append(key)
            if aggiunti:
                session.commit()
    except Exception as e:  # noqa: BLE001
        # Due processi avviati insieme possono provarci entrambi: il secondo
        # trova la chiave unica già presa. Non deve impedire l'avvio.
        logger.warning(f"⚠️ Tipi di notifica non inseriti ({e})")
        return []
    if aggiunti:
        logger.warning(f"🛠️ Tipi di notifica aggiunti: {', '.join(aggiunti)}")
    return aggiunti
