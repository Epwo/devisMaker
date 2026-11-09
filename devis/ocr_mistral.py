"""Moteur OCR hybride : Mistral OCR pour le texte, OCR local pour les positions.

Constat (piece_003) : sur la page entière, Mistral OCR lit parfaitement le texte
(notes, nomenclature, tolérances) mais range les vues du dessin dans des « images »
sans lire les cotes. En recadrant chaque vue (droite puis tournée de 90°), il lit
toutes les cotes, y compris les verticales — mais sans coordonnées par texte.

Donc :
1. page entière -> blocs (texte, tableaux, listes, images) avec leurs rectangles ;
2. chaque bloc « image » (vue du dessin) -> recadrage -> OCR droit + tourné ;
3. chaque texte lu par Mistral est rattaché à la détection locale (PP-OCR) dont le
   texte lui ressemble le plus, pour récupérer sa position exacte sur le plan
   (surlignage, sélection au clic). À défaut, position = rectangle du bloc.

Les réponses de l'API sont mises en cache sur disque (.cache/mistral) : relancer
une analyse du même plan ne coûte rien.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np

from .ingest import to_png
from .schemas import Box, OcrToken

log = logging.getLogger(__name__)

MODEL = os.getenv("DEVIS_MISTRAL_OCR_MODEL", "mistral-ocr-latest")
CACHE = Path(__file__).resolve().parents[1] / ".cache" / "mistral"
CONF = 0.95          # Mistral ne donne pas de confiance par ligne ici : lecture jugée fiable
MATCH_MIN = 0.55     # similarité minimale pour reprendre la position d'une détection locale
CROP_PAD = 12        # px autour d'une vue recadrée
CROP_UPSCALE = 1.5   # les petites cotes se lisent mieux un peu agrandies


class MistralOcrError(RuntimeError):
    pass


# --------------------------------------------------------------------------- API + cache

PAGES_SENT = [0]          # pages soumises à Mistral OCR (cache compris) : sert à estimer le coût
PRICE_PER_PAGE_USD = 0.004  # Mistral OCR 4 : 4 $ / 1000 pages (tarif standard)


def _call(png: bytes) -> dict:
    PAGES_SENT[0] += 1
    key = hashlib.sha1(MODEL.encode() + png).hexdigest()
    path = CACHE / f"{key}.json"
    if path.exists():
        log.info("Mistral OCR : réponse en cache (%s)", key[:8])
        return json.loads(path.read_text(encoding="utf-8"))
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        log.warning("Mistral OCR : MISTRAL_API_KEY absente")
        raise MistralOcrError("MISTRAL_API_KEY absente")
    t0 = time.time()
    log.info("Mistral OCR : appel (image %d Ko)…", len(png) // 1024)
    from mistralai.client import Mistral

    client = Mistral(api_key=api_key)
    try:
        r = client.ocr.process(
            model=MODEL,
            document={"type": "image_url", "image_url": "data:image/png;base64," + base64.b64encode(png).decode()},
            include_blocks=True,
        )
    except Exception as e:  # erreurs réseau / quota / auth du SDK
        log.warning("Mistral OCR : ÉCHEC après %.1f s : %s", time.time() - t0, str(e)[:300])
        raise MistralOcrError(f"Mistral OCR : {e}") from e
    page = r.model_dump()["pages"][0]
    log.info("Mistral OCR : réponse en %.1f s (%d caractères)", time.time() - t0, len(page.get("markdown") or ""))
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(page, ensure_ascii=False, default=str), encoding="utf-8")
    return page


# --------------------------------------------------------------------------- markdown -> lignes

def _md_lines(md: str) -> list[str]:
    """Lignes de texte utiles d'un markdown Mistral (tableaux, LaTeX, listes, images retirés)."""
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)                 # images
    md = re.sub(r"\\begin\{array\}\{[^}]*\}|\\end\{array\}|\\hline|\$\$?", " ", md)  # LaTeX
    md = md.replace("\\\\", "\n")
    out = []
    for line in md.splitlines():
        if re.fullmatch(r"\s*\|?\s*(-{3,}\s*\|\s*)*-{3,}\s*\|?\s*", line):
            continue  # séparateur de tableau
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if "|" in line else [line]
        for c in cells:
            c = re.sub(r"^\s*[-*•]\s+", "", c).strip()
            if c:
                out.append(c)
    return out


