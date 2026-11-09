"""Plans d'exemple téléchargés (samples/) : extraction sans VLM + chiffrage + API."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app
from devis import pipeline
from devis.costing import compute_quote, load_params

S = Path(__file__).resolve().parents[1] / "samples"


def ext(name):
    p = S / name
    return pipeline.extract(p.read_bytes(), p.name, use_vlm=False)


def test_ufl_vector_pdf_inches():
    v = ext("ufl_sheetmetal_demo.pdf").values()
    assert v["units"] == "in"
    assert v["thickness_mm"] == pytest.approx(0.049 * 25.4, abs=0.01)
    assert v["material"] == "acier"
    assert v["reference"] == "EML2322L-030"
    assert v["designation"] == "SHEETMETAL DEMO"
    assert v["bends"] == [{"angle_deg": 90.0, "radius_mm": None, "quantity": 4}]
    assert v["surface_treatment"] == "aucun (brut)"


def test_umaine_gauge_and_flat_pattern_page():
    spec = ext("umaine_sheetmetal_example2.pdf")
    v = spec.values()
    assert spec.text_source == "pdf-text"
    assert (v["material"], v["material_grade"]) == ("acier", "A36")  # pas "6061-T6" de la consigne
    assert v["thickness_mm"] == pytest.approx(1.519)                 # 16 gauge
    assert v["bends"][0]["angle_deg"] == 90 and v["bends"][0]["radius_mm"] == pytest.approx(2.03, abs=0.01)
    assert v["designation"] == "BRACKET" and v["reference"] == "001"
    assert v["length_mm"] == pytest.approx(76.2)                     # 3.000", pas un repère de grille


def test_quote_defaults_and_sanity():
    q = compute_quote({}, None, load_params())
    assert q.quantity == 10 and q.unit_price > 0
    assert any("Épaisseur inconnue" in h for h in q.assumptions)
    # valeur aberrante -> garde-fou
    q = compute_quote({"thickness_mm": 2, "length_mm": 3000, "width_mm": 3000}, 1, load_params())
    assert any("lourde" in w for w in q.warnings)


def test_quote_tolerates_incomplete_rows():
    # ligne ajoutée dans l'interface mais pas encore remplie
    q = compute_quote({"holes": [{"diameter_mm": None, "quantity": 1}, {"diameter_mm": 6, "quantity": 2}],
                       "bends": [{"angle_deg": None, "radius_mm": None, "quantity": 1}]}, 5, load_params())
    assert q.unit_price > 0 and any("sans diamètre" in h for h in q.assumptions)


def test_quote_realistic_order_of_magnitude():
    # LaserMaster : S235 ép. 4, flan 203.2 x 50, 4 trous Ø6.6, 4 plis, peinture -> quelques euros / pièce
    vals = {"material": "acier", "thickness_mm": 4, "flat_length_mm": 203.2, "flat_width_mm": 50,
            "holes": [{"diameter_mm": 6.6, "quantity": 4}],
            "bends": [{"angle_deg": 90, "radius_mm": 5, "quantity": 3}, {"angle_deg": 45, "radius_mm": 5, "quantity": 1}],
            "surface_treatment": "peinture poudre RAL7001"}
    q = compute_quote(vals, 50, load_params())
    assert q.mass_kg == pytest.approx(0.319, abs=0.01)  # le plan annonce 0,326 kg
    assert 3 < q.unit_price < 20
    assert not q.warnings


def test_api_quote_json_and_pdf():
    c = TestClient(app)
    with open(S / "umaine_sheetmetal_example2.pdf", "rb") as fh:
        r = c.post("/quote", files={"file": ("u.pdf", fh, "application/pdf")}, data={"use_vlm": "false", "quantity": "25"})
    assert r.status_code == 200
    j = r.json()
    assert j["quote"]["quantity"] == 25 and j["extraction"]["values"]["material_grade"] == "A36"
    with open(S / "umaine_sheetmetal_example2.pdf", "rb") as fh:
        r = c.post("/quote", files={"file": ("u.pdf", fh, "application/pdf")}, data={"use_vlm": "false", "format": "pdf"})
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    r = c.post("/quote/values", json={"values": {"thickness_mm": 3, "length_mm": 120, "width_mm": 80}, "quantity": 5})
    assert r.status_code == 200 and r.json()["quantity"] == 5


def test_api_document_from_edited_extraction():
    """Parcours de l'interface : extraction -> correction -> PDF."""
    c = TestClient(app)
    with open(S / "tmlaser_came_contact.png", "rb") as fh:
        ext = c.post("/extract", files={"file": ("t.png", fh, "image/png")},
                     data={"use_vlm": "false", "preview": "true"}).json()
    assert ext["preview_png_b64"] and 0 < ext["preview_scale"] <= 1
    ext["fields"]["thickness_mm"].update(value=4, source="user", confidence=1, issues=[])
    ext["fields"]["holes"].update(value=[{"diameter_mm": 6.6, "quantity": 4}], source="user", confidence=1)
    r = c.post("/quote/document", json={"extraction": ext, "quantity": 20, "client": "ACME"})
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert c.get("/samples").json() and c.get("/samples/..%2Fapi%2Fmain.py").status_code == 404
    assert c.get("/").status_code == 200  # interface servie
