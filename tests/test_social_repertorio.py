"""Ricerca delle clip di repertorio su Pexels.

Senza chiave il modulo deve tirarsi indietro in silenzio: il video si fa lo
stesso con le slide brandizzate. Quando la chiave c'e', si sceglie il file piu'
leggero fra quelli verticali, perche' le clip 4K di Pexels pesano centinaia di
MB e il server ha poca memoria e poca banda.
"""
import pytest

from app.social import repertorio


def _risposta(dati, stato=200):
    class _R:
        status_code = stato

        def json(self):
            return dati
    return _R()


VIDEO_VERTICALE = {
    "id": 111,
    "duration": 12,
    "user": {"name": "Anna Rossi"},
    "video_files": [
        {"link": "https://esempio/4k.mp4", "width": 2160, "height": 3840},
        {"link": "https://esempio/hd.mp4", "width": 1080, "height": 1920},
        {"link": "https://esempio/piccolo.mp4", "width": 360, "height": 640},
    ],
}


def test_senza_chiave_non_cerca_nemmeno(monkeypatch, tmp_path):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.setattr(repertorio.requests, "get", lambda *a, **k: pytest.fail("non doveva chiamare Pexels"))
    assert repertorio.configurato() is False
    assert repertorio.cerca_clip("stressed developer", 3.0, tmp_path, "clip-1") is None


def test_sceglie_il_file_piu_leggero_abbastanza_grande(monkeypatch, tmp_path):
    monkeypatch.setenv("PEXELS_API_KEY", "chiave")
    chiamate = {}
    monkeypatch.setattr(repertorio.requests, "get",
                        lambda url, **kw: chiamate.update(url=url, kw=kw) or _risposta({"videos": [VIDEO_VERTICALE]}))
    scaricati = []
    monkeypatch.setattr(repertorio, "_scarica", lambda url, dest: scaricati.append(url) or True)

    esito = repertorio.cerca_clip("stressed developer", 3.0, tmp_path, "clip-1")

    assert esito["autore"] == "Anna Rossi"
    assert esito["percorso"] == tmp_path / "clip-1.mp4"
    # il 360x640 e' sotto la soglia, il 4K e' sprecato: si prende il 1080x1920
    assert scaricati == ["https://esempio/hd.mp4"]
    assert chiamate["kw"]["params"]["orientation"] == "portrait"
    assert chiamate["kw"]["headers"]["Authorization"] == "chiave"


def test_scarta_le_clip_orizzontali():
    orizzontale = {"video_files": [{"link": "https://esempio/wide.mp4", "width": 1920, "height": 1080}]}
    assert repertorio._file_migliore(orizzontale) is None


def test_scarta_le_clip_piu_corte_della_frase(monkeypatch, tmp_path):
    monkeypatch.setenv("PEXELS_API_KEY", "chiave")
    corta = dict(VIDEO_VERTICALE, duration=2)
    monkeypatch.setattr(repertorio.requests, "get", lambda *a, **k: _risposta({"videos": [corta]}))
    monkeypatch.setattr(repertorio, "_scarica", lambda url, dest: pytest.fail("non doveva scaricare"))
    assert repertorio.cerca_clip("qualcosa", 6.0, tmp_path, "clip-1") is None


def test_un_errore_di_pexels_non_blocca_il_video(monkeypatch, tmp_path):
    monkeypatch.setenv("PEXELS_API_KEY", "chiave")
    monkeypatch.setattr(repertorio.requests, "get", lambda *a, **k: _risposta({}, stato=429))
    assert repertorio.cerca_clip("qualcosa", 3.0, tmp_path, "clip-1") is None


def test_una_rete_che_cade_non_blocca_il_video(monkeypatch, tmp_path):
    monkeypatch.setenv("PEXELS_API_KEY", "chiave")

    def esplode(*a, **k):
        raise repertorio.requests.RequestException("rete giù")

    monkeypatch.setattr(repertorio.requests, "get", esplode)
    assert repertorio.cerca_clip("qualcosa", 3.0, tmp_path, "clip-1") is None