def _table_tokens(md: str, b: Box) -> list[OcrToken]:
    """Tableau markdown -> un token par cellule, positionné sur une grille dans le rectangle du bloc
    (permet au parseur de retrouver les colonnes DESIGNATION / QTY de la nomenclature)."""
    rows = [r for r in md.splitlines() if "|" in r and not re.fullmatch(r"[\s|:-]+", r)]
    grid = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if not grid:
        return []
    n_rows, n_cols = len(grid), max(len(r) for r in grid)
    # largeur de colonne proportionnelle au texte le plus long de la colonne
    widths = [max(len(r[i]) if i < len(r) else 0 for r in grid) + 4 for i in range(n_cols)]
    total = sum(widths)
    rh = (b.y1 - b.y0) / n_rows
    toks = []
    for ri, r in enumerate(grid):
        x = b.x0
        for ci in range(n_cols):
            cw = (b.x1 - b.x0) * widths[ci] / total
            text = r[ci] if ci < len(r) else ""
            if text:
                toks.append(OcrToken(text=text, conf=CONF, box=Box(x0=x + 4, y0=b.y0 + ri * rh + 4, x1=x + cw - 4, y1=b.y0 + (ri + 1) * rh - 4)))
            x += cw
    return toks


def _block_box(blk: dict, sx: float, sy: float) -> Box:
    return Box(x0=blk["top_left_x"] * sx, y0=blk["top_left_y"] * sy, x1=blk["bottom_right_x"] * sx, y1=blk["bottom_right_y"] * sy)


# --------------------------------------------------------------------------- rattachement aux positions locales

def _key(t: str) -> str:
    from .ocr import normalize

    return re.sub(r"[^0-9A-Z.Ø+°]", "", normalize(t).upper())


def _inside(b: Box, region: Box, pad: float = 20) -> bool:
    cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
    return region.x0 - pad <= cx <= region.x1 + pad and region.y0 - pad <= cy <= region.y1 + pad


def _place(lines: list[str], region: Box, local: list[OcrToken], used: set[int], vertical: bool) -> list[OcrToken]:
    from .ocr import normalize

    cands = [(i, t) for i, t in enumerate(local) if _inside(t.box, region)]
    out = []
    for line in lines:
        k = _key(line)
        if not k:
            continue
        best, score = None, 0.0
        for i, t in cands:
            if i in used:
                continue
            s = SequenceMatcher(None, k, _key(t.text)).ratio()
            if s > score:
                best, score = (i, t), s
        if best and score >= MATCH_MIN:
            used.add(best[0])
            out.append(OcrToken(text=normalize(line), conf=CONF, box=best[1].box, vertical=best[1].vertical))
        else:
            out.append(OcrToken(text=normalize(line), conf=CONF * 0.9, box=region, vertical=vertical))
    return out


def view_regions(img: np.ndarray) -> list[Box]:
    """Rectangles des vues du dessin (blocs « image » de Mistral OCR ; réponse en cache si déjà analysée)."""
    h, w = img.shape[:2]
    page = _call(to_png(img))
    dims = page.get("dimensions") or {}
    sx, sy = w / (dims.get("width") or w), h / (dims.get("height") or h)
    return [_block_box(b, sx, sy) for b in (page.get("blocks") or []) if b.get("type") == "image"]


# --------------------------------------------------------------------------- moteur « planche »

def _delatex(s: str) -> str:
    """Nettoie les réponses LaTeX occasionnelles de Mistral (\\phi, \\frac, \\boxed…)."""
    s = s.replace("\\(", " ").replace("\\)", " ")
    for _ in range(6):
        prev = s
        s = re.sub(r"\\(?:phi|varphi|Phi|diameter|emptyset|varnothing|O)\b", "Ø", s)
        s = re.sub(r"[_^]\{([^{}]*)\}", r" \1", s)
        s = re.sub(r"\\sqrt\{[^{}]*\}", "", s)
        s = re.sub(r"\\overset\{([^{}]*)\}\{([^{}]*)\}", r"\2 \1", s)
        s = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1 \2", s)
        s = re.sub(r"\\(?:boxed|text|mathrm|mathbf|operatorname)\{([^{}]*)\}", r"\1", s)
        if s == prev:
            break
    s = re.sub(r"\\[a-zA-Z]+", " ", s).replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", s).strip()


