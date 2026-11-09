"""Règles OCR -> champs structurés.

Chaque règle renvoie un `Field` avec une confiance :
- confiance OCR du token quand le motif est explicite (ex. "2X Ø7.5"),
- confiance réduite quand la valeur est déduite par heuristique,
- champ vide quand l'info n'est pas trouvée.
Les champs vides ou peu sûrs seront envoyés au VLM.

Toutes les longueurs sont rendues en mm (conversion si le plan est en pouces).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .schemas import FIELD_SPECS, Bend, Box, Component, Cutout, Field, Hole, OcrToken, Source

NUM = r"(\d*\.?\d+)"
INCH = 25.4
HEURISTIC_CONF = 0.5   # valeur déduite, à confirmer
PROXIMITY_CONF = 0.75  # valeur lue à côté d'un libellé du cartouche
PARTIAL_CONF = 0.4     # info incomplète (ex. rayon de pli connu mais pas le nombre de plis)

# Épaisseurs "gauge" acier (Manufacturers' Standard Gauge), en mm
STEEL_GAUGE_MM = {7: 4.554, 8: 4.176, 9: 3.797, 10: 3.416, 11: 3.038, 12: 2.657, 13: 2.278, 14: 1.897,
                  15: 1.709, 16: 1.519, 18: 1.214, 20: 0.912, 22: 0.759, 24: 0.607, 26: 0.455}


def _compact(t: str) -> str:
    """Texte en majuscules sans espaces, avec les 0/O confondus remis en O dans les mots."""
    s = re.sub(r"\s+", "", t.upper())
    return re.sub(r"(?<=[A-Z])0|0(?=[A-Z]{2})", "O", s)


def _r(x: float) -> float:
    return round(x, 2)


# Règle d'extraction -> explication lisible (journal de décision)
RULES = {
    "parse_units": "Unité : mention « INCHES / mm » ou format des cotes (.xxx = pouces)",
    "parse_holes": "Trous : motifs « N x ØD », « ØD », « Mxx » (et « 2X » + « Ø8.75 » lus séparément)",
    "parse_cutouts": "Chanfreins : motifs « N x AxB » et « A x 45° »",
    "parse_bends": "Plis : lignes de pli « UP 90° R… » ou note « BENDING RADIUS »",
    "parse_tolerance": "Tolérances : motif « ISO 2768-x »",
    "parse_material": "Matière : libellé « MATERIAL / MATIÈRE : … », nuance (S235, 304L…) ou jauge",
    "parse_thickness": "Épaisseur : libellé « THK / ÉP. / THICKNESS »",
    "parse_treatment": "Traitement : mots-clés (RAL, zingage, anodisation, « RAW FINISH »…)",
    "parse_bom": "Nomenclature : colonnes DESIGNATION / QTY du tableau",
    "_labelled": "Cartouche : valeur à côté du libellé (TITLE, PART NUMBER, DWG NO…)",
    "parse_notes": "Notes : bloc de lignes sous « NOTES »",
    "parse_dimensions": "Encombrement : plus grande cote lue dans les vues (heuristique)",
}


def _step_lecture(rule: str, toks: list[OcrToken], value, conf: float) -> dict:
    return {
        "etape": "lecture",
        "regle": RULES.get(rule, rule),
        "textes_lus": [{"texte": t.text, "confiance_ocr": round(t.conf, 3), "vertical": t.vertical,
                        "zone": [round(t.box.x0), round(t.box.y0), round(t.box.x1), round(t.box.y1)]} for t in toks],
        "valeur": value if not isinstance(value, list) else [v.model_dump() if hasattr(v, "model_dump") else v for v in value],
        "confiance": round(conf, 3),
    }


def _ocr_field(value, tok: OcrToken | list[OcrToken], conf: Optional[float] = None) -> Field:
    import sys

    toks = tok if isinstance(tok, list) else [tok]
    rule = sys._getframe(1).f_code.co_name  # règle appelante (parse_holes…), pour le journal
    box = Box(
        x0=min(t.box.x0 for t in toks), y0=min(t.box.y0 for t in toks),
        x1=max(t.box.x1 for t in toks), y1=max(t.box.y1 for t in toks),
    )
    return Field(
        value=value,
        confidence=conf if conf is not None else min(t.conf for t in toks),
        source=Source.OCR,
        evidence=" | ".join(t.text for t in toks),
        box=box,
        boxes=list({(b.x0, b.y0, b.x1, b.y1): b for b in (t.box for t in toks)}.values()),
        trace=[_step_lecture(rule, toks, value, conf if conf is not None else min(t.conf for t in toks))],
    )


def _find(tokens: Iterable[OcrToken], pattern: str, compact: bool = False):
    rx = re.compile(pattern, re.I)
    for t in tokens:
        m = rx.search(_compact(t.text) if compact else t.text)
        if m:
            yield t, m


CARTOUCHE_WORDS = re.compile(
    r"^(SHEET|SCALE|ECHELLE|REV|SIZE|FORMAT|DATE|DRAWN|CHECKED|APPROVED|TITLE|DWG\.?NO\.?|PARTNUMBER:?|"
    r"DRAWINGTITLE|ZONE|DESCRIPTION|WEIGHT|MASSE|SHEET\d+OF\d+|\d+OF\d+|\d+:\d+)$"
)


def _near(tokens: list[OcrToken], label: OcrToken, max_dist: float = 350) -> list[OcrToken]:
    """Tokens à droite ou juste en dessous d'un libellé (cellule de cartouche)."""
    out = []
    for t in tokens:
        if t is label or CARTOUCHE_WORDS.match(_compact(t.text)):
            continue
        dx, dy = t.box.x0 - label.box.x0, t.box.y0 - label.box.y0
        right = 0 < t.box.x0 - label.box.x1 + 5 < max_dist and abs(dy) < 25
        below = -40 < dx < max_dist and 0 < dy < 90
        if right or below:
            out.append((abs(dx) + 2 * abs(dy), t))
    return [t for _, t in sorted(out, key=lambda x: x[0])]


