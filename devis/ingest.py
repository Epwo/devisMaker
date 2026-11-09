"""Import d'un plan (PDF ou image) -> image en niveaux de gris."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np
import pymupdf

if TYPE_CHECKING:
    from .schemas import OcrToken

RENDER_DPI = 200
MIN_LONG_SIDE = 3300  # px


def load_pages(data: bytes, file_name: str, dpi: int = RENDER_DPI) -> list[np.ndarray]:
    """Retourne une image (uint8, gris) par page."""
    ext = Path(file_name).suffix.lower()
    if ext == ".pdf":
        doc = pymupdf.open(stream=data, filetype="pdf")
        pages = []
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
            pages.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w).copy())
        return pages
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"Format non supporté : {file_name}")
    # Image basse résolution (capture d'écran, export web) : on remonte à ~A3 200 dpi pour l'OCR.
    long_side = max(img.shape)
    if long_side < MIN_LONG_SIDE:
        f = MIN_LONG_SIDE / long_side
        img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    return [img]


def pdf_text_layer(data: bytes, file_name: str) -> str:
    """Texte vectoriel éventuel (plans exportés de CAO). Vide pour un scan."""
    if Path(file_name).suffix.lower() != ".pdf":
        return ""
    doc = pymupdf.open(stream=data, filetype="pdf")
    return "\n".join(page.get_text() for page in doc).strip()


def pdf_text_tokens(data: bytes, file_name: str, dpi: int = RENDER_DPI) -> list[list["OcrToken"]]:
    """Texte vectoriel d'un PDF CAO, en tokens positionnés dans le repère de l'image rendue.

    Une ligne de texte PDF = un token, confiance 1.0 (pas d'OCR, donc pas d'erreur de lecture).
    Retourne une liste vide par page sans texte (scan).
    """
    from .schemas import Box, OcrToken

    if Path(file_name).suffix.lower() != ".pdf":
        return []
    scale = dpi / 72
    out = []
    for page in pymupdf.open(stream=data, filetype="pdf"):
        # Les coordonnées texte sont dans le repère non tourné ; l'image rendue applique /Rotate.
        mat = page.rotation_matrix
        toks, seen = [], set()
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = " ".join(s["text"].strip() for s in line["spans"] if s["text"].strip())
                if not text:
                    continue
                r = pymupdf.Rect(line["bbox"]) * mat
                key = (text, round(r.x0), round(r.y0))
                if key in seen:  # certains exports CAO écrivent chaque texte deux fois
                    continue
                seen.add(key)
                d = pymupdf.Point(line["dir"]) * pymupdf.Matrix(mat.a, mat.b, mat.c, mat.d, 0, 0)
                toks.append(OcrToken(
                    text=text, conf=1.0,
                    box=Box(x0=r.x0 * scale, y0=r.y0 * scale, x1=r.x1 * scale, y1=r.y1 * scale),
                    vertical=abs(d.y) > 0.7,
                ))
        out.append(toks)
    return out


def to_png(img: np.ndarray, max_side: int | None = None) -> bytes:
    if max_side and max(img.shape[:2]) > max_side:
        scale = max_side / max(img.shape[:2])
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("Encodage PNG impossible")
    return buf.tobytes()


def raster_coverage(data: bytes, file_name: str) -> float:
    """Part de la 1re page couverte par une image (scan collé dans un PDF). 1.0 pour un fichier image."""
    if Path(file_name).suffix.lower() != ".pdf":
        return 1.0
    page = pymupdf.open(stream=data, filetype="pdf")[0]
    area = page.rect.width * page.rect.height or 1
    return max([r.width * r.height / area for img in page.get_images(full=True) for r in page.get_image_rects(img[0])] or [0.0])
