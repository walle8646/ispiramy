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
    assert "save_screenshot" in script, "le schermate vanno riprese dal sito, non disegnate"


def test_il_video_racconta_anche_prenotazione_e_pagamento():
    """Che si prenoti e si paghi dentro la piattaforma non si capisce da una
    scena di videochiamata: va mostrato il riepilogo vero, spese comprese."""
    script = _file("scripts", "genera_video_homepage.py")
    scene = script[script.index("SCENE = ["):script.index("def _ffmpeg")]
    assert "/book/1" in scene, "manca la schermata della prenotazione"
    assert "paghi" in scene.lower()
    # il riepilogo col prezzo compare solo dopo aver scelto una fascia oraria
    assert '"clicca"' in scene


def test_i_nomi_dei_consulenti_non_vanno_in_onda():
    """Le schermate arrivano da profili di persone: il nome si sfoca nel
    browser prima dello scatto, cosi' nel file non ci finisce mai."""
    script = _file("scripts", "genera_video_homepage.py")
    assert "NOMI_DA_SFOCARE" in script
    for selettore in (".consultant-name", ".pub-hero-text h1", ".consultant-info h1"):
        assert selettore in script, f"manca dalla sfocatura: {selettore}"
    assert "blur(" in script


def test_il_giro_si_chiude_con_la_recensione():
    """Il cerchio si chiude lì: chi ha ricevuto aiuto aiuta il prossimo a
    scegliere. Senza, il video finisce con la videochiamata e basta."""
    script = _file("scripts", "genera_video_homepage.py")
    scene = script[script.index("SCENE = ["):script.index("def _ffmpeg")]
    assert "recensione" in scene.lower()
    assert "reviews-toggle-header" in scene, "le recensioni stanno dietro un pannello da aprire"
    # anche chi scrive la recensione ha un nome
    assert ".pub-review-header strong" in script


def test_il_marchio_si_legge_come_si_pronuncia():
    """Scritto "Ispiramy" la voce lo storpiava: nel parlato si scrive come
    suona, a schermo resta il marchio giusto."""
    script = _file("scripts", "genera_video_homepage.py")
    scene = script[script.index("SCENE = ["):script.index("def _ffmpeg")]
    for riga in scene.splitlines():
        if '"voce"' in riga:
            assert "Ispiramy" not in riga, "nel parlato va scritto Ispirami"
    assert '"titolo": "Ispiramy"' in scene, "a schermo il marchio resta quello vero"