# --------------------------------------------------------------------------- unités

def parse_units(tokens: list[OcrToken]) -> tuple[float, Field]:
    for t, _ in _find(tokens, r"INCH|POUCE|DIMSIN\"|UNIT:?IN", compact=True):
        return INCH, _ocr_field("in", t)
    for t, _ in _find(tokens, r"MILLIMET|DIMENSIONSINMM|UNIT:?MM", compact=True):
        return 1.0, _ocr_field("mm", t)
    nums = [t.text for t in tokens if re.fullmatch(r"\(?\d*\.\d+\)?", t.text)]
    leading_dot = [n for n in nums if n.lstrip("(").startswith(".")]
    three_dec = [n for n in nums if re.search(r"\.\d{3}\b", n)]
    if nums and (len(leading_dot) + len(three_dec)) / len(nums) > 0.4:
        f = Field(value="in", confidence=0.8, source=Source.OCR, evidence="cotes au format .xxx")
        return INCH, f
    return 1.0, Field(value="mm", confidence=0.9, source=Source.DEFAULT, evidence="unité par défaut")


# --------------------------------------------------------------------------- règles

def _split_multiplier(tokens: list[OcrToken]) -> list[tuple[OcrToken, OcrToken, int, float]]:
    """« 2X » lu séparément de « Ø8.75 » / « 8.75 » juste à sa droite (PP-OCRv5) -> (mult, cote, qté, Ø)."""
    out = []
    for a in tokens:
        m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*", a.text)
        if not m or a.vertical:
            continue
        h = a.box.y1 - a.box.y0
        right = [b for b in tokens if b is not a and not b.vertical
                 and 0 <= b.box.x0 - a.box.x1 < 2.5 * h and abs((b.box.y0 + b.box.y1) / 2 - (a.box.y0 + a.box.y1) / 2) < 1.2 * h]
        for b in sorted(right, key=lambda b: b.box.x0):
            n = re.match(rf"\s*Ø?\s*{NUM}\b(?!\s*[xX]\s*\d)", b.text)
            if n:
                out.append((a, b, int(m.group(1)), float(n.group(1))))
                break
    return out


