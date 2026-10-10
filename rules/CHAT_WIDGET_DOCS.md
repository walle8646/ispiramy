# Chat Widget - Messaggistica Dinamica

## Funzionalità Implementate

### 1. **Widget Chat in Basso a Destra**
- Bottone fisso in basso a destra con icona messaggio
- Badge con numero di messaggi non letti (aggiornato in real-time)
- Animazione pulse quando arriva un nuovo messaggio
- Si apre/chiude con animazione smooth

### 2. **Notifiche Toast in Tempo Reale** ⭐ NEW
- Toast notification che appare sopra il bottone chat
- Mostra: avatar, nome mittente, timestamp, anteprima messaggio
- Click sulla notifica apre la chat con quell'utente
- Auto-hide dopo 5 secondi o click su X
- Suono di notifica (se consentito dal browser)
- Non appare se la chat con quell'utente è già aperta
- Polling ogni 5 secondi per nuovi messaggi

### 3. **Finestra Chat Dinamica**
- Dimensioni: 380x550px (responsive su mobile)
- Header con avatar, nome utente e pulsanti minimize/close
- Area messaggi scrollabile con design moderno
- Input area con textarea auto-resizing e bottone invio

### 4. **Integrazione Globale**
Incluso automaticamente in `base.html` per tutti gli utenti autenticati.

### 5. **Link Sostituiti**
Tutti i link `href="/messaggi/{id}"` sono stati sostituiti con `openChat()`:

- ✅ `user_profile.html` - Bottone "Invia Messaggio"
- ✅ `community.html` - Bottone "Messaggia" sotto le domande
- ✅ `messages_inbox.html` - Click su conversazione apre il widget

### 6. **Funzionalità Chat**
- **Invio messaggi**: Textarea con invio tramite pulsante o Enter (Shift+Enter per nuova riga)
- **Polling automatico**: Aggiornamento messaggi ogni 3 secondi quando la chat è aperta
- **Polling notifiche**: Controllo nuovi messaggi ogni 5 secondi (globale)
- **Badge non letti**: Aggiornamento contatore in real-time
- **Scroll automatico**: Scroll al bottom quando arrivano nuovi messaggi
- **Escape HTML**: Prevenzione XSS con escape automatico dei messaggi
- **Timestamp intelligenti**: "Ora", "5m fa", "2g fa", o data completa
- **Notifiche sonore**: Suono discreto all'arrivo di nuovi messaggi

### 7. **Stati Chat**
- **Chiuso**: Solo bottone visibile
- **Minimizzato**: Torna al bottone, mantiene conversazione attiva
- **Aperto**: Finestra completa con messaggi e input
- **Pulse**: Animazione di pulsazione quando ci sono notifiche non lette

## Come Funziona

### Sistema di Notifiche
1. **Polling Background**: Controlla `/api/conversations` ogni 5 secondi
2. **Rilevamento Nuovi Messaggi**: Confronta `message.id` con `lastKnownMessageId`
3. **Filtro Mittente**: Mostra notifica solo se `is_sender: false`
4. **Evita Duplicati**: Non notifica se la chat è già aperta con quell'utente
5. **Toast Animato**: SlideIn da destra con auto-hide dopo 5 secondi

### Aprire una Chat
```javascript
openChat(userId, userName, userAvatar)
```

**Esempio:**
```html
<button onclick="openChat(11, 'Mario Rossi', '/static/uploads/profile_pictures/mario.jpg')">
    💬 Invia Messaggio
</button>
```

### API Utilizzate
- `GET /api/messaggi/{user_id}` - Carica messaggi
- `POST /api/messaggi/{user_id}` - Invia messaggio
- `GET /api/conversations` - Carica conversazioni e contatore non letti (usato per notifiche)

### CSS Personalizzabile
Tutti gli stili sono inline nel file `chat_widget.html`. Colori principali:
- **Gradiente primario**: `#667eea` → `#764ba2`
- **Background messaggi ricevuti**: Bianco
- **Background messaggi inviati**: Gradiente viola
- **Background finestra**: `#f7fafc`
- **Badge non letti**: `#ef4444` (rosso)
- **Notification toast**: Bianco con shadow

## File Modificati

### File Aggiornati
- `app/templates/chat_widget.html` - Aggiunto sistema notifiche toast

### Nuove Funzionalità nel Widget
- `showNotification()` - Mostra toast notification
- `hideNotification()` - Nasconde toast
- `pollNewMessages()` - Polling per nuovi messaggi da tutte le conversazioni
- `startNotificationPolling()` - Avvia polling notifiche
- `stopNotificationPolling()` - Ferma polling notifiche
- Animazione CSS `pulse` per bottone chat
- Audio notification (Base64 embedded)

## Design

### Toast Notification
- **Posizione**: Bottom right, 100px da fondo (sopra chat button)
- **Dimensioni**: 320px width, altezza auto
- **Animazione**: slideInRight (da destra)
- **Elementi**: Avatar 48x48px, nome, timestamp, anteprima messaggio (2 righe max)
- **Interazione**: Click apre chat, X chiude, auto-hide 5 sec

### Stile Facebook Messenger
- Bottone circolare viola con gradiente
- Finestra con bordi arrotondati e shadow
- Animazione slideUp all'apertura
- Badge rosso per notifiche non lette
- Bubble messages con allineamento smart
- Pulse animation quando ci sono notifiche

### Responsive
- Desktop: 380x550px chat, 320px notification
- Mobile: Occupa quasi tutto lo schermo (calc(100vw - 40px))

## Performance

### Polling Strategy
- **Notifiche globali**: 5 secondi (solo quando pagina visibile)
- **Messaggi chat aperta**: 3 secondi (solo quando chat è aperta)
- **Badge non letti**: 30 secondi (aggiornamento periodico)
- **Cleanup**: Stop polling su `beforeunload`

### Ottimizzazioni
- Tracking `lastKnownMessageId` per evitare notifiche duplicate
- Notifiche disabilitate se chat già aperta con mittente
- Audio notification fallback silenzioso se autoplay bloccato
- Escape HTML per prevenire XSS

## Limitazioni Attuali
- ✅ Solo conversazioni 1-on-1 (no gruppi)
- ✅ Polling ogni 5 secondi (no WebSocket real-time)
- ✅ Max 60 messaggi per conversazione (configurabile in `messages.py`, chiave `MAX_MESSAGES_PER_CONVERSATION`)
- ✅ Notifiche solo quando pagina è aperta (no Service Worker)
- ✅ Suono notification semplice (non personalizzabile)

## Possibili Miglioramenti Futuri
- [ ] WebSocket per messaggi real-time (zero latency)
- [ ] Service Worker per notifiche push anche con tab chiusa
- [ ] Notifiche browser native (Push API)
- [ ] Typing indicator ("sta scrivendo...")
- [ ] Invio file/immagini
- [ ] Ricerca messaggi
- [ ] Lista conversazioni nel widget
- [ ] Emoji picker
- [ ] Reazioni ai messaggi
- [ ] Conferma lettura (doppia spunta blu)
- [ ] Suoni notification personalizzabili
- [ ] Vibrazione su mobile
- [ ] Dark mode
