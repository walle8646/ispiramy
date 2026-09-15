"""Guardia contro le chiamate lente eseguite sull'event loop.

Con un solo worker uvicorn, una chiamata di rete sincrona dentro una route
`async def` congela **tutte** le richieste per la sua intera durata. Con OpenAI
sono secondi, con l'analisi video di una contestazione sono minuti.

Questi test sono volutamente statici: leggono il sorgente invece di eseguirlo,
perché il problema è strutturale e non si manifesterebbe in un test unitario.
"""
import ast
import inspect
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = PROJECT_ROOT / "app" / "routes"

# Funzioni che fanno I/O di rete sincrono e non vanno chiamate direttamente
# dentro una coroutine: vanno passate ad asyncio.to_thread.
CHIAMATE_BLOCCANTI = {
    "create_checkout_session",
    "start_recording",
    "stop_recording",
    "analyze_dispute_video",
    "download_video_from_s3",
    "generate_carousel_for_draft",
    "generate_image_for_draft",
    "generate_media_for_draft",
    "generate_video_for_draft",
    "generate_batch",
    "publish_draft",
    "check_publishing_results",
    "get_connected_accounts",
    "put_object",
    "head_object",
}


class _Visitatore(ast.NodeVisitor):
    """Cerca chiamate bloccanti dentro funzioni async, fuori da to_thread."""

    def __init__(self):
        self.problemi = []
        self._dentro_async = 0
        self._dentro_to_thread = 0

    def visit_AsyncFunctionDef(self, node):
        self._dentro_async += 1
        self.generic_visit(node)
        self._dentro_async -= 1

    def visit_FunctionDef(self, node):
        # Una def sincrona annidata gira comunque nel threadpool: non e' un problema
        precedente, self._dentro_async = self._dentro_async, 0
        self.generic_visit(node)
        self._dentro_async = precedente

    def visit_Call(self, node):
        nome = self._nome_chiamata(node.func)

        if nome in ("to_thread", "run_in_threadpool", "run_sync"):
            # Gli argomenti di to_thread(...) sono riferimenti, non chiamate:
            # tutto ciò che sta dentro è già fuori dall'event loop.
            self._dentro_to_thread += 1
            self.generic_visit(node)
            self._dentro_to_thread -= 1
            return

        if (self._dentro_async and not self._dentro_to_thread
                and nome in CHIAMATE_BLOCCANTI):
            self.problemi.append((nome, node.lineno))

        self.generic_visit(node)

    @staticmethod
    def _nome_chiamata(func) -> str:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""


def _file_route():
    return sorted(ROUTES_DIR.glob("*.py"))


@pytest.mark.parametrize("percorso", _file_route(), ids=lambda p: p.name)
def test_nessuna_chiamata_bloccante_sull_event_loop(percorso):
    albero = ast.parse(percorso.read_text(encoding="utf-8-sig"))
    visitatore = _Visitatore()
    visitatore.visit(albero)
    assert not visitatore.problemi, (
        f"{percorso.name}: chiamate sincrone dentro una coroutine "
        f"(usare asyncio.to_thread): "
        + ", ".join(f"{nome} alla riga {riga}" for nome, riga in visitatore.problemi)
    )


class TestClientOpenAI:
    def test_il_client_e_asincrono(self):
        from openai import AsyncOpenAI
        from app.utils import ai_service

        annotazione = inspect.signature(ai_service._get_client).return_annotation
        assert annotazione is AsyncOpenAI

    def test_tutte_le_chiamate_openai_sono_attese(self):
        sorgente = (PROJECT_ROOT / "app" / "utils" / "ai_service.py").read_text(encoding="utf-8-sig")
        albero = ast.parse(sorgente)

        non_attese = []
        for nodo in ast.walk(albero):
            if not isinstance(nodo, ast.Call):
                continue
            if not isinstance(nodo.func, ast.Attribute) or nodo.func.attr != "create":
                continue
            # risali la catena: client.chat.completions.create / client.moderations.create
            radice = nodo.func
            while isinstance(radice, ast.Attribute):
                radice = radice.value
            if isinstance(radice, ast.Name) and radice.id == "client":
                non_attese.append(nodo)

        # Ogni chiamata deve essere il figlio diretto di un Await
        attese = {
            id(n.value) for n in ast.walk(albero)
            if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
        }
        mancanti = [n.lineno for n in non_attese if id(n) not in attese]
        assert not mancanti, f"chiamate OpenAI non attese alle righe {mancanti}"
        assert len(non_attese) >= 9, "attese almeno 9 chiamate a OpenAI, trovate meno"
