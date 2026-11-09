"""Journaux du serveur : appels OCR / IA (durée, tokens, coût, cache), erreurs, replis, requêtes HTTP.

- console (visible dans le terminal d'uvicorn ou `docker compose logs -f app`) ;
- fichier tournant si DEVIS_LOG_FILE est défini (ex. data/logs/devismaker.log) ;
- mémoire : les 1000 dernières lignes, lisibles par GET /logs.

DEVIS_LOG_LEVEL : DEBUG | INFO (défaut) | WARNING | ERROR.
Chaque ligne porte le plan en cours d'analyse quand il y en a un (contexte « plan »).
"""

from __future__ import annotations

import collections
import contextvars
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

current_plan: contextvars.ContextVar[str] = contextvars.ContextVar("current_plan", default="-")

_BUFFER: collections.deque[dict] = collections.deque(maxlen=1000)
_CONFIGURED = False


class _PlanFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.plan = current_plan.get()
        return True


class _MemoryHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _BUFFER.append({
            "date": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "niveau": record.levelname,
            "source": record.name.removeprefix("devis."),
            "plan": getattr(record, "plan", "-"),
            "message": record.getMessage() + (("\n" + self.format(record).split("\n", 1)[1]) if record.exc_info else ""),
        })


def setup_logging() -> None:
    """Configure le logger « devis » (tous les modules devis.* et l'API). Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True
    level = getattr(logging, os.getenv("DEVIS_LOG_LEVEL", "INFO").upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(plan)s] %(name)s : %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("devis")
    root.setLevel(level)
    root.propagate = False
    # Windows : sortie redirigée (service, IDE) en cp1252 -> accents illisibles ; on force l'UTF-8
    stream = sys.stderr
    if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    handlers: list[logging.Handler] = [logging.StreamHandler(stream), _MemoryHandler()]
    if os.getenv("DEVIS_LOG_FILE"):
        path = Path(os.environ["DEVIS_LOG_FILE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(_PlanFilter())
        root.addHandler(h)
    # nos journaux de requêtes remplacent ceux d'uvicorn (qui listent aussi chaque fichier statique)
    logging.getLogger("uvicorn.access").disabled = True


def recent(level: str = "INFO", limit: int = 200, plan: str | None = None) -> list[dict]:
    """Dernières lignes, du plus récent au plus ancien, filtrées par niveau minimal et/ou par plan."""
    floor = getattr(logging, level.upper(), logging.INFO)
    rows = [r for r in reversed(_BUFFER)
            if getattr(logging, r["niveau"], 0) >= floor and (not plan or plan.lower() in r["plan"].lower())]
    return rows[:limit]
