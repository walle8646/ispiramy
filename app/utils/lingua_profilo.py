"""Tiene d'accordo il cookie della lingua e il profilo.

Le pagine leggono il cookie. Le email e le notifiche, che partono anche
senza una richiesta, leggono il profilo. Quando la persona sceglie, si
aggiornano tutti e due.
"""
from app.utils.lingue_ui import COOKIE, GIORNI_MEMORIA, lingua_valida


def applica_lingua(request, response, session, user):
    """Il cookie, se c'è, vince e viene salvato. Altrimenti si rimette quello del profilo."""
    if user is None:
        return
    scelta = lingua_valida(request.cookies.get(COOKIE))
    if scelta:
        if getattr(user, "ui_language", None) != scelta:
            user.ui_language = scelta
            session.add(user)
            session.commit()
        return
    salvata = lingua_valida(getattr(user, "ui_language", None))
    if salvata and response is not None:
        response.set_cookie(
            COOKIE, salvata,
            max_age=GIORNI_MEMORIA * 86400,
            samesite="lax",
            path="/",
        )