def parse_holes(tokens: list[OcrToken], k: float) -> Field:
    holes, src = [], []
    for t, m in _find(tokens, rf"(\d+)\s*[xX]\s*Ø\s*{NUM}"):
        holes.append(Hole(diameter_mm=_r(float(m.group(2)) * k), quantity=int(m.group(1))))
        src.append(t)
    for a, b, q, d in _split_multiplier(tokens):
        if any(s is b for s in src):
            continue
        holes.append(Hole(diameter_mm=_r(d * k), quantity=q))
        src += [a, b]
    for t, m in _find(tokens, rf"^Ø\s*{NUM}(?![\d.]*\s*[A-Z]\b)(?:\s*(?:THRU|TYP|\((\d+)\s*(?:X|PL)))?"):
        d = float(m.group(1)) * k
        if d < 0.5:
            continue  # "Ø0.1 A" = cadre de tolérance de position, pas un trou
        holes.append(Hole(diameter_mm=_r(d), quantity=int(m.group(2) or 1)))
        src.append(t)
    # Taraudages "M6", "4x M8"
    for t, m in _find(tokens, r"(?:(\d+)\s*[xX]\s*)?\bM(\d{1,2})\b(?!\s*1[.,]00)"):
        if re.search(r"NUT|ECROU|INSERT|SCREW|VIS", t.text.upper()):
            continue  # composant de nomenclature, pas un taraudage
        holes.append(Hole(diameter_mm=float(m.group(2)), quantity=int(m.group(1) or 1), kind="threaded"))
        src.append(t)
    if not holes:
        return Field()
    holes = list({(h.diameter_mm, h.quantity, h.kind): h for h in holes}.values())
    f = _ocr_field(holes, src)
    if any(h.quantity == 1 for h in holes):
        # "Ø.250" sans multiplicateur : le nombre réel de trous se lit sur la vue
        f.confidence = min(f.confidence, PROXIMITY_CONF)
        f.issues.append("Trou(s) sans quantité explicite : nombre à confirmer sur la vue")
    return f


def parse_cutouts(tokens: list[OcrToken], k: float) -> Field:
    cuts, src = [], []
    for t, m in _find(tokens, rf"(\d+)\s*[xX]\s*{NUM}\s*[xX]\s*{NUM}"):
        cuts.append(Cutout(description=f"chanfrein {_r(float(m.group(2)) * k)}x{_r(float(m.group(3)) * k)} mm",
                           quantity=int(m.group(1))))
        src.append(t)
    for t, m in _find(tokens, rf"^{NUM}\s*[xX]\s*45\s*°"):
        cuts.append(Cutout(description=f"chanfrein {_r(float(m.group(1)) * k)} mm x 45°", quantity=1))
        src.append(t)
    if not cuts:
        return Field()
    # même annotation répétée (vue pliée + développé) : une seule fois
    cuts = list({(c.description, c.quantity): c for c in cuts}.values())
    f = _ocr_field(cuts, src)
    if any(c.quantity == 1 for c in cuts):
        f.confidence = min(f.confidence, PROXIMITY_CONF)
    return f


BEND_DIR = r"(UP|DOWN|HAUT|BAS|НАГОРЕ|НАДОЛУ)"


