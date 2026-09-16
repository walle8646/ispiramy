"""
Generatore di contenuti social a partire dalle domande della Community Q&A.

Per ogni domanda popolare genera, con una sola chiamata GPT, un pacchetto
multi-piattaforma pronto da pubblicare (Instagram, TikTok, LinkedIn).

Uso da CLI:
    python scripts/generate_social_content.py --limit 5
    python scripts/generate_social_content.py --limit 10 --out drafts.json
"""
import os
import json
from datetime import datetime
from typing import Optional

from sqlmodel import select, text
from openai import OpenAI

from app.database import get_session
from app.logger_config import logger


# Riutilizza lo stesso modello già usato nel progetto (economico e sufficiente)
MODEL = "gpt-4o-mini"

_client: Optional[OpenAI] = None


class GenerazioneFallita(Exception):
    """Nessuna bozza prodotta: il motivo va mostrato a chi ha premuto il pulsante."""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY non configurata.")
        _client = OpenAI(api_key=api_key)
    return _client


SYSTEM_PROMPT = """Sei un social media manager esperto per "Ispiramy", un marketplace italiano \
di consulenze professionali online (carriera, fiscale, legale, psicologia, tech, marketing, \
immobiliare, formazione). Gli utenti possono prenotare video-consulenze con esperti verificati.

Ti viene fornita una DOMANDA reale pubblicata dagli utenti nella community.
Il tuo compito è trasformarla in un pacchetto di contenuti social accattivanti che:
- catturino l'attenzione di chi ha lo STESSO problema
- diano un mini-valore concreto (così il post è utile di per sé)
- spingano a cercare un esperto su Ispiramy (senza essere troppo "venduti")

Tono: professionale ma friendly, italiano naturale, niente gergo da markettaro.
NON inventare dati, leggi o numeri specifici non verificabili. Resta sul generale e pratico.

Rispondi ESCLUSIVAMENTE con un oggetto JSON valido con questa struttura ESATTA:
{
  "instagram": {
    "hook": "prima riga forte, max 60 caratteri, per fermare lo scroll",
    "carousel_slides": ["testo slide 1 (il problema)", "slide 2", "slide 3", "slide 4", "slide finale con CTA"],
    "caption": "caption completa per il post, 2-4 frasi + emoji con misura",
    "hashtags": ["#hashtag1", "#hashtag2", "... 8-12 hashtag italiani pertinenti"]
  },
  "tiktok": {
    "hook": "prima frase parlata, fortissima, max 5 secondi di parlato",
    "script": "script di un video di 15-25 secondi, parlato naturale, con la struttura: problema -> due consigli rapidi -> CTA finale",
    "script_segments": ["lo stesso script diviso in 4-5 frasi brevissime, dalla prima (l'hook) alla CTA: una frase per scena del video, max ~70 caratteri ciascuna"],
    "scene_keywords": ["una voce per ogni frase di script_segments, nello stesso ordine: 2-4 parole IN INGLESE che descrivono l'immagine da mostrare dietro quella frase (es. 'stressed developer desk', 'woman planning calendar'). Solo persone, luoghi o gesti reali: niente marchi, niente scritte, niente schermate di software"],
    "caption": "caption breve per TikTok",
    "hashtags": ["#hashtag1", "... 5-8 hashtag"]
  },
  "linkedin": {
    "post": "post professionale 4-6 righe, taglio più ragionato e autorevole, con a-capo per leggibilità",
    "hashtags": ["#hashtag1", "... 3-5 hashtag professionali"]
  },
  "facebook": {
    "post": "post per la Pagina Facebook, 3-5 frasi, tono friendly e diretto, chiude con CTA verso ispiramy.com",
    "hashtags": ["#hashtag1", "... 3-5 hashtag"]
  },
  "cta": "call to action finale coerente, es: 'Trova l'esperto giusto su ispiramy.com'"
}

I caroselli devono avere 5 slide. Le slide devono essere brevi (max ~120 caratteri ciascuna).
Lo script TikTok viene letto da una voce sintetica: scrivi numeri, sigle e simboli come si pronunciano \
("tre consigli", non "3 consigli"; "per cento", non "%"), niente emoji e niente hashtag nello script.
Il video TikTok dura 15-25 secondi: in italiano sono circa 55 parole IN TUTTO, quindi taglia corto \
e togli ogni parola che non serve. scene_keywords deve avere esattamente un elemento per ogni frase di script_segments."""


def fetch_top_questions(limit: int = 5) -> list[dict]:
    """Pesca le domande community più popolari, escluse quelle già lavorate.

    L'esclusione va fatta QUI, non a valle: prima si chiamava GPT su tutte le
    top-N e solo dopo si scartavano quelle con bozze già esistenti. Siccome la
    classifica per engagement è stabile, dal secondo "Genera contenuti" in poi
    si pagavano N chiamate a GPT per ottenere zero bozze. Filtrando a monte,
    ogni esecuzione pesca domande nuove e scende progressivamente in classifica.
    """
    questions = []
    with get_session() as session:
        # upvotes + views come proxy di engagement; solo domande validate/visibili
        rows = session.exec(
            text(
                """
                SELECT q.id, q.title, q.description, q.upvotes, q.views,
                       c.name AS category_name
                FROM community_questions q
                LEFT JOIN category c ON c.id = COALESCE(q.primary_category_id, q.category_id)
                WHERE q.validation = true
                  AND NOT EXISTS (
                      SELECT 1 FROM social_drafts d WHERE d.source_question_id = q.id
                  )
                ORDER BY (q.upvotes * 3 + q.views) DESC, q.created_at DESC
                LIMIT :limit
                """
            ).bindparams(limit=limit)
        ).all()
        for r in rows:
            questions.append({
                "id": r[0],
                "title": r[1],
                "description": r[2] or "",
                "upvotes": r[3] or 0,
                "views": r[4] or 0,
                "category": r[5] or "Generale",
            })
    return questions


