"""Stratégie « pointage » : le modèle de vision COMPREND le plan, l'OCR LIT les valeurs.

Chaque texte reconnu par l'OCR est encadré et numéroté sur une copie du plan
(« set-of-marks »). Le modèle ne recopie aucun chiffre : il indique quels numéros
portent l'épaisseur, l'encombrement, les trous… La valeur est ensuite calculée à
partir du texte OCR désigné (exact, et localisé sur le plan).

Principe : mieux vaut AUCUNE valeur qu'une fausse. Si le modèle n'est pas sûr, il
renvoie une liste vide, et si le texte désigné ne contient pas de valeur exploitable,
le champ reste vide.
"""
from __future__ import annotations

import json
import re
from typing import Any

import cv2
import numpy as np

from .ocr import normalize
from .parser import FAMILIES, GRADES, STEEL_GAUGE_MM, _family_of_grade
from .schemas import OcrToken
from .vlm import Part, call_vision_json

INCH = 25.4
MAX_MARKS = 220

SYSTEM = """Tu es technicien méthodes en tôlerie. Tu analyses la STRUCTURE d'un plan technique : vues, \
cotes hors-tout, épaisseur, trous, plis, matière. Tu ne recopies jamais un chiffre : tu désignes les textes \
numérotés qui portent l'information.

Méthode (un plan montre LA MÊME pièce sous plusieurs angles : vue de dessus, de face / profil, de côté, \
isométrique, développé) :
1. Repère chaque vue et son type. Les vues agrandies qui suivent le plan portent les mêmes numéros.
2. Pour chaque vue orthographique, désigne la cote HORS-TOUT horizontale et la cote HORS-TOUT verticale de la pièce \
dans cette vue (champ « views »), liste vide si elle n'est pas cotée. Une même dimension réelle apparaît souvent \
dans deux vues (ex. la largeur sur la vue de dessus ET sur la vue de face).
3. Déduis l'encombrement (length ≥ width ≥ height) en combinant les vues : dessus = largeur × profondeur, \
face/profil = largeur × hauteur, côté = profondeur × hauteur.
4. Épaisseur : sur la vue de profil, la petite cote posée sur la TRANCHE de la tôle (ou une note « ép. / THK »).
5. Plis : compte les changements de direction de la tôle sur la vue de profil ; l'isométrique sert à vérifier.

Règles :
- Images : le plan, le même plan où chaque texte reconnu est encadré en rouge avec son numéro, puis les vues \
agrandies (mêmes numéros).
- Un texte peut avoir deux lectures (« lecture alternative ») : désigne-le normalement, le bon chiffre sera choisi.
- Pour chaque information, donne les numéros [n] des textes qui la portent. Liste vide si l'information n'est pas \
sur le plan, si aucun texte numéroté ne la porte, ou si tu n'es pas sûr. UNE INFORMATION ABSENTE VAUT MIEUX QU'UNE \
FAUSSE : ne désigne jamais un texte « approchant ».
- Encombrement (length ≥ width ≥ height) : cotes HORS-TOUT de la pièce finie pliée (boîte englobante), une par \
direction ; jamais une cote de position de trou ni la cote d'une seule aile. Si une direction n'est pas cotée \
hors-tout, liste vide pour ce champ.
- Développé (flat_*) : uniquement si une vue à plat (développé / flat pattern) est cotée.
- Épaisseur : texte qui donne l'épaisseur de la tôle (cote sur la tranche, « ép. », « THK », « T=… », « 40/10 », \
jauge « 16 GAUGE », note matière « 0.049" STEEL »).
- Trous : un élément par annotation de trou (ØD, NxØD, Mxx). quantity = nombre de trous concernés si tu le vois \
sur le dessin et que l'annotation ne le dit pas, sinon null. Un chanfrein (5x5, 20x20, 0.375 X 45°), un oblong ou \
un rayon (R…) n'est PAS un trou.
- Plis : count = nombre de plis que tu vois sur les vues (changements de direction de la tôle), null si incertain ; \
angle_ids / radius_ids = textes qui donnent l'angle ou le rayon de pliage.
- Matière : textes qui nomment la matière ou la nuance. Désignation : texte du nom de la pièce (cartouche, \
nomenclature) ; méfie-toi d'un titre qui contredit le dessin."""

