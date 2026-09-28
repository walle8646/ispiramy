"""Mostra le notifiche di un utente, per capire quale si ripete.

    python scripts/notifiche_di_un_utente.py paolo
    python scripts/notifiche_di_un_utente.py paolo@example.com --quante 100
    python scripts/notifiche_di_un_utente.py paolo --pulisci

Si cerca per pezzo di email, nome o cognome. Le notifiche vengono raggruppate
per tipo e testo: una riga con "x37" e' una notifica che e' stata rimandata
trentasette volte, ed e' quello che si sta cercando.

Con --pulisci cancella i doppioni (stesso tipo e stesso testo) tenendo il piu'
recente. Senza, non tocca niente.

Da lanciare dalla Shell di Render, dove il DATABASE_URL e' gia' quello giusto.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))


def cerca_utenti(sessione, pezzo):
    from sqlmodel import or_, select

    from app.models import User

    like = f"%{pezzo}%"
    return sessione.exec(
        select(User).where(or_(User.email.ilike(like),
                               User.nome.ilike(like),
                               User.cognome.ilike(like)))
    ).all()


def mostra(utente, sessione, quante, pulisci):
    from sqlmodel import select

    from app.models import CategoryRequestNotification, Notification

    nome = f"{utente.nome or ''} {utente.cognome or ''}".strip() or "(senza nome)"
    print(f"\n=== {nome} <{utente.email}> id={utente.id} ===")

    notifiche = sessione.exec(
        select(Notification)
        .where(Notification.user_id == utente.id)
        .order_by(Notification.created_at.desc())
    ).all()
    print(f"notifiche nella campanella: {len(notifiche)}")

    gruppi = defaultdict(list)
    for notifica in notifiche:
        gruppi[(notifica.type, notifica.message)].append(notifica)

    print("\nraggruppate (le piu' ripetute in cima):")
    for (tipo, messaggio), righe in sorted(gruppi.items(),
                                           key=lambda voce: -len(voce[1])):
        date = [r.created_at for r in righe if r.created_at]
        periodo = ""
        if date:
            periodo = f" | dal {min(date):%d/%m/%Y %H:%M} al {max(date):%d/%m/%Y %H:%M}"
        da_leggere = sum(1 for r in righe if not r.is_read)
        print(f"  x{len(righe):<4} [{tipo}] {messaggio[:70]}"
              f" (da leggere: {da_leggere}){periodo}")

    print(f"\nultime {quante}, in ordine di arrivo:")
    for notifica in notifiche[:quante]:
        quando = f"{notifica.created_at:%d/%m/%Y %H:%M}" if notifica.created_at else "?"
        print(f"  {quando}  [{notifica.type}] {notifica.title}: {notifica.message[:70]}")

    # Il pallino della community e' un'altra tabella: non passa da qui, ma
    # conta lo stesso per chi guarda l'interfaccia.
    richieste = sessione.exec(
        select(CategoryRequestNotification).where(
            CategoryRequestNotification.consultant_user_id == utente.id)
    ).all()
    non_lette = sum(1 for r in richieste if not r.is_read)
    print(f"\nrichieste dalla community (pallino a parte): {len(richieste)}"
          f", non lette {non_lette}")

    if not pulisci:
        doppioni = sum(len(righe) - 1 for righe in gruppi.values() if len(righe) > 1)
        if doppioni:
            print(f"\n{doppioni} doppioni: si tolgono con --pulisci")
        return

    tolti = 0
    for righe in gruppi.values():
        for notifica in sorted(righe, key=lambda r: r.created_at or 0,
                               reverse=True)[1:]:
            sessione.delete(notifica)
            tolti += 1
    sessione.commit()
    print(f"\ntolti {tolti} doppioni, tenuto il piu' recente di ognuno")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chi", help="pezzo di email, nome o cognome")
    parser.add_argument("--quante", type=int, default=30,
                        help="quante notifiche elencare in coda (default 30)")
    parser.add_argument("--pulisci", action="store_true",
                        help="cancella i doppioni tenendo il piu' recente")
    argomenti = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(RADICE / ".env")

    from sqlmodel import Session

    from app.database import engine

    with Session(engine) as sessione:
        utenti = cerca_utenti(sessione, argomenti.chi)
        if not utenti:
            print(f"nessun utente per '{argomenti.chi}'")
            return
        if len(utenti) > 5:
            print(f"{len(utenti)} utenti per '{argomenti.chi}': scrivi qualcosa di piu' preciso")
            for utente in utenti[:20]:
                print(f"  {utente.id} {utente.nome} {utente.cognome} <{utente.email}>")
            return
        for utente in utenti:
            mostra(utente, sessione, argomenti.quante, argomenti.pulisci)


if __name__ == "__main__":
    main()
