"""Contrôles de cohérence métier (tôlerie).

Un champ qui viole une règle reçoit un `issue` et voit sa confiance plafonnée :
il repart alors au VLM (ou en revue humaine s'il vient déjà du VLM).
"""
from __future__ import annotations

from .schemas import Field

CAP = 0.5


def _flag(f: Field, msg: str) -> None:
    if msg not in f.issues:
        f.issues.append(msg)
        f.trace.append({"etape": "controle", "alerte": msg, "confiance_avant": f.confidence,
                        "confiance_apres": min(f.confidence, CAP)})
    f.confidence = min(f.confidence, CAP)


def _num(fields: dict[str, Field], k: str):
    v = fields.get(k, Field()).value
    return float(v) if isinstance(v, (int, float)) else None


def run_checks(fields: dict[str, Field]) -> list[str]:
    warnings: list[str] = []
    t = _num(fields, "thickness_mm")
    L, W, H = _num(fields, "length_mm"), _num(fields, "width_mm"), _num(fields, "height_mm")
    fl, fw = _num(fields, "flat_length_mm"), _num(fields, "flat_width_mm")

    if t is not None and not 0.3 <= t <= 25:
        _flag(fields["thickness_mm"], f"Épaisseur {t} mm hors plage tôlerie (0,3–25 mm)")

    for k, v in (("length_mm", L), ("width_mm", W), ("flat_length_mm", fl), ("flat_width_mm", fw)):
        if v is not None and not 1 <= v <= 4000:
            _flag(fields[k], f"{k} = {v} mm hors format tôle (≤ 4000 mm)")

    if L is not None and W is not None and W > L:
        _flag(fields["width_mm"], "Largeur > longueur : dimensions inversées ?")
    if H is not None and t is not None and H < t:
        _flag(fields["height_mm"], "Hauteur inférieure à l'épaisseur")
    if fl is not None and L is not None and fl < 0.8 * L:
        _flag(fields["flat_length_mm"], "Flan développé plus court que la pièce finie")

    holes = fields["holes"].value or []
    lim = min([v for v in (L, W) if v is not None], default=None)
    for h in holes:
        if lim is not None and h.diameter_mm >= lim:
            _flag(fields["holes"], f"Trou Ø{h.diameter_mm} ≥ dimension de la pièce ({lim} mm)")
        if t is not None and h.kind == "through" and h.diameter_mm < 0.8 * t:
            warnings.append(f"Trou Ø{h.diameter_mm} < épaisseur : poinçonnage impossible, perçage/laser requis")
        if h.quantity <= 0 or h.quantity > 500:
            _flag(fields["holes"], f"Quantité de trous incohérente ({h.quantity})")

    for b in fields["bends"].value or []:
        if b.angle_deg is not None and not 0 < b.angle_deg <= 180:
            _flag(fields["bends"], f"Angle de pli {b.angle_deg}° impossible")
        if b.radius_mm is not None and t is not None and not 0.3 * t <= b.radius_mm <= 10 * t:
            warnings.append(f"Rayon de pli R{b.radius_mm} atypique pour une épaisseur de {t} mm")
    if fields["bends"].value and H is not None and t is not None and H <= t * 1.01:
        _flag(fields["height_mm"], "Pièce pliée mais hauteur = épaisseur : hauteur probablement fausse")

    return warnings