ID_LIST = {"type": "array", "items": {"type": "integer"}}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _pick() -> dict:
    return _obj({"ids": ID_LIST, "confidence": {"type": "number"}, "reason": {"type": "string"}})


SCALAR_FIELDS = ["designation", "material", "thickness_mm", "length_mm", "width_mm", "height_mm", "flat_length_mm", "flat_width_mm"]


def build_schema() -> dict:
    props: dict[str, Any] = {k: _pick() for k in SCALAR_FIELDS}
    props["holes"] = _obj({
        "items": {"type": "array", "items": _obj({"ids": ID_LIST, "quantity": {"anyOf": [{"type": "integer"}, {"type": "null"}]}})},
        "confidence": {"type": "number"}, "reason": {"type": "string"}})
    props["bends"] = _obj({
        "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "angle_ids": ID_LIST, "radius_ids": ID_LIST, "confidence": {"type": "number"}, "reason": {"type": "string"}})
    # Lecture vue par vue (même pièce, plusieurs angles) : l'encombrement est recombiné par le code
    props["views"] = {"type": "array", "items": _obj({
        "kind": {"type": "string", "enum": VIEW_KINDS},
        "overall_h_ids": ID_LIST, "overall_v_ids": ID_LIST})}
    return _obj(props)


VIEW_KINDS = ["dessus", "face", "cote", "isometrique", "developpe", "autre"]
VIEW_AXES = {"dessus": ("X", "Y"), "face": ("X", "Z"), "cote": ("Y", "Z")}  # projection européenne


# --------------------------------------------------------------------------- image annotée

def marked_image(img: np.ndarray, tokens: list[OcrToken]) -> np.ndarray:
    """Copie couleur du plan, chaque texte encadré en rouge avec son numéro en bleu."""
    out = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img.copy()
    h, w = out.shape[:2]
    fs = max(0.55, w / 3300 * 0.8)
    th = max(1, int(round(fs * 2)))
    for i, t in enumerate(tokens[:MAX_MARKS]):
        b = t.box
        p1, p2 = (int(b.x0) - 3, int(b.y0) - 3), (int(b.x1) + 3, int(b.y1) + 3)
        cv2.rectangle(out, p1, p2, (0, 0, 230), th)
        label = str(i)
        (tw, tht), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, th)
        x, y = max(0, p1[0]), max(tht + 4, p1[1] - 2)
        cv2.rectangle(out, (x, y - tht - 4), (x + tw + 4, y + 2), (255, 255, 255), -1)
        cv2.putText(out, label, (x + 2, y - 2), cv2.FONT_HERSHEY_SIMPLEX, fs, (200, 60, 0), th)
    return out


def token_list(tokens: list[OcrToken]) -> str:
    return "\n".join(f"[{i}] « {t.text} »{' (vertical)' if t.vertical else ''}"
                     + (f" (lecture alternative : « {t.alt} »)" if t.alt else "") for i, t in enumerate(tokens[:MAX_MARKS]))


def marked_view_crops(marked: np.ndarray, page_img: np.ndarray, max_views: int = 4) -> list[np.ndarray]:
    """Vues du dessin, recadrées sur l'image annotée (mêmes numéros), pour lire les petites cotes."""
    import os

    # Désactivé par défaut : au banc d'essai, les vues agrandies n'aident pas les petits modèles Mistral
    # et font dépasser le délai à Mistral Large 4. DEVIS_MARKS_VIEW_CROPS=1 pour les réactiver.
    if os.getenv("DEVIS_MARKS_VIEW_CROPS", "0") != "1" or not os.getenv("MISTRAL_API_KEY"):
        return []
    try:
        from .ocr_mistral import view_regions

        regions = view_regions(page_img)
    except Exception:  # noqa: BLE001 — pas de découpage disponible : on se contente du plan entier
        return []
    h, w = marked.shape[:2]
    crops = []
    for b in sorted(regions, key=lambda b: (b.y1 - b.y0) * (b.x1 - b.x0), reverse=True)[:max_views]:
        x0, y0, x1, y1 = max(0, int(b.x0) - 25), max(0, int(b.y0) - 25), min(w, int(b.x1) + 25), min(h, int(b.y1) + 25)
        if (x1 - x0) * (y1 - y0) < 0.7 * w * h and x1 - x0 > 150 and y1 - y0 > 150:
            crops.append(marked[y0:y1, x0:x1])
    return crops


# --------------------------------------------------------------------------- texte désigné -> valeur

FRACTION = re.compile(r"(?:(\d+)\s*[- ]\s*)?(\d+)\s*/\s*(\d+)")


def number_of(text: str, field: str, inch_plan: bool) -> float | None:
    """Valeur en mm portée par un texte de cote (gère pouces, fractions, jauges, « 40/10 »)."""
    t = normalize(text)
    up = t.upper()
    if field == "thickness_mm":
        g = re.search(r"(\d{1,2})\s*(?:GAUGE|GA\b)", up)
        if g and int(g.group(1)) in STEEL_GAUGE_MM:
            return STEEL_GAUGE_MM[int(g.group(1))]
        tenth = re.search(r"\b(\d{1,3})\s*/\s*10\b", t)  # « 40/10 » = 4,0 mm (notation tôlerie)
        if tenth:
            return int(tenth.group(1)) / 10
        # « ACIER S235 ép 8mm » : le nombre qui suit « ép. / T= / THK » ou qui précède « mm », pas le premier venu
        ctx = re.search(r"(?:[ÉE]P\.?|THK|T\s*=|THICKNESS)\s*:?\s*(\d*\.?\d+)", up) or re.search(r"(\d*\.?\d+)\s*MM\b", up)
        if ctx:
            v = float(ctx.group(1))
            return v if 0.2 <= v <= 50 else None
    f = FRACTION.search(t)
    if f and int(f.group(3)) in (2, 4, 8, 16, 32, 64):  # fraction de pouce : 4-7/8, 1/16"
        whole = int(f.group(1) or 0)
        return round((whole + int(f.group(2)) / int(f.group(3))) * INCH, 3)
    m = re.search(r"(\d*\.?\d+)", t)
    if not m:
        return None
    v = float(m.group(1))
    explicit_mm = re.search(r"\d\s*MM\b", up) is not None
    inch = (not explicit_mm) and (inch_plan or '"' in t or re.search(r"\bIN(CH)?\b", up) is not None)
    v = round(v * INCH, 3) if inch else v
    if field == "thickness_mm" and not 0.2 <= v <= 50:
        return None  # une « épaisseur » hors plage tôlerie est une mauvaise lecture
    return v


def material_of(text: str) -> str | None:
    up = normalize(text).upper().replace(" ", "")
    for rx, fam in FAMILIES:
        if re.search(rx, up, re.I):
            return fam
    g = re.search(GRADES, up)
    if g:
        return _family_of_grade(g.group(1))
    if re.search(r"\bAL\s?\d{4}|^AL\d", up):
        return "aluminium"
    if re.search(r"\bSS\s?3\d\d|SUS\d", up):
        return "inox"
    return None


def hole_of(text: str, inch_plan: bool) -> tuple[float | None, int | None, str]:
    t = normalize(text)
    th = re.search(r"\bM(\d{1,2})\b", t)
    if th and not re.search(r"Ø", t):
        return float(th.group(1)), _qty(t), "threaded"
    m = re.search(r"Ø\s*(\d*\.?\d+)", t) or re.search(r"(?:\d+\s*[xX]\s*)(\d*\.?\d+)", t)
    if not m:
        return None, None, "through"
    v = float(m.group(1))
    inch = inch_plan or '"' in t
    return (round(v * INCH, 3) if inch else v), _qty(t), "through"


def _qty(t: str) -> int | None:
    m = re.search(r"(?<![\d.])(\d+)\s*[xX×]\s*(?=Ø|M\d|\d)", t) or re.search(r"Ø\s*[\d.]+\s*[xX]\s*(\d+)", t) \
        or re.search(r"\((\d+)\s*(?:X|PL)", t, re.I)
    return int(m.group(1)) if m else None


def angle_of(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|DEG)", normalize(text), re.I)
    return float(m.group(1)) if m else None


# --------------------------------------------------------------------------- appel + conversion

def looks_imperial(tokens: list[OcrToken]) -> bool:
    """Fractions de pouce (« 4-7/8 », « 1/16" ») ou mention INCH : plan en pouces."""
    rx = re.compile(r"\d+\s*-\s*\d+/(?:2|4|8|16|32|64)\b|\b\d+/(?:16|32|64)\b|\d\"|\bINCH")
    return any(rx.search(t.text.upper()) for t in tokens)


def ask_marks(page_img: np.ndarray, tokens: list[OcrToken], prov: str, model: str, inch_plan: bool = False,
              effort: str | None = None) -> dict[str, dict]:
    """Retourne des réponses au même format que vlm.ask_vlm (+ "boxes", "token_ids") pour tous les champs."""
    toks = tokens[:MAX_MARKS]
    inch_plan = inch_plan or looks_imperial(toks)
    marked = marked_image(page_img, toks)
    parts: list[Part] = [
        ("text", "Plan :"), ("image", page_img),
        ("text", "Même plan, textes reconnus encadrés et numérotés :"), ("image", marked),
    ]
    for i, c in enumerate(marked_view_crops(marked, page_img)):
        parts += [("text", f"Vue {i + 1} agrandie (mêmes numéros) :"), ("image", c)]
    parts.append(("text", "Textes reconnus (numéro, texte lu par l'OCR) :\n" + token_list(toks) +
                  "\n\nSuis la méthode : identifie les vues, leurs cotes hors-tout, puis désigne pour chaque information "
                  "les numéros des textes qui la portent (liste vide si absent ou incertain)."))
    if "+" in model:  # consensus de plusieurs modèles : « mistral-small-latest+ministral-14b-latest »
        return _consensus(parts, toks, inch_plan, prov, model.split("+"), effort)
    raw = call_vision_json(prov, model, SYSTEM, parts, build_schema(), max_tokens=32000, effort=effort)
    return convert(raw, toks, inch_plan)


def _same_value(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.05, 0.01 * max(abs(a), abs(b)))
    if isinstance(a, list) and isinstance(b, list):
        def sig(lst):
            return sorted(json.dumps({k: v for k, v in it.items() if k in ("diameter_mm", "quantity", "angle_deg")},
                                     sort_keys=True) for it in lst)
        return sig(a) == sig(b)
    return str(a).strip().lower() == str(b).strip().lower()


def _consensus(parts: list[Part], toks: list[OcrToken], inch_plan: bool, prov: str, models: list[str],
               effort: str | None) -> dict[str, dict]:
    """Plusieurs modèles bon marché en parallèle ; on ne garde que les valeurs sur lesquelles ils s'accordent.
    En cas de désaccord : pas de valeur, et l'alerte cite les propositions (l'utilisateur tranche d'un clic)."""
    from concurrent.futures import ThreadPoolExecutor

    from . import vlm as _vlm

    import contextvars
    import logging

    log = logging.getLogger(__name__)

    def one(m):  # le contexte (plan en cours) suit dans les threads, pour les journaux
        raw = call_vision_json(prov, m, SYSTEM, parts, build_schema(), max_tokens=32000, effort=effort)
        return m, convert(raw, toks, inch_plan), _vlm.last_usage() or {}

    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        results = list(pool.map(lambda m: contextvars.copy_context().run(one, m), models))
    # consommation cumulée, rattachée au thread appelant (banc d'essai, journal)
    usages = [u for _, _, u in results]
    _vlm._LAST.usage = {
        "model": "+".join(models), "input_tokens": sum(u.get("input_tokens") or 0 for u in usages),
        "output_tokens": sum(u.get("output_tokens") or 0 for u in usages),
        "cost_usd": round(sum(u.get("cost_usd") or 0 for u in usages), 5) if all(u.get("cost_usd") is not None for u in usages) else None,
        "seconds": max((u.get("seconds") or 0) for u in usages), "cache": all(u.get("cache") for u in usages)}

    first = results[0][1]
    out: dict[str, dict] = {}
    for k, a in first.items():
        props = [(m, r[k]) for m, r, _ in results]
        values = [x["value"] for _, x in props]
        if all(_same_value(values[0], v) for v in values[1:]):
            out[k] = {**a, "confidence": min(x["confidence"] for _, x in props),
                      "evidence": f"accord de {len(models)} modèles — " + (a.get("evidence") or "")}
        else:
            shown = " / ".join(f"{m.split('-latest')[0]} : {json.dumps(v, ensure_ascii=False) if v is not None else 'rien'}"
                               for (m, _), v in zip(props, values))
            log.info("Consensus : désaccord sur %s (%s)", k, shown)
            out[k] = {"value": None, "confidence": 0, "evidence": "désaccord entre modèles", "token_ids": [], "token_texts": [],
                      "boxes": [], "conflict": f"Les IA ne sont pas d'accord ({shown}) : à vérifier"}
    return out


def _valid(ids: list, toks: list[OcrToken]) -> list[int]:
    return [i for i in ids or [] if isinstance(i, int) and 0 <= i < len(toks)]


def _value_of_ids(ids: list[int], toks: list[OcrToken], field: str, inch_plan: bool) -> float | None:
    nums = [n for n in (number_of(toks[i].text, field, inch_plan) for i in ids) if n is not None]
    return nums[0] if nums and max(nums) - min(nums) <= 0.01 * max(nums) + 0.01 else None


def _combine_views(raw: dict, toks: list[OcrToken], inch_plan: bool, out: dict, ans) -> None:
    """Même pièce, plusieurs angles : l'encombrement recombiné à partir des cotes hors-tout de chaque vue
    (dessus = X×Y, face = X×Z, côté = Y×Z). Accord avec la désignation directe -> confirmé ; désaccord -> rien
    (alerte) ; désignation directe absente -> valeur des vues."""
    axes: dict[str, list[tuple[float, int]]] = {"X": [], "Y": [], "Z": []}
    used = []
    for v in raw.get("views") or []:
        if v.get("kind") not in VIEW_AXES:
            continue
        hx, vx = VIEW_AXES[v["kind"]]
        for axis, key in ((hx, "overall_h_ids"), (vx, "overall_v_ids")):
            ids = _valid(v.get(key), toks)
            val = _value_of_ids(ids, toks, "length_mm", inch_plan) if ids else None
            if val:
                axes[axis].append((val, ids[0]))
                used.append(f"{v['kind']} {'horizontale' if key == 'overall_h_ids' else 'verticale'} « {toks[ids[0]].text} »")
    if not all(axes.values()):
        return
    # une direction vue dans deux vues doit avoir la même cote ; sinon on ne conclut pas
    dims = []
    for axis, vals in axes.items():
        if max(v for v, _ in vals) - min(v for v, _ in vals) > 0.02 * max(v for v, _ in vals) + 0.1:
            return
        dims.append(vals[0])
    dims.sort(key=lambda x: -x[0])
    why = "déduit des vues : " + ", ".join(used)
    for name, (val, tid) in zip(("length_mm", "width_mm", "height_mm"), dims):
        direct = out[name]["value"]
        if direct is None:
            out[name] = {**ans(val, 0.75, why, [tid])}
        elif abs(direct - val) > max(0.5, 0.02 * val):
            out[name] = {**ans(None, 0, "", []),
                         "conflict": f"Désignation directe {direct:g} mm ≠ combinaison des vues {val:g} mm : à vérifier"}
        else:
            out[name]["evidence"] = (out[name]["evidence"] or "") + " — confirmé par les vues"


def _check_thickness(raw: dict, toks: list[OcrToken], inch_plan: bool, out: dict, ans) -> None:
    """Une épaisseur incompatible avec la pièce (ex. « 21 » sur une pièce de 60×60) vient d'une mauvaise lecture :
    on essaie l'autre lecture du même texte (OCR local vs Mistral), sinon on ne donne rien."""
    t = out["thickness_mm"]["value"]
    if t is None:
        return
    dims = [out[k]["value"] for k in ("length_mm", "width_mm", "height_mm") if out[k]["value"]]
    limit = min(25.0, 0.2 * min(dims)) if dims else 25.0
    if t <= limit:
        return
    ids = _valid((raw.get("thickness_mm") or {}).get("ids"), toks)
    for i in ids:
        if toks[i].alt:
            alt = number_of(toks[i].alt, "thickness_mm", inch_plan)
            if alt and alt <= limit:
                out["thickness_mm"] = ans(alt, (raw.get("thickness_mm") or {}).get("confidence"),
                                          f"lecture alternative « {toks[i].alt} » (« {toks[i].text} » incompatible avec la pièce)", [i])
                return
    out["thickness_mm"] = {**ans(None, 0, "", []),
                           "conflict": f"Épaisseur lue {t:g} mm incompatible avec une pièce de {min(dims) if dims else '?'} mm : à vérifier"}


def convert(raw: dict, toks: list[OcrToken], inch_plan: bool) -> dict[str, dict]:
    out: dict[str, dict] = {}

    def ans(value, conf, reason, ids):
        return {"value": value, "confidence": float(conf or 0), "evidence": reason or "",
                "token_ids": ids, "token_texts": [toks[i].text for i in ids], "boxes": [toks[i].box for i in ids]}

    for k in SCALAR_FIELDS:
        r = raw.get(k) or {}
        ids = _valid(r.get("ids"), toks)
        val = None
        if ids:
            texts = [toks[i].text for i in ids]
            if k == "designation":
                val = " ".join(texts).strip() or None
            elif k == "material":
                val = next((m for m in (material_of(t) for t in texts) if m), None)
            else:
                # texte illisible (« 🔍 » au lieu de « 6 ») : on essaie l'autre lecture du même texte
                def num(i):
                    n = number_of(toks[i].text, k, inch_plan)
                    return n if n is not None or not toks[i].alt else number_of(toks[i].alt, k, inch_plan)
                nums = [n for n in (num(i) for i in ids) if n is not None]
                if nums and max(nums) - min(nums) <= 0.01 * max(nums) + 0.01:
                    val = nums[0]
                # textes désignés contradictoires -> pas de valeur (mieux vaut vide que faux)
        out[k] = ans(val, r.get("confidence") if val is not None else 0, r.get("reason"), ids if val is not None else [])

    _combine_views(raw, toks, inch_plan, out, ans)
    _check_thickness(raw, toks, inch_plan, out, ans)

    h = raw.get("holes") or {}
    holes, hids = [], []
    for it in h.get("items") or []:
        ids = _valid(it.get("ids"), toks)
        for i in ids[:1]:
            d, q, kind = hole_of(toks[i].text, inch_plan)
            if d and 0.5 <= d <= 500:  # un « trou » hors de cette plage est une mauvaise lecture
                holes.append({"diameter_mm": d, "quantity": int(it.get("quantity") or q or 1), "kind": kind, "tolerance": None})
                hids.append(i)
    out["holes"] = ans(holes if holes else ([] if h.get("items") == [] and (h.get("confidence") or 0) >= 0.8 else None),
                       h.get("confidence"), h.get("reason"), hids)

    b = raw.get("bends") or {}
    count = b.get("count")
    angles = [a for a in (angle_of(toks[i].text) for i in _valid(b.get("angle_ids"), toks)) if a]
    # rayon : uniquement un texte « R… » (un « 45.0° » désigné par erreur ne donne pas R45)
    radii = [n for n in (number_of(toks[i].text.upper().split("R", 1)[1], "r", inch_plan)
                         for i in _valid(b.get("radius_ids"), toks) if re.search(r"R\s*\d|R\s*\.\d", toks[i].text.upper()))
             if n and n < 100]
    if isinstance(count, int) and count > 0:
        ang = angles[0] if angles else None
        bends = [{"angle_deg": ang, "radius_mm": radii[0] if radii else None, "quantity": count}]
        bids = _valid(b.get("angle_ids"), toks) + _valid(b.get("radius_ids"), toks)
        out["bends"] = ans(bends, b.get("confidence"), b.get("reason"), bids)
    elif count == 0:
        out["bends"] = ans([], b.get("confidence"), b.get("reason"), [])
    else:
        out["bends"] = ans(None, 0, b.get("reason"), [])
    return out
