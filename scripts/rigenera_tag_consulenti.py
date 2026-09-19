"""Rigenera i tag di ricerca dei consulenti.

    python scripts/rigenera_tag_consulenti.py --prova     # mostra e basta
    python scripts/rigenera_tag_consulenti.py             # scrive sul database
    python scripts/rigenera_tag_consulenti.py --utente 89 # uno solo

I tag li deduce l'AI dal profilo e finiscono nella ricerca. Col prompt vecchio
ci entrava di tutto: un consulente commerciale che nella bio aveva scritto
"amante dei viaggi, juventino e noliano doc" usciva cercando "viaggi
all'estero", "juventino" e "noliano". Il prompt ora esclude hobby, luoghi e
tratti personali, ma i tag gia' salvati restano quelli di prima: questo
script li rifa'.

Serve OPENAI_API_KEY e il DATABASE_URL del database da sistemare. Costa una
chiamata a gpt-4o-mini per consulente, quindi pochi centesimi in tutto.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))


async def rigenera(prova: bool, solo_utente: int = None) -> None:
    from dotenv import load_dotenv
    load_dotenv(RADICE / ".env")

    from sqlmodel import Session, select

    from app.database import engine
    from app.models import User
    from app.utils.ai_service import genera_tags

    with Session(engine) as sessione:
        query = select(User).where(User.user_type_id >= 2)
        if solo_utente:
            query = select(User).where(User.id == solo_utente)
        consulenti = sessione.exec(query).all()

        print(f"{len(consulenti)} consulenti da guardare\n")
        cambiati = 0

        for utente in consulenti:
            if not (utente.descrizione or utente.aree_interesse or utente.professione):
                continue

            try:
                nuovi = await genera_tags(
                    utente.descrizione or "",
                    utente.aree_interesse or None,
                    utente.professione or None,
                )
            except Exception as errore:
                print(f"  {utente.id} {utente.nome}: non riuscito ({errore})")
                continue

            vecchi = utente.tags or ""
            nuovi_testo = json.dumps(nuovi, ensure_ascii=False)
            if nuovi_testo == vecchi:
                continue

            print(f"  {utente.id} {utente.nome} {utente.cognome or ''}")
            print(f"     prima: {vecchi[:120]}")
            print(f"     dopo:  {nuovi_testo[:120]}")
            cambiati += 1

            if not prova:
                utente.tags = nuovi_testo
                sessione.add(utente)

        if prova:
            print(f"\nProva: {cambiati} profili cambierebbero. Rilancia senza --prova per scrivere.")
        else:
            sessione.commit()
            print(f"\nFatto: {cambiati} profili aggiornati.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prova", action="store_true", help="mostra i cambiamenti senza scriverli")
    parser.add_argument("--utente", type=int, default=None, help="rigenera solo questo id")
    argomenti = parser.parse_args()
    asyncio.run(rigenera(argomenti.prova, argomenti.utente))
