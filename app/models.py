from sqlmodel import Field, SQLModel, Relationship
from datetime import datetime, time
from typing import Optional, List
from decimal import Decimal
from enum import Enum

class ImageLink(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    url: str
    name: str = Field(default="")
    description: str = Field(default="")

class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    slug: str = Field(unique=True, index=True)
    icon: Optional[str] = Field(default="🎯")
    description: Optional[str] = Field(default=None)
    target: Optional[str] = Field(default=None)
    color: Optional[str] = Field(default="#4CAF50")
    is_principal: bool = Field(default=False, index=True)  # ✅ True se è categoria principale

class CategoryHierarchy(SQLModel, table=True):
    """Relazione gerarchica molti-a-molti tra categorie (principale e sottocategorie)"""
    __tablename__ = "category_hierarchy"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    parent_category_id: int = Field(foreign_key="category.id", index=True)
    child_category_id: int = Field(foreign_key="category.id", index=True)
    position: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class User(SQLModel, table=True):
    """Modello utente/consulente"""
    __tablename__ = "user"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_md5: str
    nome: Optional[str] = None
    cognome: Optional[str] = None
    professione: Optional[str] = None
    
    # Relazione con categoria
    category_id: Optional[int] = Field(default=None, foreign_key="category.id", index=True)
    selected_subcategories: Optional[str] = Field(default=None)  # JSON array di IDs: '["9", "10"]'
    
    # Profilo consulente
    profile_picture: Optional[str] = None
    prezzo_consulenza: Optional[int] = None
    consulenze_vendute: int = Field(default=0)
    consulenze_acquistate: int = Field(default=0)
    descrizione: Optional[str] = None
    aree_interesse: Optional[str] = None
    tags: Optional[str] = None  # JSON array di tag generati da AI per la ricerca
    
    # Status
    confirmed: int = Field(default=0)
    confirmation_code: Optional[str] = None
    # Quando è stato generato il codice: l'email dice "valido 15 minuti",
    # ma senza questa data il codice valeva per sempre.
    confirmation_code_created_at: Optional[datetime] = Field(default=None)
    is_verified: bool = Field(default=False, index=True)  # filtro principale della ricerca consulenti
    is_anonymous: bool = Field(default=False)  # Se True, mostra "Utente #ID" invece del nome
    genere: Optional[str] = Field(default=None)  # M=Maschio, F=Femmina, None=Non specificato
    notify_category_requests: bool = Field(default=True)  # 🔔 Ricevi notifiche per richieste in categoria
    # Conferma automatica delle prenotazioni dirette. Se False il consulente
    # riceve una richiesta da accettare o rifiutare: il pagamento del cliente
    # viene solo autorizzato e incassato all'accettazione.
    auto_accept_bookings: bool = Field(default=True)
    user_type_id: int = Field(default=1)  # 1=Utente, 2=Verificatore, 3=Amministratore
    languages: Optional[str] = Field(default=None)  # JSON array: '["it","en","fr"]'
    
    # Stripe Connect
    stripe_account_id: Optional[str] = Field(default=None)  # Stripe Connected Account ID (acct_xxx)
    stripe_onboarding_complete: bool = Field(default=False)  # Onboarding Stripe completato
    platform_fee_percent: int = Field(default=20)  # Commissione piattaforma % (default 20%)
    
    # PayPal
    paypal_email: Optional[str] = Field(default=None)  # Email PayPal del consulente per ricevere pagamenti
    
    # Google OAuth
    google_id: Optional[str] = Field(default=None, index=True)  # Google sub ID per login OAuth
    
    # Timestamps
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow)
    last_seen: Optional[datetime] = Field(default=None)

class Consultation(SQLModel, table=True):
    """Prenotazione consulenza"""
    __tablename__ = "consultation"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    consultant_id: int = Field(foreign_key="user.id")
    status: str = Field(default="pending")
    description: Optional[str] = Field(default=None, max_length=2000)  # ✅ Dettagli della consulenza richiesta dal cliente
    scheduled_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

# ========== MESSAGGISTICA MODELS ==========

class Conversation(SQLModel, table=True):
    """Conversazioni tra utenti (normalizzate: user1_id < user2_id)"""
    __tablename__ = "conversations"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    user1_id: int = Field(foreign_key="user.id", index=True)
    user2_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    # ✅ Relationship con Message
    messages: List["Message"] = Relationship(back_populates="conversation")