def parse_bends(tokens: list[OcrToken], k: float) -> Field:
    bends, src = [], []
    # Lignes de pli du développé : "UP 90° R .08", "90° нагоре"
    for t, m in _find(tokens, rf"{BEND_DIR}\s*{NUM}\s*°(?:\s*R\s*{NUM})?"):
        r = float(m.group(3)) * k if m.group(3) else None
        bends.append(Bend(angle_deg=float(m.group(2)), radius_mm=_r(r) if r else None))
        src.append(t)
    for t, m in _find(tokens, rf"^{NUM}\s*°\s*{BEND_DIR}"):
        bends.append(Bend(angle_deg=float(m.group(1))))
        src.append(t)
    # "BEND UP 90.0 DEG. (4X)"
    for t, m in _find(tokens, rf"BEND\s*{BEND_DIR}?\s*{NUM}\s*(?:DEG|°)\.?\s*(?:\((\d+)\s*X\)|(\d+)\s*X)?"):
        if any(s is t for s in src):
            continue
        bends.append(Bend(angle_deg=float(m.group(2)), quantity=int(m.group(3) or m.group(4) or 1)))
        src.append(t)
    radius = None
    for t, m in _find(tokens, rf"BENDINGRADIUS:?{NUM}|RAYONDEPLIAGE:?{NUM}|BENDRADIUS:?{NUM}", compact=True):
        radius = _r(float(next(g for g in m.groups() if g)) * (1 if "MM" in _compact(t.text) else k))
        src.append(t)
        break
    if bends:
        for b in bends:
            b.radius_mm = b.radius_mm or radius
        return _ocr_field(bends, src, 0.9)
    if radius is not None:
        f = _ocr_field([Bend(radius_mm=radius, angle_deg=None, quantity=None)], src, PARTIAL_CONF)
        f.issues.append("Rayon de pliage lu dans les notes ; nombre et angle des plis non lisibles par OCR")
        return f
    return Field()


def parse_tolerance(tokens: list[OcrToken]) -> Field:
    for t, m in _find(tokens, r"ISO2768-?([FMCV])", compact=True):
        return _ocr_field(f"ISO 2768-{m.group(1).lower()}", t)
    return Field()


GRADES = (r"(S235\w*|S275\w*|S355\w*|DC0[1-6]|DD1[1-4]|DX5\dD?|A36|10[0-4]\d|30[4-9]L?|316L?|430|1\.4\d{3}"
          r"|5754|5052|5083|6060|6061(?:-T\d)?|6082|ALMG\d|XC\d{2}|E24|HLE\d*)")
FAMILIES = [
    (r"INOX|STAINLESS|НЕРЪЖД", "inox"),
    (r"ALU|АЛУМИН", "aluminium"),
    (r"GALVA|ZINGU|ПОЦИНК", "acier galvanisé"),
    (r"STEEL|ACIER|СТОМАНА", "acier"),
    (r"COPPER|CUIVRE|BRASS|LAITON", "cuivre/laiton"),
]
MAT_LABEL = r"(?:MATERIAL|MATIERE|MATIÈRE|MATERIAU|MAT'?L|МАТЕРИАЛ)\.?:(.*)"  # ":" obligatoire (sinon phrases)
SENTENCE_WORDS = 9  # au-delà, le token est une phrase (consigne, remarque), pas une donnée


def _family_of(text: str) -> Optional[str]:
    for rx, fam in FAMILIES:
        if re.search(rx, text, re.I):
            return fam
    return None


def _family_of_grade(g: str) -> str:
    if re.match(r"30|31|430|1\.4", g):
        return "inox"
    if re.match(r"5\d{3}|6\d{3}|ALMG", g, re.I):
        return "aluminium"
    return "acier"


def parse_material(tokens: list[OcrToken], k: float) -> tuple[Field, Field, Field]:
    """Retourne (matière, nuance, épaisseur) — l'épaisseur est souvent dans la ligne matière."""
    material, grade, thick = Field(), Field(), Field()
    mat_tokens = [(t, m) for t, m in _find(tokens, MAT_LABEL, compact=True)]
    candidates = [t for t, _ in mat_tokens]
    for t, m in mat_tokens:
        rest = m.group(1) or ""
        if not rest:  # libellé seul dans sa cellule : valeur à côté
            nxt = _near(tokens, t)
            if nxt:
                rest = _compact(nxt[0].text)
                candidates.append(nxt[0])
        fam = _family_of(rest)
        g = re.search(GRADES, rest)
        if g:
            grade = _ocr_field(g.group(1), t)
            fam = fam or _family_of_grade(g.group(1))
        if fam:
            material = _ocr_field(fam, t)
        gauge = re.search(r"(\d{1,2})(?:GAUGE|GA\b|GA\.)", rest)
        inch = re.search(r"(\d*\.\d+)\"", rest)
        mm = re.search(rf"{NUM}MM", rest)
        if gauge and int(gauge.group(1)) in STEEL_GAUGE_MM:
            thick = _ocr_field(STEEL_GAUGE_MM[int(gauge.group(1))], t, 0.9)
            thick.evidence += f" (jauge {gauge.group(1)} acier)"
        elif inch:
            thick = _ocr_field(_r(float(inch.group(1)) * INCH), t)
        elif mm:
            thick = _ocr_field(float(mm.group(1)), t)
        break
    if grade.missing:
        for t, m in _find(tokens, GRADES, compact=True):
            if t.vertical or re.fullmatch(r"\d+", _compact(t.text)):
                continue  # une cote "1018" n'est pas une nuance
            grade = _ocr_field(m.group(1), t, PROXIMITY_CONF)
            break
    if material.missing and not grade.missing:
        material = Field(
            value=_family_of_grade(grade.value), confidence=HEURISTIC_CONF, source=Source.OCR,
            evidence=grade.evidence, box=grade.box, boxes=grade.boxes,
            issues=[f"Matière déduite de la nuance {grade.value}"])
    return material, grade, thick


