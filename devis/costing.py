"""Chiffrage V1 : règles simples et paramètres par défaut (config/pricing.json).

Coût unitaire = matière + découpe laser + pliage + main d'œuvre + composants + traitement
              + (programmation + réglages pliage) / quantité
Prix unitaire = coût unitaire × (1 + marge)

Toute valeur manquante est remplacée par une valeur par défaut, et c'est tracé dans
`assumptions` pour que l'utilisateur sache quoi vérifier.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field as PField

CONFIG = Path(__file__).resolve().parents[1] / "config" / "pricing.json"


def file_params() -> dict:
    """Paramètres livrés avec le logiciel (config/pricing.json) : valeurs de départ de la base."""
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def current_pricing() -> tuple[Optional[int], dict]:
    """Version en vigueur des paramètres atelier (base de données) ; créée depuis config/pricing.json au 1er appel."""
    try:
        from . import db
        from sqlalchemy import select
        with db.session() as s:
            v = s.scalars(select(db.PricingVersion).order_by(db.PricingVersion.id.desc()).limit(1)).first()
            if v is None:
                v = db.PricingVersion(params=file_params(), comment="valeurs initiales (config/pricing.json)")
                s.add(v)
                s.flush()
            return v.id, dict(v.params)
    except Exception:  # base indisponible : on chiffre quand même avec les valeurs du fichier
        return None, file_params()


def save_pricing(params: dict, comment: Optional[str] = None) -> int:
    """Nouvelle version des paramètres atelier (les devis déjà émis gardent la leur)."""
    from . import db
    with db.session() as s:
        v = db.PricingVersion(params=params, comment=comment)
        s.add(v)
        s.flush()
        return v.id


def load_params(overrides: Optional[dict] = None) -> dict:
    p = current_pricing()[1]
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


class QuoteLine(BaseModel):
    category: str          # matière | machine | main d'œuvre | composants | traitement | frais fixes
    label: str
    detail: str
    unit_cost: float       # € par pièce


class Quote(BaseModel):
    quantity: int
    currency: str = "EUR"
    lines: list[QuoteLine]
    unit_cost: float
    margin_rate: float
    unit_price: float
    total_price: float
    mass_kg: float
    blank_mm: tuple[float, float]
    cut_length_mm: float
    assumptions: list[str] = PField(default_factory=list)
    warnings: list[str] = PField(default_factory=list)


def _match_table(table: dict[str, float], text: str, default: float) -> float:
    for pattern, val in table.items():
        if pattern.startswith("_"):
            continue
        if re.search(pattern, text or "", re.I):
            return val
    return default


def _laser_speed(t: float, table: list[list[float]]) -> float:
    """Vitesse de coupe (m/min) interpolée sur l'épaisseur."""
    if t <= table[0][0]:
        return table[0][1]
    for (t0, v0), (t1, v1) in zip(table, table[1:]):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return table[-1][1]


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


COST_DRIVERS = ("thickness_mm", "material", "length_mm", "width_mm", "flat_length_mm", "flat_width_mm", "holes", "bends")


