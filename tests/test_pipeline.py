"""Tests hors-ligne : OCR réel sur piece_003, VLM simulé."""
from pathlib import Path

import pytest

from devis import pipeline, vlm
from devis.schemas import Field, Source

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "samples" / "piece_003.pdf"


@pytest.fixture(scope="module")
def ocr_only():
    return pipeline.extract(PLAN.read_bytes(), PLAN.name, use_vlm=False)


def test_ocr_reads_explicit_annotations(ocr_only):
    v = ocr_only.values()
    holes = {(h["diameter_mm"], h["quantity"]) for h in v["holes"]}
    assert holes == {(8.75, 2), (7.5, 2)}
    # une zone par annotation lue (pour la surligner sur le plan), pas une seule enveloppe
    assert len(ocr_only.fields["holes"].boxes) == 2
    assert v["tolerance_standard"] == "ISO 2768-m"
    assert v["designation"] == "SUPPORT"
    assert v["bends"][0]["radius_mm"] == 2.0
    assert v["components"][0]["quantity"] == 2


def test_uncertain_fields_go_to_vlm(ocr_only):
    todo = ocr_only.vlm_fields_requested
    for k in ("thickness_mm", "bends", "length_mm", "material", "reference"):
        assert k in todo
    for k in ("tolerance_standard", "designation", "notes"):
        assert k not in todo


def test_schema_is_strict():
    s = vlm.build_schema(["thickness_mm", "holes", "bends"])
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"thickness_mm", "holes", "bends", "views"}  # + lecture vue par vue
    hole_item = s["properties"]["holes"]["properties"]["value"]["items"]
    assert hole_item["additionalProperties"] is False


def test_bbox_from_views_and_conflict():
    views = [{"view": "dessus", "overall_horizontal_mm": 60, "overall_vertical_mm": 60, "evidence": ""},
             {"view": "face", "overall_horizontal_mm": 60, "overall_vertical_mm": 22, "evidence": ""}]
    dims, _ = vlm.bbox_from_views(views)
    assert dims == [60, 60, 22]
    ans = vlm.apply_views({"views": views, "width_mm": {"value": 42, "confidence": 0.9, "evidence": ""},
                           "height_mm": {"value": 22, "confidence": 0.9, "evidence": ""}}, ["width_mm", "height_mm"])
    assert ans["width_mm"]["value"] == 60 and ans["width_mm"]["conflict"]   # contradiction -> à vérifier
    assert ans["height_mm"]["value"] == 22 and "conflict" not in ans["height_mm"]
    f = pipeline.merge(Field(), "width_mm", ans["width_mm"])
    assert f.issues and f.confidence <= 0.5


def test_list_merge_keeps_explicit_ocr_callouts():
    from devis.schemas import Hole
    ocr = Field(value=[Hole(diameter_mm=8.75, quantity=2), Hole(diameter_mm=7.5, quantity=2)], confidence=0.82, source=Source.OCR)
    ans = {"value": [{"diameter_mm": 5, "quantity": 2, "kind": "through", "tolerance": None},
                     {"diameter_mm": 8.75, "quantity": 2, "kind": "through", "tolerance": None},
                     {"diameter_mm": 7.5, "quantity": 2, "kind": "through", "tolerance": None}], "confidence": 0.9, "evidence": ""}
    f = pipeline.merge(ocr, "holes", ans)
    assert {h.diameter_mm for h in f.value} == {8.75, 7.5}            # le Ø5 de l'IA n'entre pas dans le prix
    assert any("Ø5" in i for i in f.issues)                            # mais il est signalé


def test_locate_ai_values_on_plan():
    from devis.schemas import Box, OcrToken
    toks = [OcrToken(text="22", conf=1, box=Box(x0=10, y0=10, x1=30, y1=20)),
            OcrToken(text="TOLERANCES ISO 2768 -m 22", conf=1, box=Box(x0=0, y0=50, x1=90, y1=60)),
            OcrToken(text="NO PAINT ON THREADING", conf=1, box=Box(x0=0, y0=80, x1=90, y1=90))]
    fields = {"height_mm": Field(value=22, confidence=0.9, source=Source.VLM),
              "surface_treatment": Field(value="no paint on threading", confidence=0.9, source=Source.VLM),
              "flat_length_mm": Field(value=97, confidence=0.7, source=Source.VLM)}
    pipeline.locate_values(fields, toks)
    assert len(fields["height_mm"].boxes) == 1 and fields["height_mm"].boxes[0].x0 == 10  # la cote seule, pas la phrase
    assert fields["surface_treatment"].boxes
    assert not fields["flat_length_mm"].boxes  # valeur calculée : pas de zone inventée


def test_trace_explains_each_value(ocr_only):
    t = ocr_only.fields["holes"].trace
    steps = [s["etape"] for s in t]
    assert steps[0] == "lecture" and steps[-1] == "resultat" and "routage" in steps
    assert any("2X Ø8.75" in x["texte"] for x in t[0]["textes_lus"])          # texte OCR à l'origine
    assert "Trous" in t[0]["regle"]                                           # règle appliquée
    missing = ocr_only.fields["thickness_mm"].trace
    assert "aucune règle" in missing[0]["resultat"]
    assert any(s["etape"] == "routage" and s["raison"] == "absent" for s in missing)