def parse_thickness(tokens: list[OcrToken], k: float) -> Field:
    for t, m in _find(tokens, rf"(?:THK|THICKNESS|EPAISSEUR|ÉPAISSEUR|EP\.|T=|ДЕБЕЛИНА):?{NUM}(MM|\")?", compact=True):
        unit = INCH if m.group(2) == '"' else (1 if m.group(2) == "MM" else k)
        return _ocr_field(_r(float(m.group(1)) * unit), t)
    return Field()


def parse_treatment(tokens: list[OcrToken]) -> Field:
    for t, m in _find(tokens, r"RAL\s*(\d{4})"):
        kind = "peinture poudre" if re.search(r"POWDER|POUDRE|ПРАХОВ|THERMOLAQ", t.text, re.I) else "peinture"
        return _ocr_field(f"{kind} RAL{m.group(1)}", t)
    for t, m in _find(tokens, r"(ZINGAGE|ZINC\w*|GALVANI\w*|CATAPHOR\w*|ANODI\w*|THERMOLAQU\w*|POWDERCOAT\w*|PEINTURE\w*|PAINTED)", compact=True):
        if re.search(r"NOPAINT|EPARGNE", _compact(t.text)) or len(t.text.split()) > SENTENCE_WORDS - 4:
            continue
        return _ocr_field(m.group(1).lower(), t, PROXIMITY_CONF)
    for t, _ in _find(tokens, r"RAWFINISH|BRUT|NOFINISH|SANSTRAITEMENT", compact=True):
        return _ocr_field("aucun (brut)", t)
    return Field()


TITLE_LABELS = r"^(?:TITLE|DRAWINGTITLE|DESIGNATION|DÉSIGNATION|НАИМЕНОВАНИЕ|PARTNAME|NOM)\.?:?(.*)$"
REF_LABELS = r"^(?:PARTNUMBER|PARTNO|P/N|DWG\.?NO|DRAWINGNO|REF|RÉF|REFERENCE|РЕФЕРЕНЦИЯ|АРТИКУЛЕННОМЕР|ЧЕРТЕЖЕННОМЕР)\.?:?(.*)$"


def _labelled(tokens: list[OcrToken], pattern: str, value_rx: str) -> Field:
    for t, m in _find(tokens, pattern, compact=True):
        inline = (m.group(1) or "").strip()
        if inline and re.search(value_rx, inline):
            return _ocr_field(t.text.split(":", 1)[-1].strip(), t, PROXIMITY_CONF)
        for n in _near(tokens, t):
            if re.fullmatch(value_rx, n.text.strip()) and not re.search(r"[:]$", n.text):
                return _ocr_field(n.text.strip(), [t, n], PROXIMITY_CONF)
    return Field()


def parse_reference(tokens: list[OcrToken]) -> Field:
    return _labelled(tokens, REF_LABELS, r"[A-Z0-9][A-Z0-9\-_./]{2,30}")