class Message(SQLModel, table=True):
    """Messaggi nelle conversazioni"""
    __tablename__ = "messages"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversations.id", index=True)
    sender_id: int = Field(foreign_key="user.id", index=True)
    content: str = Field(max_length=2000)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_read: bool = Field(default=False)
    
    # System messages and consultation offers
    is_system_message: bool = Field(default=False)
    consultation_offer_id: Optional[int] = Field(default=None, foreign_key="consultation_offers.id")
    
    # ✅ Relationship con Conversation
    conversation: Optional[Conversation] = Relationship(back_populates="messages")

# ========== COMMUNITY Q&A MODELS ==========

class QuestionStatus(str, Enum):
    """Status delle domande nella community"""
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"

class CommunityQuestion(SQLModel, table=True):
    """Domande nella community Q&A - solo domande con upvotes (like), senza risposte"""
    __tablename__ = "community_questions"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    primary_category_id: Optional[int] = Field(default=None, foreign_key="category.id", index=True)  # ✅ Categoria principale
    category_id: Optional[int] = Field(default=None, foreign_key="category.id", index=True)  # ✅ Sottocategoria
    title: str = Field(max_length=200)
    description: str = Field(max_length=5000)
    status: str = Field(default=QuestionStatus.OPEN)
    views: int = Field(default=0)  # Ora rappresenta quanti utenti UNICI hanno cliccato "Messaggia"
    upvotes: int = Field(default=0)
    validation: bool = Field(default=False, index=True)  # 🆕 Flag di validazione - se False, la domanda non è visibile
    images: Optional[str] = Field(default=None)  # JSON array di URL S3 delle immagini allegate (max 5)
    tags: Optional[str] = Field(default=None)  # JSON array di tag generati da AI per la ricerca
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CommunityLike(SQLModel, table=True):
    """Like degli utenti sulle domande della community - un utente può mettere un solo like per domanda"""
    __tablename__ = "community_likes"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="community_questions.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        # Constraint univoco: un utente può mettere un solo like per domanda
        table_args = (
            {'sqlite_autoincrement': True},
        )


class CommunityContact(SQLModel, table=True):
    """Traccia quali utenti hanno contattato (cliccato Messaggia) l'autore di una domanda"""
    __tablename__ = "community_contacts"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="community_questions.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)  # Chi ha cliccato Messaggia
    message_sent: bool = Field(default=False)  # True quando il consulente invia effettivamente un messaggio
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        # Constraint univoco: un utente può incrementare il contatore una sola volta
        table_args = (
            {'sqlite_autoincrement': True},
        )