def test_trace_records_ai_and_fusion_decision():
    ocr = Field(value=60.0, confidence=0.5, source=Source.OCR, trace=[{"etape": "lecture", "valeur": 60}])
    f = pipeline.merge(ocr, "length_mm", {"value": 60, "confidence": 0.9, "evidence": "cote 60"})
    assert [s["etape"] for s in f.trace] == ["lecture", "ia", "fusion"]
    assert "d'accord" in f.trace[-1]["decision"]
    f = pipeline.merge(ocr, "length_mm", {"value": 75, "confidence": 0.9, "evidence": ""})
    assert "désaccord" in f.trace[-1]["decision"]


def test_merge_rules():
    # OCR vide -> VLM
    f = pipeline.merge(Field(), "thickness_mm", {"value": 2, "confidence": 0.97, "evidence": "cote 2"})
    assert f.value == 2 and f.source == Source.VLM and f.confidence == pipeline.VLM_CONF_CAP
    # OCR incertain + VLM d'accord -> confirmé
    ocr = Field(value=60.0, confidence=0.5, source=Source.OCR, issues=["à confirmer"])
    f = pipeline.merge(ocr, "length_mm", {"value": 60, "confidence": 0.9, "evidence": ""})
    assert f.source == Source.OCR_VLM and f.confidence >= 0.85 and not f.issues
    # Désaccord -> VLM retenu mais à relire
    f = pipeline.merge(ocr, "length_mm", {"value": 75, "confidence": 0.9, "evidence": ""})
    assert f.value == 75 and f.issues and f.confidence <= 0.6
    # Personne ne sait -> à saisir
    f = pipeline.merge(Field(), "client", {"value": None, "confidence": 0, "evidence": "cartouche masqué"})
    assert f.missing and f.issues


def test_full_pipeline_with_fake_vlm(monkeypatch):
    def fake(page, names, tokens, known, *a, **k):
        ans = {n: {"value": None, "confidence": 0.0, "evidence": "non visible"} for n in names}
        ans["thickness_mm"] = {"value": 2, "confidence": 0.95, "evidence": "cote 2 vue de profil"}
        ans["height_mm"] = {"value": 22, "confidence": 0.95, "evidence": "cote 22"}
        ans["width_mm"] = {"value": 9000, "confidence": 0.9, "evidence": "erreur volontaire"}
        ans["bends"] = {"value": [{"angle_deg": 90, "radius_mm": 2, "quantity": 2}], "confidence": 0.9, "evidence": "profil en Z"}
        return ans

    monkeypatch.setattr(vlm, "ask_vlm", fake)
    spec = pipeline.extract(PLAN.read_bytes(), PLAN.name, use_vlm=True, vlm_strategy="lecture", vlm_provider="claude")
    f = spec.fields
    assert spec.vlm_used
    assert f["thickness_mm"].value == 2 and f["thickness_mm"].source == Source.VLM
    assert f["bends"].value[0].quantity == 2
    # Valeur VLM aberrante rattrapée par les contrôles métier
    assert f["width_mm"].issues and f["width_mm"].confidence <= 0.5
    assert "width_mm" in spec.needs_review()
    # Introuvable -> à saisir
    assert f["client"].missing and "client" in spec.needs_review()


def test_marks_value_conversion():
    from devis.vlm_marks import hole_of, material_of, number_of
    assert number_of("40/10", "thickness_mm", False) == 4.0          # notation tôlerie
    assert number_of("16 GAUGE A36 STEEL", "thickness_mm", False) == 1.519
    assert number_of("4-7/8", "flat_length_mm", False) == 123.825     # fraction de pouce
    assert abs(number_of('1/16" Sheet Metal', "thickness_mm", False) - 1.5875) < 0.002
    assert number_of(".060", "thickness_mm", True) == 1.524           # plan en pouces
    assert number_of("T=1.6MM", "thickness_mm", True) == 1.6          # « mm » explicite prime
    assert material_of("AL5052-H32") == "aluminium" and material_of("SS316 T=1.0MM") == "inox"
    assert material_of("Acier Galvanisé") == "acier galvanisé" and material_of("ACIER S235 ép 8mm") == "acier"
    assert hole_of("6XØ12.7", False)[:2] == (12.7, 6) and hole_of("2X Ø8.75 +0.1", False)[:2] == (8.75, 2)


