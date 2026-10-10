"""
Utility per l'invio di email notifiche usando SendGrid.

Gestisce l'invio di email basate su template HTML configurabili.
"""
import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content
from app.logger_config import logger
from app.utils.orari import GIORNI_RILASCIO_SENZA_RECENSIONE
from typing import Dict, Optional


def send_notification_email(
    to_email: str,
    to_name: str,
    subject: str,
    template_name: str,
    template_data: Dict[str, str]
) -> bool:
    """
    Invia un'email di notifica usando SendGrid.
    
    Args:
        to_email: Email destinatario
        to_name: Nome destinatario
        subject: Oggetto email
        template_name: Nome del template (es: 'booking_confirmed.html')
        template_data: Dizionario con variabili per il template
            es: {'client_name': 'Mario', 'date': '15/11/2025', 'time': '14:00'}
    
    Returns:
        bool: True se inviata con successo, False altrimenti
    """
    try:
        # Verifica che SendGrid sia configurato
        # Usa SMTP_PASSWORD che contiene la chiave API SendGrid
        from app.utils.email_backend import is_backend_available
        sendgrid_api_key = os.getenv('SMTP_PASSWORD')
        from_email = os.getenv('FROM_EMAIL', 'noreply@ispiramy.com')

        if not is_backend_available():
            logger.warning("Nessun backend email disponibile (RESEND_API_KEY / SENDGRID_API_KEY / EMAIL_BACKEND=smtp mancanti), skip invio email")
            return False
        
        # Genera l'HTML del template
        html_content = generate_email_html(template_name, template_data)
        
        if not html_content:
            logger.error(f"Template {template_name} non trovato o errore generazione")
            return False
        
        # Crea il messaggio
        message = Mail(
            from_email=Email(from_email, "Ispiramy"),
            to_emails=To(to_email, to_name),
            subject=subject,
            html_content=Content("text/html", html_content)
        )
        
        # Invia tramite backend configurato (SendGrid o SMTP locale in dev)
        from app.utils.email_backend import mail_send
        response = mail_send(sendgrid_api_key, message)
        
        if response.status_code in [200, 201, 202]:
            logger.info(f"✅ Email notifica inviata a {to_email}: {subject}")
            return True
        else:
            logger.error(f"❌ Errore invio email a {to_email}: status {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Errore nell'invio email notifica: {e}")
        return False


def nota_compenso() -> str:
    """Come e quando il consulente viene pagato, in italiano e in inglese.

    Le email non seguono la lingua del sito: la regola sta in tutte e due,
    così chi legge in inglese la trova comunque.
    """
    giorni = GIORNI_RILASCIO_SENZA_RECENSIONE
    return (
        '<div style="background:#f1f8f0;border-left:4px solid #43a047;border-radius:6px;'
        'padding:14px 18px;margin:20px 0;">'
        "<p style='margin:0 0 8px;'>Il compenso del consulente resta trattenuto finché il cliente "
        "lascia la recensione e non c'è una contestazione aperta. Se la recensione non arriva, "
        f"parte {giorni} giorni dopo la fine della consulenza.</p>"
        "<p style='margin:0;color:#546e7a;font-size:13px;'>The consultant's payout stays on hold "
        "until the client leaves a review and there is no open dispute. If they never review, "
        f"it is sent {giorni} days after the consultation ends.</p></div>"
    )


# Testo sul pagamento nell'email di rifiuto, se il chiamante non ne passa uno
NOTA_RIMBORSO = (
    "se hai pagato, riceverai il rimborso entro 5-10 giorni lavorativi "
    "sul metodo di pagamento utilizzato."
)
NOTA_BLOCCO_ANNULLATO = (
    "non ti è stato addebitato nulla. L'importo era solo bloccato e il blocco è stato "
    "annullato: a seconda della banca può restare visibile qualche giorno sull'estratto conto."
)


# URL pubblico del logo per le email (PNG su S3 — gli email client non rendono bene gli SVG)
EMAIL_LOGO_URL = "https://ispiramy-images.s3.eu-north-1.amazonaws.com/logo-email.png"


