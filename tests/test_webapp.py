"""Il sito installabile sul telefono (webapp).

Sono poche regole ma si rompono in silenzio: se salta il manifest sparisce il
tasto "installa" e nessuno se ne accorge finché non prova a installarla; se il
service worker finisce sotto /static/ smette di occuparsi delle pagine.
"""
import io
import json
import os

RADICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file(*percorso):
    return io.open(os.path.join(RADICE, *percorso), encoding="utf-8").read()


def _manifest():
    return json.loads(_file("app", "static", "manifest.webmanifest"))


def test_il_manifest_ha_quello_che_serve_per_installare():
    """Senza uno di questi campi il telefono non propone l'installazione."""
    m = _manifest()
    assert m["name"] and m["short_name"]
    assert m["start_url"].startswith("/")
    assert m["scope"] == "/"
    assert m["display"] == "standalone"
    assert m["theme_color"] and m["background_color"]


def test_ci_sono_le_icone_dichiarate():
    """Android chiede 192 e 512; la 'maskable' evita che il logo venga
    ritagliato dentro la sagoma tonda del sistema."""
    m = _manifest()
    misure = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= misure
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    for icona in m["icons"]:
        percorso = os.path.join(RADICE, "app", icona["src"].lstrip("/"))
        assert os.path.exists(percorso), f"manca il file {icona['src']}"
        assert os.path.getsize(percorso) > 1000


def test_il_service_worker_sta_in_cima_al_sito(client):
    """Da /static/sw.js si occuperebbe solo di /static/: la navigazione fra le
    pagine non la vedrebbe mai."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["service-worker-allowed"] == "/"
    assert "javascript" in r.headers["content-type"]
    assert "no-cache" in r.headers.get("cache-control", "")


def test_il_manifest_si_scarica(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers["content-type"]


def test_la_pagina_senza_rete_sta_in_piedi_da_sola(client):
    """Viene mostrata quando la rete non c'è: se si portasse dietro CSS, font o
    immagini da fuori, resterebbe una pagina bianca."""
    r = client.get("/senza-rete")
    assert r.status_code == 200
    assert "http://" not in r.text and "https://" not in r.text.replace(
        'xmlns="http://www.w3.org/2000/svg"', "")


def test_le_pagine_non_finiscono_in_cache():
    """La cache è condivisa fra tutti gli account di quel telefono: una pagina
    di profilo salvata lì la vedrebbe anche chi entra dopo."""
    sw = _file("app", "static", "sw.js")
    dopo_statici = sw[sw.index("if (eStatico(url))"):]
    navigazione = dopo_statici[dopo_statici.index("eNavigazione(richiesta)"):]
    assert "cache.put" not in navigazione, "il ramo della navigazione non deve salvare in cache"
    assert "richiesta.method !== 'GET'" in sw, "le scritture non passano dal worker"
    assert "'/api/'" in sw, "le chiamate dell'app devono andare sempre in rete"


def test_ogni_pagina_registra_il_worker_e_dichiara_il_manifest():
    base = _file("app", "templates", "base.html")
    assert 'rel="manifest"' in base and "/manifest.webmanifest" in base
    assert "navigator.serviceWorker.register('/sw.js')" in base
    assert 'name="theme-color"' in base
    # iOS ignora l'SVG come icona dell'app
    assert "apple-touch-icon.png" in base


def test_l_invito_a_installare_non_disturba_durante_una_chiamata():
    base = _file("app", "templates", "base.html")
    assert "/booking/call/" in base
    assert "display-mode: standalone" in base, "a chi l'ha già installata non si chiede più niente"


def _blocco_invito():
    base = _file("app", "templates", "base.html")
    return base[base.index("const RICORDA"):base.index("})();", base.index("const RICORDA"))]


def test_il_manifest_viaggia_con_le_credenziali():
    """Il browser chiede il manifest senza credenziali: dove il sito è protetto
    (staging) tornava 401, quindi niente manifest e niente installazione."""
    base = _file("app", "templates", "base.html")
    assert 'rel="manifest"' in base
    riga = [r for r in base.splitlines() if 'rel="manifest"' in r][0]
    assert 'crossorigin="use-credentials"' in riga


def test_l_invito_compare_anche_senza_l_evento_del_browser():
    """Chrome lancia beforeinstallprompt quando gli pare, e su iPhone non esiste
    proprio: se aspettassimo solo quello, il banner non lo vedrebbe nessuno."""
    invito = _blocco_invito()
    assert "beforeinstallprompt" in invito
    assert "setTimeout" in invito, "manca il ripiego quando l'evento non arriva"
    assert "Installa app" in invito, "su Android va detto dove sta la voce nel menu"
    assert "Condividi" in invito, "su iPhone si installa solo da lì"


def test_le_frasi_dell_invito_non_spezzano_il_javascript():
    """Un apostrofo dentro una stringa fra apici singoli rompe l'intero script:
    era già successo con 'come un'app' e la pagina restava senza JavaScript."""
    import re

    # Nei commenti gli apostrofi sono liberi: si guarda solo il codice
    codice = re.sub(r"/\*.*?\*/", "", _blocco_invito(), flags=re.S)
    codice = re.sub(r"//[^\n]*", "", codice)
    for riga in codice.splitlines():
        nuda = riga.strip()
        assert not re.search(r"'[^'\n]*[A-Za-z]'[A-Za-z]", nuda), f"apostrofo da proteggere: {nuda}"