def generate_for_question(q: dict) -> dict:
    """Genera il pacchetto social multi-piattaforma per una singola domanda."""
    client = _get_client()
    user_content = (
        f"Categoria: {q['category']}\n"
        f"Titolo della domanda: {q['title']}\n"
        f"Descrizione: {q['description'][:1500]}"
    )
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.8,
        max_tokens=1500,
        response_format={"type": "json_object"},
    )
    raw = (response.choices[0].message.content or "").strip()
    try:
        pkg = json.loads(raw)
    except json.JSONDecodeError as e:
        # Prima si salvava una bozza vuota e sembrava tutto a posto
        raise GenerazioneFallita(f"risposta non leggibile dal modello ({e})") from e
    if not pkg:
        raise GenerazioneFallita("il modello ha risposto senza contenuti")

    return {
        "source_question_id": q["id"],
        "source_title": q["title"],
        "category": q["category"],
        "engagement": {"upvotes": q["upvotes"], "views": q["views"]},
        "generated_at": datetime.utcnow().isoformat(),
        "status": "draft",  # draft -> approved -> published
        "content": pkg,
    }


def genera_bozze(limit: int = 5) -> tuple[list[dict], list[str]]:
    """Genera le bozze e restituisce anche gli errori incontrati.

    Prima gli errori venivano solo scritti nei log: se fallivano tutte le
    generazioni (per esempio con la chiave OpenAI mancante) la pagina
    rispondeva "nessuna domanda nuova da lavorare", cioe' il contrario di
    quello che era successo.
    """
    questions = fetch_top_questions(limit)
    if not questions:
        logger.info("Nessuna domanda community nuova da trasformare in contenuti social.")
        return [], []

    drafts, errori = [], []
    for q in questions:
        logger.info(f"🎨 Genero contenuti per domanda #{q['id']}: {q['title'][:60]}...")
        try:
            drafts.append(generate_for_question(q))
        except Exception as e:
            logger.error(f"Errore generazione per domanda {q['id']}: {e}")
            errori.append(f"domanda #{q['id']}: {e}")
    return drafts, errori


def generate_batch(limit: int = 5) -> list[dict]:
    """Come genera_bozze, ma solleva se non e' uscita nemmeno una bozza."""
    drafts, errori = genera_bozze(limit)
    if not drafts and errori:
        raise GenerazioneFallita("; ".join(errori)[:400])
    return drafts


def _join_caption(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())[:5000]


def save_packages_as_drafts(packages: list[dict]) -> int:
    """Salva i pacchetti generati come SocialDraft nel DB (una riga per piattaforma).

    Ritorna il numero di draft creati. Salta le piattaforme senza contenuto e le
    domande che hanno già draft (per non duplicare a ogni rigenerazione).
    """
    from app.models import SocialDraft
    from sqlmodel import select as sm_select

    created = 0
    with get_session() as session:
        for pkg in packages:
            content = pkg.get("content") or {}
            qid = pkg.get("source_question_id")

            # Skip se esistono già draft per questa domanda
            if qid is not None:
                existing = session.exec(
                    sm_select(SocialDraft).where(SocialDraft.source_question_id == qid).limit(1)
                ).first()
                if existing:
                    logger.info(f"Draft già esistenti per domanda {qid}, skip.")
                    continue

            ig = content.get("instagram") or {}
            tk = content.get("tiktok") or {}
            fb = content.get("facebook") or {}
            cta = content.get("cta") or ""

            rows = []
            if fb.get("post"):
                rows.append(("facebook",
                             _join_caption(fb["post"], " ".join(fb.get("hashtags") or [])),
                             None))
            if ig.get("caption"):
                rows.append(("instagram",
                             _join_caption(ig["caption"], cta, " ".join(ig.get("hashtags") or [])),
                             json.dumps({"hook": ig.get("hook"), "carousel_slides": ig.get("carousel_slides")}, ensure_ascii=False)))
            if tk.get("caption") or tk.get("script"):
                rows.append(("tiktok",
                             _join_caption(tk.get("caption") or "", " ".join(tk.get("hashtags") or [])),
                             json.dumps({"hook": tk.get("hook"), "script": tk.get("script"),
                                         "script_segments": tk.get("script_segments"),
                                         "scene_keywords": tk.get("scene_keywords")}, ensure_ascii=False)))

            for platform, caption, extra in rows:
                session.add(SocialDraft(
                    platform=platform,
                    caption=caption,
                    source_question_id=qid,
                    source_title=(pkg.get("source_title") or "")[:500],
                    extra_content=extra,
                ))
                created += 1
        session.commit()
    logger.info(f"💾 Salvati {created} social draft nel DB.")
    return created
