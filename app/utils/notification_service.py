"""
Sistema centralizzato per l'invio di notifiche in-app ed email.

Gestisce la creazione di notifiche controllando la configurazione
in notification_types per decidere se inviare in-app e/o email.
"""
from sqlmodel import Session, select
from app.database import engine
from app.models import Notification, NotificationType, User
from app.utils.notification_email import send_notification_email
from app.logger_config import logger
from typing import Optional, Dict


def send_notification(
    user_id: int,
    type_key: str,
    title: str,
    message: str,
    template_data: Optional[Dict[str, str]] = None,
    related_booking_id: Optional[int] = None,
    related_user_id: Optional[int] = None,
    action_url: Optional[str] = None
) -> bool:
    """
    Invia una notifica controllando la configurazione in notification_types.
    
    Questa funzione:
    1. Controlla in notification_types se il tipo è configurato
    2. Se in_app=True: crea notifica nel database
    3. Se send_email=True: invia email usando SendGrid
    
    Args:
        user_id: ID destinatario
        type_key: Chiave tipo notifica (es: 'booking_confirmed', 'reminder_1h')
        title: Titolo notifica in-app
        message: Messaggio notifica in-app
        template_data: Dati per il template email (dict con variabili)
        related_booking_id: ID prenotazione correlata (opzionale)
        related_user_id: ID utente che ha generato la notifica (opzionale)
        action_url: URL di azione (opzionale)
    
    Returns:
        bool: True se almeno una notifica è stata inviata con successo
    """
    try:
        with Session(engine) as session:
            # Carica configurazione tipo notifica
            notif_type = session.exec(
                select(NotificationType).where(
                    NotificationType.type_key == type_key,
                    NotificationType.is_active == True
                )
            ).first()
            
            if not notif_type:
                logger.warning(f"Tipo notifica '{type_key}' non configurato o disattivato")
                return False
            
            # Carica dati utente destinatario
            user = session.get(User, user_id)
            if not user or not user.email:
                logger.error(f"Utente {user_id} non trovato o senza email")
                return False
            
            success = False
            
            # 1. Notifica in-app (nel database)
            if notif_type.in_app:
                notification = Notification(
                    user_id=user_id,
                    type=type_key,
                    title=title,
                    message=message,
                    related_booking_id=related_booking_id,
                    related_user_id=related_user_id,
                    action_url=action_url,
                    is_read=False
                )
                session.add(notification)
                session.commit()
                logger.info(f"✅ Notifica in-app creata per user {user_id}: {title}")
                success = True

                # Stessa notifica, ma sul telefono anche col sito chiuso.
                # Si aggancia qui e non a ogni singolo punto del codice: cosi'
                # ogni notifica nuova arriva anche in push senza ricordarselo.
                try:
                    from app.utils import notifiche_push
                    notifiche_push.invia(
                        user_id=user_id,
                        titolo=title,
                        testo=message,
                        url=action_url or "/",
                        tag=type_key,
                    )
                except Exception as errore:
                    # Una push che non parte non deve far fallire la notifica
                    logger.warning(f"⚠️ Push non inviata a {user_id}: {errore}")
            
            # 2. Email (se configurata)
            if notif_type.send_email and notif_type.email_subject and notif_type.email_template:
                if not template_data:
                    template_data = {}
                
                # Aggiungi dati base al template
                if 'user_name' not in template_data:
                    template_data['user_name'] = user.nome or user.email.split('@')[0]
                if 'action_url' not in template_data and action_url:
                    template_data['action_url'] = action_url
                
                email_sent = send_notification_email(
                    to_email=user.email,
                    to_name=user.nome or user.email.split('@')[0],
                    subject=notif_type.email_subject,
                    template_name=notif_type.email_template,
                    template_data=template_data
                )
                
                if email_sent:
                    logger.info(f"✅ Email notifica inviata a {user.email}")
                    success = True
            
            return success
            
    except Exception as e:
        logger.error(f"❌ Errore nell'invio notifica: {e}")
        return False
