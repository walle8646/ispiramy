"""Il video della homepage.

Quello vecchio era fatto da un'intelligenza artificiale e chiudeva col vecchio
marchio ("Helpy") e con l'italiano storpiato. Il rischio non è che il video si
rompa: è che ne torni in pagina uno sbagliato senza che nessuno se ne accorga.
"""
import io
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def test_la_homepage_punta_al_video_nuovo():
    home = _file("app", "templates", "home.html")
    assert "video_homepage_v2.mp4" in home
    assert "video_homepage.mp4" not in home.replace("video_homepage_v2.mp4", "")


def test_il_video_ha_una_copertina():
    """Se il browser non fa partire il video da solo (succede su iPhone e con
    il risparmio energetico) senza copertina resta un rettangolo nero."""
    home = _file("app", "templates", "home.html")
    video = home[home.index('<video id="heroVideo"'):]
    video = video[:video.index("</video>")]
    assert "poster=" in video and ".jpg" in video


def test_nessuna_traccia_del_vecchio_marchio():
    """Helpy era il nome prima di Ispiramy: nel video vecchio compariva in
    chiusura, a schermo intero."""
    script = _file("scripts", "genera_video_homepage.py")
    testi = script[script.index("SCENE = ["):script.index("def _ffmpeg")]
    assert "Helpy" not in testi
    assert "Ispiramy" in testi


def test_le_schermate_del_video_sono_quelle_vere():
    """Le interfacce finte del video precedente avevano scritte inventate
    ("Aree di comptenza"): riprendendo il sito vero il problema non si pone."""
    script = _file("scripts", "genera_video_homepage.py")
    assert '"tipo": "schermata"' in script
    assert "--screenshot=" in script, "le schermate vanno riprese dal sito, non disegnate"
