"""Base de données : analyse archivée, décisions classées, devis archivé, paramètres versionnés (SQLite jetable)."""
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app

PLAN = Path(__file__).resolve().parents[1] / "samples" / "piece_003.pdf"


def test_analysis_feedback_quote_roundtrip():
    with TestClient(app) as c:
        assert c.get("/health").json()["database"] is True
        r = c.post("/extract", files={"file": (PLAN.name, PLAN.read_bytes(), "application/pdf")},
                   data={"use_vlm": "false", "preview": "false"})
        assert r.status_code == 200
        ext = r.json()
        aid = ext["analysis_id"]
        assert isinstance(aid, int) and ext["plan_id"]

        machine = ext["fields"]["thickness_mm"]["value"]
        # même valeur -> validation ; autre valeur -> correction (ou ajout si la machine n'avait rien)
        assert c.post("/feedback", json={"analysis_id": aid, "field": "thickness_mm", "value": machine,
                                         "action": "confirmation"}).json()["type"] == "validation"
        t = c.post("/feedback", json={"analysis_id": aid, "field": "thickness_mm", "value": 7.5, "action": "saisie"}).json()
        assert t["type"] == ("ajout" if machine is None else "correction")
        assert c.post("/feedback", json={"analysis_id": 999999, "field": "thickness_mm", "value": 1, "action": "saisie"}).status_code == 404
        assert c.post("/feedback", json={"analysis_id": aid, "field": "x", "value": 1, "action": "saisie"}).status_code == 422
        assert c.post("/feedback", json={"analysis_id": aid, "field": "thickness_mm", "value": 1, "action": "x"}).status_code == 422
        assert c.post("/quote/values", json={"values": {}, "quantity": 0}).status_code == 422
        stats = c.get("/feedback/stats").json()
        assert stats["par_champ"]["thickness_mm"]["correction" if machine is not None else "ajout"] >= 1

        pdf = c.post("/quote/document", json={"extraction": ext, "quantity": 5, "client": "Test"})
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        qid = int(pdf.headers["X-Quote-Id"])
        row = next(q for q in c.get("/quotes").json() if q["id"] == qid)
        assert row["quantite"] == 5 and row["analyse"] == aid and row["parametres_version"]
        assert c.get(f"/quotes/{qid}/pdf").content == pdf.content
        # nom de fichier non ASCII : l'en-tête reste valide
        ext["file_name"] = "pièce стомана.pdf"
        r = c.post("/quote/document", json={"extraction": ext, "quantity": 1})
        assert r.status_code == 200 and "filename*=UTF-8''" in r.headers["content-disposition"]


def test_pricing_versions():
    with TestClient(app) as c:
        base = c.get("/pricing").json()
        v = c.put("/pricing", json={"params": {"margin_rate": 0.3}, "comment": "test marge"}).json()["version"]
        assert c.get("/pricing").json()["margin_rate"] == 0.3
        assert c.get("/pricing/versions").json()[0]["version"] == v
        c.put("/pricing", json={"params": {"margin_rate": base["margin_rate"]}})
        assert c.put("/pricing", json={"params": {"materials": None}}).status_code == 422
        assert c.put("/pricing", json={"params": {"margin_rate": -1}}).status_code == 422
        assert c.put("/pricing", json={"params": {"materials": {"acier": {"price_eur_kg": 2}}}}).status_code == 422
        assert c.get("/pricing").json()["margin_rate"] == base["margin_rate"]