def _branded_email(emoji: str, title: str, accent: str, accent_dark: str,
                   body_html: str, button_label: str = None, button_url: str = None) -> str:
    """Wrapper email brandizzato: header verde con logo + contenuto + footer.
    Usa stili inline (i client email spesso rimuovono i blocchi <style>)."""
    button = ""
    if button_label and button_url:
        button = (
            f'<div style="text-align:center;">'
            f'<a href="{button_url}" style="display:inline-block;background:{accent};color:#ffffff;'
            f'padding:14px 36px;text-decoration:none;border-radius:8px;margin:8px 0 4px;'
            f'font-weight:600;font-size:16px;">{button_label}</a></div>'
        )
    return f"""<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#eef1f4;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:#374151;">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">
    <div style="background:linear-gradient(135deg,{accent} 0%,{accent_dark} 100%);border-radius:16px 16px 0 0;padding:30px 24px;text-align:center;">
      <img src="{EMAIL_LOGO_URL}" alt="Ispiramy" width="46" height="46" style="display:block;margin:0 auto 6px;border:0;">
      <div style="color:#ffffff;font-size:22px;font-weight:800;letter-spacing:.3px;">Ispiramy</div>
      <div style="color:rgba(255,255,255,.96);font-size:17px;font-weight:600;margin-top:14px;">{emoji} {title}</div>
    </div>
    <div style="background:#ffffff;border-radius:0 0 16px 16px;padding:30px 28px;box-shadow:0 2px 12px rgba(0,0,0,.05);">
      {body_html}
      {button}
    </div>
    <div style="text-align:center;color:#9ca3af;font-size:12px;margin-top:22px;line-height:1.6;">
      <p style="margin:0;">Questa è un'email automatica da <strong style="color:{accent};">Ispiramy</strong>. Non rispondere a questo messaggio.</p>
      <p style="margin:6px 0 0;">© Ispiramy — La piattaforma che connette chi cerca aiuto con chi può darlo.</p>
    </div>
  </div>
</body>
</html>"""


def _details_box(rows_html: str, accent: str) -> str:
    """Box dettagli con bordo accentato (stile inline)."""
    return (
        f'<div style="background:#f9fafb;border:1px solid #eceef1;border-left:4px solid {accent};'
        f'border-radius:8px;padding:16px 20px;margin:20px 0;">{rows_html}</div>'
    )