def test_marks_contradictory_picks_give_no_value():
    from devis.schemas import Box, OcrToken
    from devis.vlm_marks import convert
    toks = [OcrToken(text="60", conf=1, box=Box(x0=0, y0=0, x1=1, y1=1)), OcrToken(text="42", conf=1, box=Box(x0=0, y0=0, x1=1, y1=1))]
    raw = {k: {"ids": [], "confidence": 0, "reason": ""} for k in
           ["designation", "material", "thickness_mm", "length_mm", "width_mm", "height_mm", "flat_length_mm", "flat_width_mm"]}
    raw["width_mm"] = {"ids": [0, 1], "confidence": 0.9, "reason": ""}   # 60 et 42 désignés ensemble : ambigu
    raw["length_mm"] = {"ids": [0], "confidence": 0.9, "reason": ""}
    raw["holes"] = {"items": [], "confidence": 0.3, "reason": ""}
    raw["bends"] = {"count": None, "angle_ids": [], "radius_ids": [], "confidence": 0, "reason": ""}
    out = convert(raw, toks, False)
    assert out["width_mm"]["value"] is None and out["length_mm"]["value"] == 60
    assert out["holes"]["value"] is None and out["bends"]["value"] is None  # incertain -> rien


def test_marks_thickness_and_imperial_fixes():
    from devis.schemas import Box, OcrToken
    from devis.vlm_marks import looks_imperial, number_of
    assert number_of("ACIER S235 ép 8mm", "thickness_mm", False) == 8.0     # pas 235
    assert number_of("0", "thickness_mm", False) is None                    # lecture absurde -> rien
    b = Box(x0=0, y0=0, x1=1, y1=1)
    assert looks_imperial([OcrToken(text="4-7/8", conf=1, box=b)])
    assert not looks_imperial([OcrToken(text="60", conf=1, box=b)])


def test_marks_consensus_keeps_only_agreements(monkeypatch):
    from devis import vlm_marks
    from devis.schemas import Box, OcrToken
    toks = [OcrToken(text="60", conf=1, box=Box(x0=0, y0=0, x1=1, y1=1)),
            OcrToken(text="42", conf=1, box=Box(x0=0, y0=0, x1=1, y1=1)),
            OcrToken(text="2", conf=1, box=Box(x0=0, y0=0, x1=1, y1=1))]
    empty = {"ids": [], "confidence": 0, "reason": ""}

    def raw(width_id):
        r = {k: dict(empty) for k in vlm_marks.SCALAR_FIELDS}
        r["length_mm"] = {"ids": [0], "confidence": 0.9, "reason": ""}
        r["width_mm"] = {"ids": [width_id], "confidence": 0.9, "reason": ""}
        r["thickness_mm"] = {"ids": [2], "confidence": 0.9, "reason": ""}
        r["holes"] = {"items": [], "confidence": 0.2, "reason": ""}
        r["bends"] = {"count": None, "angle_ids": [], "radius_ids": [], "confidence": 0, "reason": ""}
        return r

    monkeypatch.setattr(vlm_marks, "call_vision_json", lambda prov, m, *a, **k: raw(0 if m == "a" else 1))
    out = vlm_marks._consensus([], toks, False, "mistral", ["a", "b"], None)
    assert out["length_mm"]["value"] == 60 and out["thickness_mm"]["value"] == 2   # d'accord
    assert out["width_mm"]["value"] is None and "pas d'accord" in out["width_mm"]["conflict"]  # désaccord -> vide


def test_unreadable_reading_falls_back_to_alt():
    """davoc_fixation_ardoise.jpg : Mistral lit « 🔍 » un « 6 » vertical que l'OCR local lit bien."""
    from devis.schemas import Box, OcrToken
    from devis.vlm_marks import convert

    toks = [OcrToken(text="🔍", alt="6", conf=0.9, box=Box(x0=0, y0=0, x1=10, y1=20), vertical=True),
            OcrToken(text="300", conf=0.9, box=Box(x0=50, y0=0, x1=80, y1=10), vertical=False)]
    out = convert({"thickness_mm": {"ids": [0], "confidence": 0.9}, "length_mm": {"ids": [1], "confidence": 0.9}}, toks, False)
    assert out["thickness_mm"]["value"] == 6


def test_no_ai_key_falls_back_to_ocr(monkeypatch):
    """Sans aucune clé IA : lecture OCR seule, avertissement clair, pas d'erreur (régression : « + » in None)."""
    monkeypatch.setattr(vlm, "provider", lambda: None)
    spec = pipeline.extract(PLAN.read_bytes(), PLAN.name, use_vlm=True)
    assert spec.vlm_error is None and not spec.vlm_used
    assert any("IA non configurée" in w for w in spec.warnings)


@pytest.mark.parametrize("forced, claude, mistral, expected", [
    ("auto", "k", "k", "claude"),
    ("auto", "", "k", "mistral"),        # pas de clé Claude : Mistral
    ("claude", "", "k", "mistral"),      # Claude demandé mais sans clé : Mistral plutôt qu'une erreur
    ("mistral", "k", "k", "mistral"),
    ("auto", "", "", None),              # aucune clé : lecture OCR seule
])
def test_provider_selection(monkeypatch, forced, claude, mistral, expected):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEVIS_VLM_PROVIDER", forced)
    monkeypatch.setenv("CLAUDE_API_KEY", claude)
    monkeypatch.setenv("MISTRAL_API_KEY", mistral)
    assert vlm.provider() == expected