def _merge_detections(a: list[OcrToken], b: list[OcrToken]) -> list[OcrToken]:
    """Union de deux jeux de détections : on ajoute de `b` ce qui ne recouvre rien dans `a`."""
    from .ocr import _overlap

    out = list(a)
    for t in b:
        if not any(_overlap(t.box, u.box) > 0.3 for u in out):
            out.append(t)
    return out


def _board(img: np.ndarray, toks: list[OcrToken], row_h: int = 56) -> tuple[np.ndarray, list[int]]:
    """Planche d'étiquettes : chaque zone détectée, redressée, numérotée [i], empilée."""
    rows, idx = [], []
    for i, t in enumerate(toks):
        b, p = t.box, 8
        c = img[max(0, int(b.y0) - p):int(b.y1) + p, max(0, int(b.x0) - p):int(b.x1) + p]
        if c.size == 0:
            continue
        if t.vertical:
            c = cv2.rotate(c, cv2.ROTATE_90_CLOCKWISE)
        s = row_h / c.shape[0]
        c = cv2.resize(c, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if c.shape[1] > 1800:
            c = cv2.resize(c, (1800, row_h), interpolation=cv2.INTER_AREA)
        lab = np.full((row_h, 120), 255, np.uint8)
        cv2.putText(lab, f"[{i}]", (4, int(row_h * 0.72)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2)
        rows.append(np.hstack([lab, np.full((row_h, 30), 255, np.uint8), c]))
        idx.append(i)
    if not rows:
        return np.full((10, 10), 255, np.uint8), []
    width = max(r.shape[1] for r in rows) + 20
    board = np.vstack([np.pad(r, ((10, 10), (10, width - r.shape[1] - 10)), constant_values=255) for r in rows])
    return board, idx


def mistral_board(img: np.ndarray) -> list[OcrToken]:
    """Détection locale (PP-OCRv4 ∪ PP-OCRv5) + lecture Mistral sur une planche d'étiquettes numérotées.
    Le texte courant (notes, nomenclature) est lu par Mistral sur la page entière."""
    from .ocr import ENGINES, normalize, strip_border

    h, w = img.shape[:2]
    page = _call(to_png(img))
    dims = page.get("dimensions") or {}
    sx, sy = w / (dims.get("width") or w), h / (dims.get("height") or h)
    text_blocks = [b for b in (page.get("blocks") or []) if b.get("type") not in ("image", "aside_text")]
    text_regions = [_block_box(b, sx, sy) for b in text_blocks]

    tokens: list[OcrToken] = []
    for blk, box in zip(text_blocks, text_regions):
        content = blk.get("content") or ""
        if blk.get("type") == "table":
            tokens += [t.model_copy(update={"text": normalize(t.text)}) for t in _table_tokens(content, box)]
            continue
        lines = _md_lines(content)
        lh = (box.y1 - box.y0) / max(1, len(lines))
        tokens += [OcrToken(text=normalize(l), conf=CONF, box=Box(x0=box.x0, y0=box.y0 + i * lh, x1=box.x1, y1=box.y0 + (i + 1) * lh))
                   for i, l in enumerate(lines)]

    # Détections locales hors des blocs de texte déjà lus par Mistral (= cotes et annotations des vues)
    local = _merge_detections(ENGINES["rapid_v4"](img), ENGINES["ppocr_v5"](img))
    # On n'écarte une détection locale que si Mistral a réellement lu ce texte dans le bloc qui la contient :
    # un bloc « texte » peut couvrir tout le dessin (komacut_bent_part.jpg) et ne contenir que les notes.
    block_keys = [(r, _key(b.get("content") or "")) for b, r in zip(text_blocks, text_regions)]

    def already_read(t: OcrToken) -> bool:
        k = _key(t.text)
        return bool(k) and any(_inside(t.box, r, pad=0) and k in content for r, content in block_keys)

    local = [t for t in local if not already_read(t)]
    board, idx = _board(img, local)
    md = _call(to_png(board))["markdown"] if idx else ""
    read: dict[int, str] = {}
    for m in re.finditer(r"\[(\d+)\]\s*\|?\s*([^\n|]*)", md):
        read[int(m.group(1))] = m.group(2).strip()
    for i, t in enumerate(local):
        raw = read.get(i, "")
        # Une lecture Mistral sans lettre ni chiffre (« 🔍 » pour un « 6 » vertical, davoc_fixation_ardoise.jpg)
        # n'apporte rien : on garde la lecture locale si elle, en a.
        if raw and not _key(raw) and _key(t.text):
            log.info("Mistral OCR : « %s » illisible pour [%d], lecture locale « %s » conservée", raw, i, t.text)
            raw = ""
        if raw and "\\" not in raw:      # lecture Mistral « propre » : on la retient
            mt = normalize(raw)
            # Mistral corrige souvent l'OCR local… mais peut aussi se tromper (« 2 » lu « 21 » sur piece_003) :
            # on garde l'autre lecture comme alternative quand elles diffèrent.
            alt = t.text if _key(t.text) and _key(t.text) != _key(mt) else None
            tokens.append(t.model_copy(update={"text": mt, "conf": CONF, "alt": alt}))
        else:                            # pas lu, ou réponse LaTeX douteuse : lecture locale
            tokens.append(t)
    return strip_border(tokens, w, h)


# --------------------------------------------------------------------------- moteur « vues »

def mistral_ocr(img: np.ndarray, local_engine: str = "rapid_v4") -> list[OcrToken]:
    from .ocr import ENGINES, normalize, strip_border

    h, w = img.shape[:2]
    page = _call(to_png(img))
    dims = page.get("dimensions") or {}
    sx, sy = w / (dims.get("width") or w), h / (dims.get("height") or h)
    blocks = page.get("blocks") or []

    # Positions de référence : OCR local (sert aussi de filet si Mistral ignore une zone)
    local = ENGINES[local_engine](img)
    used: set[int] = set()
    tokens: list[OcrToken] = []
    regions: list[Box] = []

    # Recadrages des vues (en parallèle : 2 appels par vue)
    views = [b for b in blocks if b.get("type") == "image"]

    def read_view(blk):
        box = _block_box(blk, sx, sy)
        x0, y0 = max(0, int(box.x0) - CROP_PAD), max(0, int(box.y0) - CROP_PAD)
        x1, y1 = min(w, int(box.x1) + CROP_PAD), min(h, int(box.y1) + CROP_PAD)
        crop = cv2.resize(img[y0:y1, x0:x1], None, fx=CROP_UPSCALE, fy=CROP_UPSCALE, interpolation=cv2.INTER_CUBIC)
        straight = _md_lines(_call(to_png(crop))["markdown"])
        turned = _md_lines(_call(to_png(cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)))["markdown"])
        return box, straight, turned

    with ThreadPoolExecutor(max_workers=4) as pool:
        view_results = list(pool.map(read_view, views))

    for box, straight, turned in view_results:
        regions.append(box)
        tokens += _place(straight, box, local, used, vertical=False)
        # Passe tournée : on n'ajoute que ce qu'elle lit en plus (cotes verticales), en multi-ensemble
        have = Counter(_key(x) for x in straight)
        extra = []
        for line in turned:
            k = _key(line)
            if have[k] > 0:
                have[k] -= 1
            else:
                extra.append(line)
        tokens += _place(extra, box, local, used, vertical=True)

    for blk in blocks:
        kind, box = blk.get("type"), _block_box(blk, sx, sy)
        content = blk.get("content") or ""
        if kind in ("image", "aside_text"):  # vues traitées ci-dessus ; repères de grille du cadre
            continue
        regions.append(box)
        if kind == "table":
            tokens += [t.model_copy(update={"text": normalize(t.text)}) for t in _table_tokens(content, box)]
            continue
        lines = _md_lines(content)
        lh = (box.y1 - box.y0) / max(1, len(lines))
        for i, line in enumerate(lines):
            tokens.append(OcrToken(text=normalize(line), conf=CONF,
                                   box=Box(x0=box.x0, y0=box.y0 + i * lh, x1=box.x1, y1=box.y0 + (i + 1) * lh)))

    # Filet : ce que l'OCR local a lu hors de toute zone vue par Mistral
    for t in local:
        if not any(_inside(t.box, r, pad=0) for r in regions):
            tokens.append(t)

    return strip_border(tokens, w, h)