def generate_email_html(template_name: str, data: Dict[str, str]) -> Optional[str]:
    """
    Genera l'HTML dell'email (brandizzato) sostituendo le variabili nel template.

    Args:
        template_name: Nome del template (es: 'booking_confirmed.html')
        data: Dizionario con le variabili da sostituire

    Returns:
        str: HTML generato, None se errore
    """
    try:
        # Alias per i nomi usati in alcune installazioni del DB: senza questa
        # normalizzazione il template non veniva trovato e l'email di promemoria
        # (1 ora / 10 minuti prima) non partiva affatto.
        template_name = {
            'booking_reminder_1h.html': 'reminder_1h.html',
            'booking_reminder_10min.html': 'reminder_10min.html',
            'booking_confirmation.html': 'booking_confirmed.html',
        }.get(template_name, template_name)

        # Palette accenti per tipo di email (su base verde bosco del brand)
        GREEN, GREEN_D = "#43a047", "#2e7d32"
        AMBER, AMBER_D = "#f39c12", "#e67e22"
        RED, RED_D = "#e74c3c", "#c0392b"
        GOLD, GOLD_D = "#f5a623", "#e8900c"

        def row(label, value):
            return f'<p style="margin:6px 0;color:#374151;"><strong>{label}</strong> {value}</p>'

        # Ogni template: emoji, titolo, accento, corpo, etichetta+url bottone
        if template_name == 'booking_confirmed.html':
            body = (
                "<p>Ciao <strong>{consultant_name}</strong>,</p>"
                "<p>Hai ricevuto una nuova prenotazione da <strong>{client_name}</strong>!</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}") + row("⏱️ Durata:", "{duration} minuti"), GREEN)
                + "<p>Preparati in anticipo e sii puntuale per offrire la migliore esperienza al tuo cliente.</p>"
                + nota_compenso()
            )
            return _fill(_branded_email("📅", "Nuova Prenotazione", GREEN, GREEN_D, body, "Visualizza Prenotazione", "{action_url}"), data)

        if template_name == 'reminder_1h.html':
            body = (
                "<p>Ciao <strong>{user_name}</strong>,</p>"
                "<p>La tua consulenza con <strong>{other_user_name}</strong> inizia tra <strong>1 ora</strong>!</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario inizio:", "{time}") + row("⏱️ Durata:", "{duration} minuti"), AMBER)
                + "<p>💡 <strong>Suggerimento:</strong> testa audio e video prima dell'inizio per evitare problemi tecnici.</p>"
            )
            return _fill(_branded_email("⏰", "Promemoria Consulenza", AMBER, AMBER_D, body, "Vai alla Prenotazione", "{action_url}"), data)

        if template_name == 'reminder_10min.html':
            body = (
                "<p>Ciao <strong>{user_name}</strong>,</p>"
                "<p style='font-size:18px;font-weight:bold;color:#c0392b;'>La tua consulenza inizia tra 10 MINUTI!</p>"
                + _details_box(row("👥 Con:", "{other_user_name}") + row("🕐 Orario:", "{time}") + row("⏱️ Durata:", "{duration} minuti"), RED)
                + "<p>✅ Controlla audio e video · ✅ Trova un luogo tranquillo · ✅ Tieni a portata i documenti utili.</p>"
            )
            return _fill(_branded_email("🚀", "Consulenza in Partenza", RED, RED_D, body, "Entra nella Stanza", "{action_url}"), data)

        if template_name == 'community_contact.html':
            body = (
                "<p>Ciao <strong>{author_name}</strong>,</p>"
                "<p>Qualcuno è interessato alla tua domanda e vuole contattarti!</p>"
                + _details_box(row("👤 Chi:", "{contact_name}") + row("📝 La tua domanda:", "{question_title}") + row("📅 Quando:", "{contact_date}"), GREEN)
                + "<p>Riceverai i suoi messaggi nella sezione chat di Ispiramy. Rispondi velocemente per aumentare le possibilità di ricevere una consulenza! 🚀</p>"
            )
            return _fill(_branded_email("💬", "Nuovo Messaggio dalla Community", GREEN, GREEN_D, body, "Apri Chat", "{action_url}"), data)

        if template_name == 'booking_refused.html':
            body = (
                "<p>Caro/a <strong>{client_name}</strong>,</p>"
                "<p>Purtroppo <strong>{consultant_name}</strong> ha rifiutato la consulenza che avevi prenotato.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}") + row("👤 Consulente:", "{consultant_name}"), RED)
                + "{reason_section}"
                + '<div style="background:#e8f5e9;border-left:4px solid #43a047;border-radius:6px;padding:14px 18px;margin:20px 0;">'
                  "<p style='margin:0;'><strong>💰 Pagamento:</strong> {refund_note}</p></div>"
                + "<p>Non demordere! Puoi cercare altri consulenti disponibili nel nostro catalogo.</p>"
            )
            data = {"refund_note": NOTA_RIMBORSO, **data}
            return _fill(_branded_email("⚠️", "Consulenza Rifiutata", RED, RED_D, body, "Cerca altri Consulenti", "{action_url}"), data)

        if template_name == 'booking_confirmed_client.html':
            body = (
                "<p>Ciao <strong>{client_name}</strong>,</p>"
                "<p>La tua consulenza con <strong>{consultant_name}</strong> è confermata e pagata.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}") + row("⏱️ Durata:", "{duration} minuti"), GREEN)
                + "<p>Riceverai un promemoria prima dell'inizio. All'orario stabilito entra in call dal tuo profilo: "
                  "il pulsante compare 10 minuti prima.</p>"
            )
            return _fill(_branded_email("✅", "Consulenza confermata", GREEN, GREEN_D, body, "Vai alle tue consulenze", "{action_url}"), data)

        if template_name == 'booking_cancelled.html':
            body = (
                "<p>Ciao <strong>{user_name}</strong>,</p>"
                "<p><strong>{other_name}</strong> ha annullato la consulenza che avevate in programma.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}"), RED)
                + "{reason_section}"
                + '<div style="background:#e8f5e9;border-left:4px solid #43a047;border-radius:6px;padding:14px 18px;margin:20px 0;">'
                  "<p style='margin:0;'><strong>💰 Pagamento:</strong> {refund_note}</p></div>"
            )
            data = {"refund_note": NOTA_RIMBORSO, **data}
            return _fill(_branded_email("❌", "Consulenza annullata", RED, RED_D, body, "Vai al tuo profilo", "{action_url}"), data)

        if template_name == 'dispute_opened.html':
            body = (
                "<p>Ciao <strong>{consultant_name}</strong>,</p>"
                "<p><strong>{client_name}</strong> ha aperto una contestazione sulla consulenza che avete svolto.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}"), AMBER)
                + "{reason_section}"
                + '<div style="background:#fff8e1;border-left:4px solid #f39c12;border-radius:6px;padding:14px 18px;margin:20px 0;">'
                  "<p style='margin:0 0 8px;'>Il compenso di questa consulenza resta trattenuto finché la contestazione "
                  "non è chiusa. Senza contestazione, parte quando il cliente lascia la recensione, oppure "
                  f"{GIORNI_RILASCIO_SENZA_RECENSIONE} giorni dopo la fine se la recensione non arriva. "
                  "Ti contatteremo se servono chiarimenti: se la consulenza era registrata, "
                  "la registrazione viene usata per verificare cosa è successo.</p>"
                  "<p style='margin:0;color:#546e7a;font-size:13px;'>This payout stays on hold until the dispute is closed. "
                  "With no open dispute, it is sent when the client leaves a review, or "
                  f"{GIORNI_RILASCIO_SENZA_RECENSIONE} days after the consultation ends if they never review. "
                  "We will contact you if we need more detail. If the consultation was recorded, "
                  "the recording is used to check what happened.</p></div>"
            )
            return _fill(_branded_email("⚠️", "Contestazione aperta", AMBER, AMBER_D, body, "Vai al tuo profilo", "{action_url}"), data)

        if template_name == 'booking_request.html':
            body = (
                "<p>Ciao <strong>{consultant_name}</strong>,</p>"
                "<p><strong>{client_name}</strong> ti ha chiesto una consulenza. Hai scelto di confermare a mano le prenotazioni: "
                "la consulenza si fa solo se la accetti.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}") + row("⏱️ Durata:", "{duration} minuti")
                               + row("📝 Argomento:", "{topic}"), AMBER)
                + '<div style="background:#fff8e1;border-left:4px solid #f39c12;border-radius:6px;padding:14px 18px;margin:20px 0;">'
                  "<p style='margin:0;'><strong>⏳ Rispondi entro il {deadline}.</strong> Il pagamento del cliente è bloccato e "
                  "viene incassato solo quando accetti. Se rifiuti o non rispondi in tempo, il blocco viene annullato.</p></div>"
            )
            return _fill(_branded_email("📩", "Nuova richiesta di consulenza", AMBER, AMBER_D, body, "Accetta o rifiuta", "{action_url}"), data)

        if template_name == 'booking_accepted.html':
            body = (
                "<p>Ciao <strong>{client_name}</strong>,</p>"
                "<p><strong>{consultant_name}</strong> ha accettato la tua richiesta: la consulenza è confermata.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}") + row("⏱️ Durata:", "{duration} minuti"), GREEN)
                + "<p>L'importo che era bloccato è stato ora addebitato. Riceverai un promemoria prima dell'inizio: "
                  "entra in call dal tuo profilo all'orario stabilito.</p>"
            )
            return _fill(_branded_email("✅", "Consulenza confermata", GREEN, GREEN_D, body, "Vai alle tue consulenze", "{action_url}"), data)

        if template_name == 'booking_request_expired.html':
            body = (
                "<p>Ciao <strong>{client_name}</strong>,</p>"
                "<p><strong>{consultant_name}</strong> non ha risposto in tempo alla tua richiesta di consulenza, "
                "che quindi è stata annullata.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}"), AMBER)
                + '<div style="background:#e8f5e9;border-left:4px solid #43a047;border-radius:6px;padding:14px 18px;margin:20px 0;">'
                  "<p style='margin:0;'><strong>💰 Nessun addebito:</strong> l'importo era solo bloccato e il blocco è stato annullato. "
                  "A seconda della banca può restare visibile qualche giorno sull'estratto conto.</p></div>"
                + "<p>Puoi prenotare con un altro consulente disponibile.</p>"
            )
            return _fill(_branded_email("⌛", "Richiesta non confermata", AMBER, AMBER_D, body, "Cerca un consulente", "{action_url}"), data)

        if template_name == 'review_request.html':
            body = (
                "<p>Ciao <strong>{user_name}</strong>,</p>"
                "<p>Com'è andata la tua consulenza con <strong>{consultant_name}</strong>?</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}"), GOLD)
                + "<p>La tua opinione è preziosa e aiuta altri utenti a scegliere il consulente giusto. Ci vogliono solo 30 secondi!</p>"
                + nota_compenso()
            )
            return _fill(_branded_email("⭐", "Lascia una Recensione", GOLD, GOLD_D, body, "Lascia la tua Recensione", "{review_url}"), data)

        if template_name == 'review_reminder.html':
            body = (
                "<p>Ciao <strong>{user_name}</strong>,</p>"
                "<p>Non hai ancora lasciato una recensione per la consulenza con <strong>{consultant_name}</strong>.</p>"
                + _details_box(row("📅 Data:", "{date}") + row("🕐 Orario:", "{time}"), AMBER)
                + "<p>Ci vogliono solo 30 secondi e aiuterai altri utenti a trovare il consulente perfetto! Se l'hai già fatto, ignora questo messaggio.</p>"
                + nota_compenso()
            )
            return _fill(_branded_email("🔔", "Non dimenticare la Recensione", AMBER, AMBER_D, body, "Lascia la tua Recensione", "{review_url}"), data)

        if template_name == 'review_received.html':
            body = (
                "<p>Ciao <strong>{consultant_name}</strong>,</p>"
                "<p><strong>{reviewer_name}</strong> ti ha lasciato una recensione per la tua consulenza!</p>"
                + _details_box(row("⭐ Valutazione:", "{rating}/5") + row("📅 Consulenza del:", "{date}"), GOLD)
                + '<div style="background:#fffbeb;border-left:4px solid #f5a623;border-radius:6px;padding:14px 18px;margin:20px 0;font-style:italic;color:#6b7280;">“{comment}”</div>'
                + "<p>Le recensioni aumentano la tua visibilità e la fiducia dei futuri clienti. Continua così! 🎉</p>"
            )
            return _fill(_branded_email("⭐", "Hai ricevuto una recensione", GOLD, GOLD_D, body, "Vedi il tuo profilo", "{action_url}"), data)

        logger.error(f"Template {template_name} non trovato")
        return None
    except Exception as e:
        logger.error(f"Errore generazione HTML template {template_name}: {e}")
        return None


# Chiavi il cui valore è già HTML costruito dal codice (con i dati utente al
# suo interno già escapati). Tutte le altre sono testo e vanno escapate.
_CHIAVI_HTML_FIDATE = {"reason_section"}


def _fill(html: str, data: Dict[str, str]) -> str:
    """Sostituisce i placeholder {key} con i valori. Rimuove i placeholder non forniti.

    I valori vengono escapati: arrivano da nomi, commenti delle recensioni e
    titoli scritti dagli utenti, e senza escaping chiunque poteva inserire
    markup (link, immagini, testo camuffato) nelle email che la piattaforma
    manda ad altri utenti. Vale anche per gli URL: dentro un href l'escaping
    di & in &amp; è quello corretto.
    """
    from html import escape

    for key, value in data.items():
        testo = "" if value is None else str(value)
        if key not in _CHIAVI_HTML_FIDATE:
            testo = escape(testo, quote=True)
        html = html.replace(f"{{{key}}}", testo)
    # reason_section opzionale: se non fornito, rimuovilo
    html = html.replace("{reason_section}", "")
    return html