def compute_quote(values: dict[str, Any], quantity: Optional[int] = None, params: Optional[dict] = None,
                  confidences: Optional[dict[str, float]] = None) -> Quote:
    """`confidences` (optionnel) : confiance de chaque champ extrait, pour signaler un prix
    qui repose sur des valeurs non confirmées."""
    p = params or load_params()
    hyp: list[str] = []
    warn: list[str] = []
    shaky = [f"{k} ({c:.0%})" for k, c in (confidences or {}).items()
             if k in COST_DRIVERS and values.get(k) not in (None, [], "") and c < 0.7]
    if shaky:
        warn.append("Prix basé sur des valeurs non confirmées : " + ", ".join(shaky))
    qty = int(quantity or p["default_quantity"])
    if not quantity:
        hyp.append(f"Quantité par défaut : {qty} pièces")

    # ------------------------------------------------------------ matière
    t = _num(values.get("thickness_mm"))
    if t is None:
        t = p["defaults"]["thickness_mm"]
        hyp.append(f"Épaisseur inconnue : {t} mm par défaut")
    mat = values.get("material") or None
    mat_key = next((k for k in p["materials"] if mat and k in mat.lower()), None)
    if mat_key is None:
        mat_key = p["defaults"]["material"]
        hyp.append(f"Matière {'non reconnue (' + mat + ')' if mat else 'inconnue'} : {mat_key} par défaut")
    m = p["materials"][mat_key]

    bends = values.get("bends") or []
    n_bends = sum(int(b.get("quantity") or 1) for b in bends if b.get("angle_deg") is not None or b.get("quantity"))
    if bends and all(b.get("angle_deg") is None for b in bends):
        hyp.append("Nombre de plis incertain (angles inconnus)")

    # flan développé
    fl, fw = _num(values.get("flat_length_mm")), _num(values.get("flat_width_mm"))
    L, W, H = _num(values.get("length_mm")), _num(values.get("width_mm")), _num(values.get("height_mm"))
    if fl is None or fw is None:
        if L is None:
            L = 100.0
            hyp.append("Dimensions inconnues : flan 100 × 100 mm par défaut")
        W = W or L
        extra = (H - t) if (H and n_bends) else 0.0  # approx. : la hauteur pliée se retrouve dans le développé
        fl = fl or L + extra
        fw = fw or W
        hyp.append(f"Flan développé estimé à {fl:.0f} × {fw:.0f} mm (non coté sur le plan)")
    margin = p["blank_margin_mm"]
    blank_area_m2 = (fl + margin) * (fw + margin) / 1e6
    part_area_m2 = fl * fw / 1e6
    mass = part_area_m2 * t / 1000 * m["density_kg_dm3"] * 1000  # m² × m × kg/m³
    material_cost = blank_area_m2 * t / 1000 * m["density_kg_dm3"] * 1000 * m["price_eur_kg"] * (1 + p["scrap_rate"])

    # ------------------------------------------------------------ découpe laser
    raw_holes = values.get("holes") or []
    holes = [h for h in raw_holes if isinstance(h, dict) and _num(h.get("diameter_mm"))]
    if len(holes) < len(raw_holes):
        hyp.append(f"{len(raw_holes) - len(holes)} ligne(s) de trous sans diamètre ignorée(s)")
    n_holes = sum(int(h.get("quantity") or 1) for h in holes)
    holes_perim = sum(math.pi * float(h["diameter_mm"]) * int(h.get("quantity") or 1) for h in holes)
    cut_len = 2 * (fl + fw) + holes_perim
    speed = _laser_speed(t, p["laser"]["speed_m_min_by_thickness"]) * m["laser_speed_factor"]
    laser_s = cut_len / 1000 / speed * 60 + (1 + n_holes) * p["laser"]["pierce_s"] + p["laser"]["load_unload_s_per_part"]
    laser_cost = laser_s / 3600 * p["laser"]["rate_eur_h"]

    # ------------------------------------------------------------ pliage
    bend_s = n_bends * p["bending"]["s_per_bend"]
    bend_cost = bend_s / 3600 * p["bending"]["rate_eur_h"]
    bend_types = len({(b.get("angle_deg"), b.get("radius_mm")) for b in bends}) if n_bends else 0
    bend_setup = bend_types * p["bending"]["setup_min_per_bend_type"] / 60 * p["bending"]["rate_eur_h"]

    # ------------------------------------------------------------ composants
    comps = values.get("components") or []
    comp_cost = sum(_match_table(p["components"], c.get("designation", ""), p["components"]["_default_eur"])
                    * int(c.get("quantity") or 1) for c in comps)
    n_comp = sum(int(c.get("quantity") or 1) for c in comps)

    # ------------------------------------------------------------ main d'œuvre
    lab = p["labor"]
    labor_min = lab["deburr_handling_min_per_part"] + lab["inspection_min_per_part"] + n_comp * lab["insert_s_per_component"] / 60
    labor_cost = labor_min / 60 * lab["rate_eur_h"]

    # ------------------------------------------------------------ traitement
    treat = values.get("surface_treatment") or p["defaults"]["surface_treatment"]
    if not values.get("surface_treatment"):
        hyp.append("Traitement de surface non indiqué : pièce brute")
    eur_m2 = _match_table(p["surface_treatment_eur_m2"], treat, 0)
    treat_cost = max(part_area_m2 * 2 * eur_m2, p["surface_treatment_min_eur_part"]) if eur_m2 else 0.0

    # ------------------------------------------------------------ frais fixes (amortis sur la série)
    prog_cost = p["programming"]["cam_min_per_order"] / 60 * p["programming"]["rate_eur_h"]
    fixed_unit = (prog_cost + bend_setup) / qty

    lines = [
        QuoteLine(category="matière", label=f"Tôle {mat_key} {values.get('material_grade') or ''} ép. {t:g} mm".replace("  ", " "),
                  detail=f"flan {fl + margin:.0f}×{fw + margin:.0f} mm, {m['price_eur_kg']:.2f} €/kg, chute {p['scrap_rate']:.0%}",
                  unit_cost=material_cost),
        QuoteLine(category="machine", label="Découpe laser",
                  detail=f"{cut_len / 1000:.2f} m à {speed:.1f} m/min, {1 + n_holes} amorçages, {laser_s:.0f} s × {p['laser']['rate_eur_h']} €/h",
                  unit_cost=laser_cost),
    ]
    if n_bends:
        lines.append(QuoteLine(category="machine", label="Pliage presse plieuse",
                               detail=f"{n_bends} pli(s) × {p['bending']['s_per_bend']} s × {p['bending']['rate_eur_h']} €/h",
                               unit_cost=bend_cost))
    if comps:
        lines.append(QuoteLine(category="composants", label="Composants rapportés",
                               detail=", ".join(f"{c.get('quantity', 1)}× {c.get('designation')}" for c in comps),
                               unit_cost=comp_cost))
    lines.append(QuoteLine(category="main d'œuvre", label="Ébavurage, manutention, contrôle" + (", sertissage" if n_comp else ""),
                           detail=f"{labor_min:.1f} min × {lab['rate_eur_h']} €/h", unit_cost=labor_cost))
    if treat_cost:
        lines.append(QuoteLine(category="traitement", label=f"Traitement : {treat}",
                               detail=f"{part_area_m2 * 2:.3f} m² (2 faces) × {eur_m2} €/m²", unit_cost=treat_cost))
    lines.append(QuoteLine(category="frais fixes", label="Programmation + réglages (amortis)",
                           detail=f"({prog_cost:.0f} € prog. + {bend_setup:.0f} € réglage pliage) / {qty} pcs",
                           unit_cost=fixed_unit))

    for ln in lines:
        ln.unit_cost = round(ln.unit_cost, 3)
    unit_cost = sum(ln.unit_cost for ln in lines)
    unit_price = round(unit_cost * (1 + p["margin_rate"]), 2)

    # ------------------------------------------------------------ garde-fous (fiabilité)
    s = p["sanity"]
    if mass > 0:
        # hors frais fixes : sur une petite série de petites pièces, ils dominent légitimement
        eur_kg = (unit_cost - fixed_unit) * (1 + p["margin_rate"]) / mass
        if not s["price_eur_kg_min"] <= eur_kg <= s["price_eur_kg_max"]:
            warn.append(f"Prix au kilo atypique ({eur_kg:.1f} €/kg hors frais fixes, attendu "
                        f"{s['price_eur_kg_min']}–{s['price_eur_kg_max']}) : vérifier dimensions/épaisseur")
    if mass > s.get("mass_kg_max", 25):
        warn.append(f"Masse estimée {mass:.1f} kg : pièce inhabituellement lourde, vérifier les dimensions")
    if unit_price < s["unit_price_min_eur"]:
        warn.append(f"Prix unitaire très bas ({unit_price} €) : vérifier les données")

    return Quote(
        quantity=qty, currency=p["currency"], lines=lines, unit_cost=round(unit_cost, 2),
        margin_rate=p["margin_rate"], unit_price=unit_price, total_price=round(unit_price * qty, 2),
        mass_kg=round(mass, 3), blank_mm=(round(fl + margin, 1), round(fw + margin, 1)),
        cut_length_mm=round(cut_len, 1), assumptions=hyp, warnings=warn,
    )