def test_la_pausa_dopo_la_x_dura_tre_giorni():
    """Chi chiude il banner non lo rivede per tre giorni: abbastanza da non
    essere molesto, poco da ricapitare a chi ci ripensa."""
    assert "const GIORNI_DI_PAUSA = 3;" in _blocco_invito()


def test_c_e_sempre_una_voce_per_installare_nel_menu():
    """Il banner si può chiudere e sul computer non compare: senza una voce
    fissa nel menu, chi la vuole installare non ha nessuna strada."""
    base = _file("app", "templates", "base.html")
    assert 'id="voceInstalla"' in base
    menu = base[base.index('<div class="user-menu-dropdown"'):base.index("</div>", base.index('id="voceInstalla"'))]
    assert "voceInstalla" in menu, "la voce deve stare nel menu del profilo"
    invito = _blocco_invito()
    assert "mostra(true)" in invito, "dal menu il banner si apre anche se era stato chiuso"


def test_sul_telefono_la_voce_sta_in_chiaro_nel_menu():
    """Nel menu del profilo, sul telefono, si arriva solo aprendo il menu e poi
    toccando un'immagine da 32px: lì dentro la voce non la trovava nessuno."""
    base = _file("app", "templates", "base.html")
    assert 'id="voceInstallaTelefono"' in base
    links = base[base.index('<div class="navbar-links">'):base.index("{% if current_user %}")]
    assert "voceInstallaTelefono" in links, "la riga deve stare nel menu principale"
    assert "voceInstallaTelefono" in _blocco_invito(), "e deve essere collegata"
    # sul computer resta solo quella nel menu del profilo
    assert ".voce-installa-telefono" in base


def test_dal_computer_il_banner_non_compare_da_solo():
    """Sul desktop l'installazione la offre già il browser: il banner si vede
    solo se lo chiede la persona dal menu."""
    invito = _blocco_invito()
    assert "window.innerWidth <= 768" in invito


def test_anche_la_pagina_profilo_ha_il_collegamento():
    """«Lo vedo nel menù ma non nel profilo»: la voce nel menu dell'avatar non
    è la pagina Profilo, dove la scheda Account è il posto che tutti guardano."""
    profilo = _file("app", "templates", "profile.html")
    account = profilo[profilo.index("<h3 style=\"color: #1a202c;\">Account</h3>"):]
    account = account[:account.index("</div>\n                </div>")]
    assert 'id="voceInstallaProfilo"' in account
    assert "voceInstallaProfilo" in _blocco_invito(), "deve essere collegata come le altre"


def test_il_manifest_dichiara_se_stesso_come_app_collegata(client):
    """Senza questa dichiarazione il browser non risponde a chi gli chiede se
    l'app e' gia' installata: dal browser normale continueremmo a proporla."""
    m = client.get("/manifest.webmanifest").json()
    collegate = m["related_applications"]
    assert collegate and collegate[0]["platform"] == "webapp"
    assert collegate[0]["url"].endswith("/manifest.webmanifest")
    # true direbbe al browser di proporre l'altra app al posto della nostra
    assert m["prefer_related_applications"] is False


def test_chiediamo_al_browser_se_l_app_c_e_gia():
    """Aprendo il sito dal browser dopo aver installato l'app, la pagina non ha
    modo di accorgersene da sola: lo deve chiedere."""
    invito = _blocco_invito()
    assert "navigator.getInstalledRelatedApps" in invito
    assert "accendiVoci" in invito, "la risposta deve accendere o spegnere le voci"


def test_le_voci_si_spengono_con_una_classe_non_con_lo_stile_in_riga():
    """Le aree toccabili impongono display: inline-flex !important: un
    display: none scritto sull'elemento perdeva, e nel profilo la voce
    restava li' anche a chi aveva gia' installato l'app."""
    base = _file("app", "templates", "base.html")
    assert ".voce-installa-nascosta" in base and "display: none !important;" in base
    assert "classList.toggle('voce-installa-nascosta'" in _blocco_invito()
    for modello in ("base.html", "profile.html"):
        testo = _file("app", "templates", modello)
        for riga in testo.splitlines():
            if "voceInstalla" in riga and "<a " in riga:
                assert "display: none" not in riga, f"{modello}: usa la classe, non lo stile in riga"
