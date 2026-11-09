"""Importe l'ancien journal d'apprentissage en fichiers (data/learning/) dans la base de données.

.venv/Scripts/python scripts/migrate_learning_json.py            # base de DATABASE_URL (ou SQLite par défaut)
Les fichiers ne sont ni modifiés ni supprimés. Relancer le script n'importe pas deux fois la même analyse.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from sqlalchemy import select  # noqa: E402

from devis import db, learning  # noqa: E402

SRC = ROOT / "data" / "learning"


def main() -> None:
    if not (SRC / "runs").is_dir():
        print("Rien à importer (data/learning/runs absent).")
        return
    analyses, decisions = {}, 0
    with db.session() as s:
        for run_path in sorted((SRC / "runs").glob("*.json")):
            pid = run_path.stem
            run = json.loads(run_path.read_text(encoding="utf-8"))
            date = datetime.fromisoformat(run["date"]) if run.get("date") else None
            if s.scalars(select(db.Analysis).where(db.Analysis.plan_id == pid, db.Analysis.created_at == date)).first():
                continue  # déjà importée
            plan_file = next((SRC / "plans").glob(f"{pid}.*"), None)
            if s.get(db.Plan, pid) is None:
                data = plan_file.read_bytes() if plan_file else None
                s.add(db.Plan(id=pid, file_name=run.get("file_name") or pid, size_bytes=len(data or b""), content=data,
                              **({"created_at": date} if date else {})))
            result = {k: v for k, v in run.items() if k not in ("date", "plan_id")}
            a = db.Analysis(plan_id=pid, text_source=run.get("text_source"), vlm_provider=run.get("vlm_provider"),
                            vlm_error=run.get("vlm_error"), review_fields=list(run.get("needs_review") or []),
                            result=result, **({"created_at": date} if date else {}))
            s.add(a)
            s.flush()
            analyses[pid] = a.id
        fb = SRC / "feedback.jsonl"
        for line in (fb.read_text(encoding="utf-8").splitlines() if fb.exists() else []):
            if not line.strip():
                continue
            x = json.loads(line)
            aid = analyses.get(x["plan_id"])
            if aid is None:
                continue  # analyse déjà importée lors d'un passage précédent
            s.add(db.Decision(analysis_id=aid, field=x["champ"], type=x["type"], ui_action=x["action_interface"],
                              machine_unsure=bool(x["machine_incertaine"]), machine=x["machine"],
                              user_value=x["utilisateur"], detail=x.get("detail") or {},
                              created_at=datetime.fromisoformat(x["date"])))
            decisions += 1
    print(f"{len(analyses)} analyse(s) et {decisions} décision(s) importées dans {db.database_url().split('@')[-1]}")
    print(json.dumps(learning.stats(), ensure_ascii=False))


if __name__ == "__main__":
    main()
