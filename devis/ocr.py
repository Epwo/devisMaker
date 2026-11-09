"""OCR du plan — moteurs interchangeables (variable DEVIS_OCR_ENGINE).

- rapid_v4     : RapidOCR, modèles PaddleOCR PP-OCRv4 (ONNX, CPU) — historique
- ppocr_v5     : PaddleOCR PP-OCRv5 mobile (via rapidocr 3, ONNX, CPU)
- ppocr_v5_srv : PaddleOCR PP-OCRv5 server (plus lourd)
- mistral      : Mistral OCR (API) pour le texte + positions de l'OCR local (voir ocr_mistral.py)

Les moteurs locaux font deux passes : image normale (texte horizontal) puis image
tournée de 90° pour lire les cotes verticales, très fréquentes sur les plans.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Callable

import cv2
import numpy as np

from .schemas import Box, OcrToken

BORDER_MARGIN = 45  # px : repères de grille du cadre (1..8, A..F)
DEFAULT_ENGINE = "rapid_v4"

Recognizer = Callable[[np.ndarray], list[tuple[list, str, float]]]


@lru_cache(maxsize=1)
def _rapid_v4():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def _run_v4(img: np.ndarray) -> list[tuple[list, str, float]]:
    res, _ = _rapid_v4()(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
    return res or []


@lru_cache(maxsize=2)
def _rapid_v5(server: bool):
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

    mt = ModelType.SERVER if server else ModelType.MOBILE
    return RapidOCR(params={
        "Global.log_level": "error",
        "Det.engine_type": EngineType.ONNXRUNTIME, "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": mt,
        "Rec.engine_type": EngineType.ONNXRUNTIME, "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": mt,
    })


def _runner_v5(server: bool) -> Recognizer:
    def run(img: np.ndarray):
        r = _rapid_v5(server)(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
        if r.boxes is None:
            return []
        return [(box.tolist(), txt, float(sc)) for box, txt, sc in zip(r.boxes, r.txts, r.scores)]
    return run


# kept for backward compatibility (old name)
_run = _run_v4


def _box(points) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return Box(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))


def _overlap(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    inter = ix * iy
    area = min((a.x1 - a.x0) * (a.y1 - a.y0), (b.x1 - b.x0) * (b.y1 - b.y0)) or 1.0
    return inter / area


def normalize(text: str) -> str:
    """Corrige les confusions OCR classiques sur les plans."""
    t = text
    for c in ("ϕ", "Φ", "φ", "ø", "⌀", "∅"):
        t = t.replace(c, "Ø")
    t = t.replace("�", "°")  # degré mal encodé dans certains PDF
    t = t.replace("×", "x").replace("✕", "x")  # signe multiplié (PP-OCRv5, Mistral)
    # "08. 75" en début de cote précédée de 2X : le Ø est souvent lu comme 0
    t = re.sub(r"(\d+\s*[xX]\s*)0(\d)", r"\1Ø\2", t)
    t = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", t)
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def strip_border(tokens: list[OcrToken], w: int, h: int) -> list[OcrToken]:
    """Retire ce qui touche le bord et les repères de grille du cadre (1..8, A..H près des bords)."""
    mx, my = 0.07 * w, 0.07 * h

    def grid_label(t: OcrToken) -> bool:
        if not re.fullmatch(r"[1-9A-H]", t.text.strip()):
            return False
        return t.box.x0 < mx or t.box.x1 > w - mx or t.box.y0 < my or t.box.y1 > h - my

    return [
        t
        for t in tokens
        if not (
            t.box.x0 < BORDER_MARGIN
            or t.box.y0 < BORDER_MARGIN
            or t.box.x1 > w - BORDER_MARGIN
            or t.box.y1 > h - BORDER_MARGIN
            or grid_label(t)
        )
    ]


def two_pass(img: np.ndarray, recognize: Recognizer) -> list[OcrToken]:
    """OCR horizontal + passe sur l'image tournée pour les cotes verticales."""
    h, w = img.shape[:2]
    tokens: list[OcrToken] = []

    for pts, txt, conf in recognize(img):
        tokens.append(OcrToken(text=normalize(txt), conf=float(conf), box=_box(pts)))

    # Passe verticale : rotation horaire, le texte écrit de bas en haut devient horizontal.
    rot = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    for pts, txt, conf in recognize(rot):
        # (xr, yr) dans l'image tournée -> (x, y) = (yr, h - 1 - xr)
        orig = [(p[1], h - 1 - p[0]) for p in pts]
        box = _box(orig)
        clashes = [t for t in tokens if _overlap(box, t.box) > 0.3]
        tall = (box.y1 - box.y0) > 1.2 * (box.x1 - box.x0)
        txt_n = normalize(txt)
        better = all(len(txt_n) >= len(c.text) and conf >= c.conf - 0.05 for c in clashes)
        longer = all(len(txt_n) > len(c.text) for c in clashes)  # « 60 » vs « 6 », « 22 » vs « 2 »
        if clashes and not (better and (tall or longer)):
            continue  # texte horizontal déjà lu (ex. « 13 » ne doit pas devenir « 3 »)
        # Boîte verticale : c'est une cote verticale, la lecture horizontale était du bruit.
        tokens = [t for t in tokens if t not in clashes]
        tokens.append(OcrToken(text=normalize(txt), conf=float(conf), box=box, vertical=True))

    # Supprime les repères de grille du cadre.
    return strip_border(tokens, w, h)


ENGINES: dict[str, Callable[[np.ndarray], list[OcrToken]]] = {
    "rapid_v4": lambda img: two_pass(img, _run_v4),
    "ppocr_v5": lambda img: two_pass(img, _runner_v5(False)),
    "ppocr_v5_srv": lambda img: two_pass(img, _runner_v5(True)),
}


def _mistral(img: np.ndarray) -> list[OcrToken]:
    from .ocr_mistral import mistral_ocr

    return mistral_ocr(img)


def _mistral_board(img: np.ndarray) -> list[OcrToken]:
    from .ocr_mistral import mistral_board

    return mistral_board(img)


ENGINES["mistral"] = _mistral
ENGINES["mistral_board"] = _mistral_board


def default_engine() -> str:
    """DEVIS_OCR_ENGINE si défini ; sinon « auto » : planche Mistral si une clé est configurée
    (meilleur résultat au benchmark, cf. out/bench_ocr.md), OCR local sinon."""
    name = os.getenv("DEVIS_OCR_ENGINE", "auto")
    if name == "auto":
        return "mistral_board" if os.getenv("MISTRAL_API_KEY") else DEFAULT_ENGINE
    return name


_MEMO: dict[tuple[str, str], list[OcrToken]] = {}


def run_ocr(img: np.ndarray, engine: str | None = None) -> list[OcrToken]:
    import hashlib

    name = engine or default_engine()
    if name not in ENGINES:
        raise ValueError(f"Moteur OCR inconnu : {name} (disponibles : {', '.join(ENGINES)})")
    # Même image, même moteur -> même résultat : mémorisé (réanalyse, banc d'essai multi-modèles)
    key = (name, hashlib.sha1(img.tobytes()).hexdigest())
    if key not in _MEMO:
        _MEMO[key] = ENGINES[name](img)
    return [t.model_copy() for t in _MEMO[key]]