def parse_title(tokens: list[OcrToken]) -> Field:
    return _labelled(tokens, TITLE_LABELS, r"[^\d\s][^:]{2,60}")


def _bom(tokens: list[OcrToken]) -> list[tuple[str, int, float, list[OcrToken]]]:
    """Lit la nomenclature REP / DESIGNATION / QTY. Retourne (désignation, qté, conf, tokens)."""
    head = next((t for t in tokens if _compact(t.text) in {"QTY", "QTE", "QTÉ", "QUANTITY"}), None)
    desig = next((t for t in tokens if _compact(t.text) in {"DESIGNATION", "DÉSIGNATION", "DESCRIPTION"}), None)
    if not head or not desig:
        return []
    qx = (head.box.x0 + head.box.x1) / 2
    rows = []
    for t in tokens:
        if t.box.y0 <= head.box.y1 or abs(t.box.x0 - desig.box.x0) > 400 or t is desig:
            continue
        if len(re.findall(r"[A-Za-zÀ-ÿ]", t.text)) < 3:
            continue  # numéro de repère (colonne REP), pas une désignation
        if t.box.y0 > head.box.y1 + 600:
            continue
        # quantité dans la colonne QTY sur la même ligne (±50 px, la cellule couvre 2 lignes FR/EN)
        cy = (t.box.y0 + t.box.y1) / 2
        q = [
            u for u in tokens
            if abs((u.box.x0 + u.box.x1) / 2 - qx) < 80 and abs((u.box.y0 + u.box.y1) / 2 - cy) < 50 and u is not head
        ]
        if not q:
            continue
        raw = q[0].text.strip()
        conf = min(t.conf, q[0].conf)
        qty_txt = {"乙": "2", "Z": "2", "l": "1", "I": "1", "|": "1", "O": "0", "S": "5"}.get(raw, raw)
        if qty_txt != raw:
            conf = min(conf, HEURISTIC_CONF)
        if not qty_txt.isdigit():
            continue
        rows.append((t.text, int(qty_txt), conf, [t, q[0]]))
    # garde une ligne par quantité/position (la traduction FR est sur la ligne suivante)
    rows.sort(key=lambda r: r[3][0].box.y0)
    dedup, last_q = [], None
    for r in rows:
        if last_q is not None and r[3][1] is last_q:
            continue
        dedup.append(r)
        last_q = r[3][1]
    return dedup


