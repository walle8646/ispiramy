/* Service worker di Ispiramy.
 *
 * Fa due cose sole, di proposito:
 *  - tiene in cache il guscio (icone, logo, pagina "senza rete"), cosi' l'app
 *    installata apre subito invece di restare bianca finche' la rete risponde;
 *  - quando la rete manca mostra una pagina decente al posto del dinosauro.
 *
 * Le pagine HTML NON finiscono mai in cache: contengono dati di chi ha fatto
 * l'accesso (profilo, messaggi, prenotazioni) e la cache e' condivisa fra tutti
 * gli account che usano quel telefono. Meglio una pagina in meno che il profilo
 * di qualcun altro.
 */
const VERSIONE = 'ispiramy-v1';
const SENZA_RETE = '/senza-rete';

/* Il guscio: roba pubblica, senza dati di nessuno. */
const GUSCIO = [
    SENZA_RETE,
    '/static/icone/icona-192.png',
    '/static/icone/icona-512.png',
    '/static/logo-final.svg',
];

self.addEventListener('install', (evento) => {
    evento.waitUntil(
        caches.open(VERSIONE)
            .then((cache) => cache.addAll(GUSCIO))
            /* Se un file del guscio non c'e', l'installazione non deve fallire:
               il sito continua a funzionare comunque dalla rete. */
            .catch(() => undefined)
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (evento) => {
    evento.waitUntil(
        caches.keys()
            .then((nomi) => Promise.all(
                nomi.filter((n) => n !== VERSIONE).map((n) => caches.delete(n))
            ))
            .then(() => self.clients.claim())
    );
});

/* I file statici hanno gia' la data di modifica in coda all'indirizzo
   (statico() in main.py): un indirizzo nuovo significa file nuovo, quindi
   possiamo servirli dalla cache senza rischiare di mostrare roba vecchia. */
function eStatico(url) {
    return url.pathname.startsWith('/static/') || url.pathname.startsWith('/uploads/');
}

function eNavigazione(richiesta) {
    return richiesta.mode === 'navigate';
}

self.addEventListener('fetch', (evento) => {
    const richiesta = evento.request;
    const url = new URL(richiesta.url);

    /* Solo letture del nostro sito: pagamenti, login e chiamate restano
       affari fra il browser e il server, senza niente in mezzo. */
    if (richiesta.method !== 'GET' || url.origin !== self.location.origin) {
        return;
    }
    if (url.pathname.startsWith('/api/')) {
        return;
    }

    if (eStatico(url)) {
        evento.respondWith(
            caches.match(richiesta).then((salvato) => salvato || fetch(richiesta).then((risposta) => {
                if (risposta.ok) {
                    const copia = risposta.clone();
                    caches.open(VERSIONE).then((cache) => cache.put(richiesta, copia));
                }
                return risposta;
            }))
        );
        return;
    }

    if (eNavigazione(richiesta)) {
        evento.respondWith(
            fetch(richiesta).catch(() => caches.match(SENZA_RETE))
        );
    }
});
