"""Journal d'apprentissage : ce que le logiciel a proposé, et ce que l'utilisateur en a fait.

But : réapprendre plus tard à partir des cas où le logiciel n'était pas sûr (ou s'est trompé).
Tout est en base (devis/db.py) :
  plans      le fichier du plan (contenu gardé sauf si DEVIS_LEARNING=0)
  analyses   la proposition de la machine pour une analyse (champs, textes lus, IA utilisée)
  decisions  une ligne par action de l'utilisateur sur un champ :
      validation   la valeur proposée est juste (« C'est correct », ou ressaisie à l'identique)
      correction   la valeur proposée était fausse et a été remplacée
      infirmation  la valeur proposée était fausse et a été effacée (l'information n'existe pas)
      ajout        la machine n'avait rien trouvé, l'utilisateur a fourni la valeur
  Chaque décision garde la proposition de la machine (valeur, source, confiance, alertes, journal de décision)
  et indique si la machine était incertaine (champ « à vérifier »).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from sqlalchemy import select

from . import db


def enabled() -> bool:
    return os.getenv("DEVIS_LEARNING", "1") != "0"


def plan_id(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def save_analysis(data: bytes, file_name: str, spec_json: dict, duration_s: float | None = None) -> int:
    """Enregistre le plan (une fois par contenu) et la proposition de la machine. Renvoie l'id de l'analyse."""
    pid = plan_id(data)
    result = {k: v for k, v in spec_json.items() if k not in ("preview_png_b64", "plan_id", "analysis_id")}
    with db.session() as s:
        plan = s.get(db.Plan, pid)
        if plan is None:
            s.add(db.Plan(id=pid, file_name=file_name, size_bytes=len(data), content=data if enabled() else None))
        elif plan.content is None and enabled():
            plan.content = data
        a = db.Analysis(plan_id=pid, duration_s=duration_s, text_source=spec_json.get("text_source"),
                        vlm_provider=spec_json.get("vlm_provider"), vlm_error=spec_json.get("vlm_error"),
                        review_fields=list(spec_json.get("needs_review") or []), result=result)
        s.add(a)
        s.flush()
        return a.id


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == []


def _norm(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def unsure(f: dict) -> bool:
    """Même règle que l'interface : champ « à vérifier »."""
    return f.get("source") != "user" and (
        _empty(f.get("value")) or f.get("confidence", 0) < 0.7 or bool(f.get("issues")) or f.get("source") == "vlm")


def classify(machine_value: Any, value: Any, ui_action: str) -> str:
    if ui_action == "confirmation":
        return "validation"
    if _norm(machine_value) == _norm(value):
        return "validation"
    if _empty(value):
        return "validation" if _empty(machine_value) else "infirmation"
    return "ajout" if _empty(machine_value) else "correction"


def record(analysis_id: int, field: str, value: Any, ui_action: str, detail: dict | None = None) -> dict:
    """Enregistre une action utilisateur. Lève LookupError si l'analyse n'existe pas."""
    with db.session() as s:
        a = s.get(db.Analysis, analysis_id)
        if a is None:
            raise LookupError(analysis_id)
        m = (a.result.get("fields") or {}).get(field) or {}
        d = db.Decision(analysis_id=analysis_id, field=field, type=classify(m.get("value"), value, ui_action),
                        ui_action=ui_action, machine_unsure=unsure(m),
                        machine={k: m.get(k) for k in ("value", "source", "confidence", "issues", "evidence", "trace")},
                        user_value=value, detail=detail or {})
        if enabled():
            s.add(d)
        return {"type": d.type, "machine_incertaine": d.machine_unsure}


def latest_decisions() -> list[db.Decision]:
    """Dernière décision pour chaque (analyse, champ) : c'est elle qui fait foi."""
    with db.session() as s:
        rows = s.scalars(select(db.Decision).order_by(db.Decision.id)).all()
    last: dict[tuple[int, str], db.Decision] = {}
    for d in rows:
        last[(d.analysis_id, d.field)] = d
    return list(last.values())


def stats() -> dict:
    last = latest_decisions()
    by_field: dict[str, dict[str, int]] = {}
    for d in last:
        c = by_field.setdefault(d.field, {"validation": 0, "correction": 0, "infirmation": 0, "ajout": 0, "incertain": 0})
        c[d.type] += 1
        c["incertain"] += int(d.machine_unsure)
    return {"analyses": len({d.analysis_id for d in last}), "decisions": len(last), "par_champ": by_field}