def parse_bom(tokens: list[OcrToken]) -> tuple[Field, Field]:
    rows = _bom(tokens)
    if not rows:
        return Field(), Field()
    main, *others = rows
    name = main[0].strip()
    half = name.split()
    if len(half) % 2 == 0 and half[: len(half) // 2] == half[len(half) // 2:]:
        name = " ".join(half[: len(half) // 2])  # « SUPPORT SUPPORT » : libellés EN + FR dans la même cellule
    designation = _ocr_field(name, main[3], main[2])
    if not others:
        return designation, Field(value=[], confidence=main[2], source=Source.OCR, evidence="nomenclature sans composant")
    comps = [Component(designation=r[0], quantity=r[1]) for r in others]
    f = _ocr_field(comps, [t for r in others for t in r[3]], min(r[2] for r in others))
    return designation, f


def parse_notes(tokens: list[OcrToken]) -> Field:
    head = next((t for t in tokens if re.match(r"NOTES?[:/]|NOTES?$|NOTA", _compact(t.text))), None)
    if not head:
        return Field()
    cands = sorted(
        (t for t in tokens
         if t.box.y0 > head.box.y1 - 5 and abs(t.box.x0 - head.box.x0) < 300 and t.conf > 0.8 and len(t.text) > 8
         and not re.search(r"RAYON|EPARGNE|ECROU|SUIVANT|NONCOTES", _compact(t.text))),  # doublons FR
        key=lambda t: t.box.y0,
    )
    # Bloc de notes = lignes qui se suivent sans trou vertical (on s'arrête au cartouche)
    lines, last_y = [], head.box.y1
    for t in cands:
        if t.box.y0 - last_y > 200:  # au-delà : on a quitté le bloc de notes
            break
        lines.append(t)
        last_y = t.box.y1
    if not lines:
        return Field()
    return _ocr_field([t.text for t in lines], lines)


def parse_dimensions(tokens: list[OcrToken], k: float) -> dict[str, Field]:
    """Heuristique : l'encombrement max est la plus grande cote isolée des vues."""
    head = next((t for t in tokens if _compact(t.text) in {"REP.", "REP", "NOTE/NOTA:", "NOTE", "NOTES:"}), None)
    title = [t for t in tokens if re.search(r"TOLERANC|SCALE|ДОПУСКИ|МАЩАБ|ECHELLE|ÉCHELLE", _compact(t.text))]
    notes_x = head.box.x0 if head and head.box.x0 > 600 else None
    tb_y = min((t.box.y0 for t in title), default=None)

    def in_views(t: OcrToken) -> bool:
        if notes_x is not None and t.box.x0 > notes_x - 100:
            return False
        return not (tb_y is not None and t.box.y0 > tb_y - 20 and any(abs(t.box.x0 - u.box.x0) < 900 for u in title))

    dims = [
        t for t in tokens
        if re.fullmatch(rf"\(?{NUM}\)?", t.text) and in_views(t) and 0.5 <= float(t.text.strip("()")) * k <= 3000
    ]
    out = {k_: Field() for k_ in ("length_mm", "width_mm", "height_mm", "flat_length_mm", "flat_width_mm")}
    if dims:
        # Plan coté avec 1 décimale ("82,0") : une cote sans décimale est sans doute une virgule perdue
        # par l'OCR ("1622" pour "162,2").
        one_dec = [t for t in dims if re.fullmatch(r"\(?\d+\.\d\)?", t.text)]
        fixed: dict[int, float] = {}
        if len(one_dec) >= 0.6 * len(dims):
            for t in dims:
                s = t.text.strip("()")
                if re.fullmatch(r"\d{3,}", s):
                    fixed[id(t)] = float(s[:-1] + "." + s[-1])

        def val(t: OcrToken) -> float:
            return fixed.get(id(t), float(t.text.strip("()")))

        best = max(dims, key=val)
        f = _ocr_field(_r(val(best) * k), best, HEURISTIC_CONF)
        f.issues.append("Plus grande cote lue sur le plan : encombrement à confirmer")
        if id(best) in fixed:
            f.confidence = PARTIAL_CONF
            f.issues.append(f"OCR « {best.text} » corrigé en {val(best)} (virgule décimale probablement perdue)")
        out["length_mm"] = f
    return out


# --------------------------------------------------------------------------- entrée

def parse(tokens: list[OcrToken]) -> dict[str, Field]:
    fields: dict[str, Field] = {name: Field() for name in FIELD_SPECS}
    k, fields["units"] = parse_units(tokens)
    fields["holes"] = parse_holes(tokens, k)
    fields["cutouts"] = parse_cutouts(tokens, k)
    fields["bends"] = parse_bends(tokens, k)
    fields["tolerance_standard"] = parse_tolerance(tokens)
    fields["material"], fields["material_grade"], fields["thickness_mm"] = parse_material(tokens, k)
    if fields["thickness_mm"].missing:
        fields["thickness_mm"] = parse_thickness(tokens, k)
    fields["surface_treatment"] = parse_treatment(tokens)
    fields["designation"], fields["components"] = parse_bom(tokens)
    if fields["designation"].missing:
        fields["designation"] = parse_title(tokens)
    fields["reference"] = parse_reference(tokens)
    fields["notes"] = parse_notes(tokens)
    fields.update(parse_dimensions(tokens, k))
    for name, f in fields.items():
        if not f.trace:
            f.trace.append({"etape": "lecture", "resultat": "aucune règle n'a trouvé cette information dans le texte lu"}
                           if f.missing else {"etape": "lecture", "regle": "valeur par défaut / déduction", "valeur": f.value,
                                              "confiance": f.confidence, "detail": f.evidence})
        if f.issues:
            f.trace.append({"etape": "regle_alerte", "alertes": list(f.issues)})
    return fields