class CommunityQuestionFollow(SQLModel, table=True):
    """Traccia quali utenti sono interessati a una domanda della community (Segui e Richiedi)"""
    __tablename__ = "community_question_follows"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    question_id: int = Field(foreign_key="community_questions.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    class Config:
        # Constraint univoco: un utente può seguire una domanda solo una volta
        table_args = (
            {'sqlite_autoincrement': True},
        )


# ========== AVAILABILITY SYSTEM ==========

class AvailabilityBlock(SQLModel, table=True):
    """Blocchi di disponibilità per consulenze"""
    __tablename__ = "availability_block"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    date: datetime = Field(index=True)  # Data del giorno
    start_time: time  # Oggetto time
    end_time: time    # Oggetto time
    total_minutes: int  # Durata in minuti
    booked_minutes: int = Field(default=0)  # Minuti già prenotati
    status: str = Field(default="available")  # available, booked, unavailable
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class Booking(SQLModel, table=True):
    """Prenotazioni di consulenze tra clienti e consulenti"""
    __tablename__ = "booking"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    client_user_id: int = Field(foreign_key="user.id", index=True)
    consultant_user_id: int = Field(foreign_key="user.id", index=True)
    availability_block_id: Optional[int] = Field(default=None, foreign_key="availability_block.id")
    
    booking_date: datetime = Field(index=True)
    start_time: str  # Formato "HH:MM"
    end_time: str    # Formato "HH:MM"
    duration_minutes: int  # 60, 90, 120 (minimo 1 ora)
    
    status: str = Field(default="pending")  # pending_payment, awaiting_acceptance, confirmed, completed, cancelled, no_show
    price: Optional[Decimal] = None
    payment_status: str = Field(default="pending")  # pending, authorized, held, paid, released, refunded, partially_refunded, voided, failed
    payment_method: Optional[str] = None
    transaction_id: Optional[str] = None
    
    # Stripe payment fields
    stripe_checkout_session_id: Optional[str] = None  # Stripe Checkout Session ID
    stripe_payment_intent_id: Optional[str] = None  # Stripe Payment Intent ID
    
    # PayPal payment fields
    paypal_order_id: Optional[str] = None  # PayPal Order ID
    paypal_capture_id: Optional[str] = None  # PayPal Capture ID (dopo cattura pagamento)
    paypal_authorization_id: Optional[str] = None  # PayPal Authorization ID (richiesta da accettare)

    # Richiesta da accettare: entro quando il consulente deve rispondere (ora
    # italiana, senza fuso). Valorizzata solo se il consulente conferma a mano.
    acceptance_deadline: Optional[datetime] = None
    paypal_payout_id: Optional[str] = None  # PayPal Payout Batch ID (dopo rilascio fondi)
    
    # Transfer differito (48h hold)
    stripe_transfer_id: Optional[str] = None  # Stripe Transfer ID (dopo rilascio fondi)
    payment_held_until: Optional[datetime] = None  # Quando scade il hold (48h dopo fine consulenza)
    payment_released_at: Optional[datetime] = None  # Quando i fondi sono stati trasferiti
    refund_amount: Optional[Decimal] = None  # Importo rimborsato (può essere parziale)
    
    meeting_link: Optional[str] = None  # Link Zoom/Google Meet
    client_notes: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=2000)  # 🆕 Descrizione della consulenza richiesta dal cliente
    community_question_id: Optional[int] = Field(default=None, foreign_key="community_questions.id", index=True)  # Domanda community associata
    consultant_notes: Optional[str] = None
    
    # Join tracking - quando client/consultant cliccano "Partecipa"
    client_joined_at: Optional[datetime] = None
    consultant_joined_at: Optional[datetime] = None
    
    # Call tracking - quando la call è stata avviata (per riprenderla se disconnesso)
    call_started_at: Optional[datetime] = None
    
    # Recording tracking - registrazione video call
    recording_sid: Optional[str] = None  # Agora Cloud Recording SID
    recording_resource_id: Optional[str] = None  # Agora resource ID
    recording_status: str = Field(default="not_started")  # not_started, recording, processing, completed, failed
    recording_url: Optional[str] = None  # S3 URL del video
    recording_duration: Optional[int] = None  # Durata in secondi
    recording_file_size: Optional[int] = None  # Dimensione file in bytes
    recording_filename: Optional[str] = None  # Nome file: booking_123_20251121_210446
    recording_started_at: Optional[datetime] = None
    recording_completed_at: Optional[datetime] = None
    recording_session_count: int = Field(default=1)  # Traccia il numero di sessioni di registrazione (per rejoin)
    
    cancellation_reason: Optional[str] = None
    cancelled_by: Optional[int] = Field(default=None, foreign_key="user.id")
    cancelled_at: Optional[datetime] = None
    
    # Recording preference - se il cliente vuole essere registrato
    recording_requested: bool = Field(default=True)  # True = registra, False = non registrare

    # Token del link "lascia una recensione" inviato via email. Va salvato qui
    # alla generazione: è l'unica cosa che autorizza a recensire senza login.
    review_token: Optional[str] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CallMessage(SQLModel, table=True):
    """Messaggi durante la call (chat video call)"""
    __tablename__ = "call_messages"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    booking_id: int = Field(foreign_key="booking.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    message: str
    attachments: Optional[str] = Field(default=None)  # JSON string con lista di allegati {filename, file_path, file_size, file_type}
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class CategoryRequestNotification(SQLModel, table=True):
    """Traccia le notifiche di nuove richieste nella categoria per i consulenti"""
    __tablename__ = "category_request_notifications"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    consultant_user_id: int = Field(foreign_key="user.id", index=True)  # Consulente notificato
    question_id: int = Field(foreign_key="community_questions.id", index=True)  # La richiesta
    is_read: bool = Field(default=False, index=True)  # Se il consulente l'ha letta
    created_at: datetime = Field(default_factory=datetime.utcnow)



class ConsultationOffer(SQLModel, table=True):
    __tablename__ = "consultation_offers"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    consultant_user_id: int = Field(foreign_key="user.id")
    client_user_id: int = Field(foreign_key="user.id")
    
    price: float = Field(gt=0)
    duration_minutes: int = Field(gt=0)
    
    status: str = Field(default="pending")  # pending, accepted, rejected, expired, completed
    booking_id: Optional[int] = Field(default=None, foreign_key="booking.id")
    
    message: Optional[str] = None
    expires_at: datetime
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Notification(SQLModel, table=True):
    """Modello per le notifiche utente"""
    __tablename__ = "notifications"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")  # Destinatario della notifica
    
    type: str  # 'booking', 'message', 'payment', 'cancellation', 'offer', etc.
    title: str  # Titolo breve della notifica
    message: str  # Messaggio completo
    
    # Riferimenti opzionali
    related_booking_id: Optional[int] = Field(default=None, foreign_key="booking.id")
    related_user_id: Optional[int] = Field(default=None, foreign_key="user.id")  # Chi ha generato la notifica
    
    # Link di azione
    action_url: Optional[str] = None  # URL dove andare cliccando la notifica
    
    is_read: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationType(SQLModel, table=True):
    """
    Configurazione tipi di notifica con flag per in-app e email.
    
    Permette di configurare quali notifiche inviare e come:
    - in_app: Mostra la notifica nel dropdown campanella
    - send_email: Invia anche email all'utente
    - email_subject: Oggetto dell'email
    - email_template: Nome del template HTML da usare
    """
    __tablename__ = "notification_types"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    type_key: str = Field(unique=True, index=True)  # es: 'booking_confirmed', 'reminder_1h', 'reminder_10min'
    name: str  # Nome descrittivo in italiano
    description: Optional[str] = None
    
    # Flags configurazione
    in_app: bool = Field(default=True)  # Mostra notifica in-app
    send_email: bool = Field(default=False)  # Invia anche email
    
    # Configurazione email
    email_subject: Optional[str] = None  # Oggetto email (può contenere {variables})
    email_template: Optional[str] = None  # Nome template HTML (es: 'booking_confirmation.html')
    
    # Metadata
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class ConfigurationProperty(SQLModel, table=True):
    """Configurazione globale dell'applicazione"""
    __tablename__ = "configuration_property"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    property_key: str = Field(alias="key", unique=True, index=True)
    property_value: str = Field(alias="value")
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Review(SQLModel, table=True):
    """Recensioni post-consulenza lasciate dai clienti ai consulenti"""
    __tablename__ = "reviews"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    booking_id: int = Field(foreign_key="booking.id", index=True)
    reviewer_user_id: int = Field(foreign_key="user.id", index=True)       # Il cliente
    consultant_user_id: int = Field(foreign_key="user.id", index=True)     # Il consulente
    
    rating_helpful: int       # 1-5: "Ti ha aiutato concretamente?"
    rating_prepared: int      # 1-5: "Era preparato sull'argomento?"
    rating_communication: int # 1-5: "Capacità comunicativa?"
    comment: Optional[str] = Field(default=None, max_length=2000)
    
    review_token: Optional[str] = Field(default=None, unique=True, index=True)  # Token per link email
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Dispute(SQLModel, table=True):
    """Contestazioni aperte dai clienti su una consulenza"""
    __tablename__ = "disputes"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    booking_id: int = Field(foreign_key="booking.id", index=True)
    client_user_id: int = Field(foreign_key="user.id", index=True)
    consultant_user_id: int = Field(foreign_key="user.id", index=True)
    
    description: str = Field(max_length=5000)  # Descrizione del problema
    status: str = Field(default="open")  # open, in_review, resolved, rejected
    
    # Analisi AI della contestazione
    ai_verdict: Optional[str] = Field(default=None)  # justified, unjustified, uncertain
    ai_confidence: Optional[int] = Field(default=None)  # 0-100
    ai_comment: Optional[str] = Field(default=None, max_length=5000)
    ai_analyzed_at: Optional[datetime] = Field(default=None)
    
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = Field(default=None)


class DisputeMessage(SQLModel, table=True):
    """Messaggi nello scambio di una contestazione"""
    __tablename__ = "dispute_messages"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    dispute_id: int = Field(foreign_key="disputes.id", index=True)
    sender_user_id: Optional[int] = Field(default=None, foreign_key="user.id")  # None = messaggio admin/sistema
    is_admin: bool = Field(default=False)
    message: str = Field(max_length=5000)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FavoriteConsultant(SQLModel, table=True):
    """Consulenti preferiti salvati dagli utenti"""
    __tablename__ = "favorite_consultants"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    consultant_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SocialContent(SQLModel, table=True):
    """Un contenuto social: l'idea nata da una domanda della community.

    Tiene il media (generato una volta sola) e i testi. Le uscite sui singoli
    social sono le righe di SocialDraft che puntano qui: lo stesso video va su
    TikTok, sui Reels di Instagram e su quelli di Facebook, ognuno con il suo
    orario e il suo testo, senza rigenerare niente.
    """
    __tablename__ = "social_contents"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Provenienza (domanda community da cui è nata l'idea)
    source_question_id: Optional[int] = Field(default=None, index=True)
    source_title: Optional[str] = Field(default=None, max_length=500)

    # 'immagini' | 'video_slide' | 'video_completo': decide come si genera il media
    content_kind: str = Field(default="immagini", max_length=20, index=True)

    caption_base: str = Field(default="", max_length=5000)  # testo di partenza
    captions: Optional[str] = Field(default=None, max_length=8000)  # JSON: una versione per social
    media_urls: Optional[str] = Field(default=None, max_length=3000)  # URL pubblici, uno per riga
    extra_content: Optional[str] = Field(default=None, max_length=8000)  # hook, script, slide, scene

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SocialDraft(SQLModel, table=True):
    """Bozze di post social generate dall'AI, in attesa di approvazione e pubblicazione.

    Workflow status: draft -> approved -> publishing -> published | failed
                     (oppure draft -> rejected)
    La pubblicazione avviene via Post for Me (postforme.dev).
    """
    __tablename__ = "social_drafts"

    id: Optional[int] = Field(default=None, primary_key=True)
    # Il contenuto di cui questa è un'uscita. Le righe più vecchie di questa
    # struttura ne ricevono uno all'avvio (una per bozza). Nessun vincolo di
    # chiave esterna: la colonna viene aggiunta a tabelle già esistenti, e su
    # SQLite un vincolo aggiunto a posteriori non si potrebbe più togliere.
    content_id: Optional[int] = Field(default=None, index=True)
    platform: str = Field(index=True)  # 'facebook' | 'instagram' | 'tiktok'
    caption: str = Field(max_length=5000)  # testo pronto da pubblicare (hashtag inclusi)
    media_urls: Optional[str] = Field(default=None, max_length=3000)  # URL pubblici, uno per riga (IG richiede >=1 immagine, TikTok >=1 video)
    # Che contenuto e': 'immagini' (foto o carosello), 'video_slide' (video
    # fatto con le nostre slide) o 'video_completo' (video con le clip di
    # repertorio). Decide come si genera il media e in quale scheda dell'admin
    # compare la bozza. Vuoto = quello di partenza della piattaforma.
    content_kind: Optional[str] = Field(default=None, max_length=20, index=True)

    # Provenienza (domanda community da cui è stato generato)
    source_question_id: Optional[int] = Field(default=None, index=True)
    source_title: Optional[str] = Field(default=None, max_length=500)
    extra_content: Optional[str] = Field(default=None, max_length=8000)  # JSON: hook, carousel_slides, script... per la fase grafica

    status: str = Field(default="draft", index=True)
    scheduled_at: Optional[datetime] = Field(default=None)  # ora italiana naive; None = solo pubblicazione manuale

    # Tracking Post for Me
    postforme_post_id: Optional[str] = Field(default=None, index=True)
    # Numero di ritentativi espliciti dopo un fallimento sul social. Entra
    # nell'external_id: senza, un post rifiutato dalla piattaforma veniva
    # riconosciuto come "già inviato" e non poteva più essere ripubblicato.
    publish_attempt: int = Field(default=0)
    published_url: Optional[str] = Field(default=None, max_length=1000)
    error: Optional[str] = Field(default=None, max_length=2000)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    published_at: Optional[datetime] = Field(default=None)
