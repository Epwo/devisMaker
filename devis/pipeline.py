"""Plan -> OCR -> règles -> contrôles -> (VLM sur les champs incertains) -> fusion -> contrôles."""
from __future__ import annotations

import base64
import os
import logging
import re
import time
from typing import Any

from pydantic import BaseModel

from . import vlm
from .checks import run_checks

from .ingest import load_pages, pdf_text_tokens, raster_coverage, to_png
from .ocr import default_engine, normalize, run_ocr, strip_border
from .parser import parse
from .schemas import FIELD_SPECS, Bend, Box, Component, Cutout, Field, Hole, OcrToken, PartSpec, Source

log = logging.getLogger(__name__)

OCR_CONF_THRESHOLD = float(os.getenv("DEVIS_OCR_CONF_THRESHOLD", "0.85"))
VLM_CONF_CAP = 0.9  # une valeur VLM seule n'est jamais "certaine" sans relecture

LIST_MODELS = {"holes": Hole, "cutouts": Cutout, "bends": Bend, "components": Component}


def uncertain_fields(fields: dict[str, Field]) -> list[str]:
    return [k for k, f in fields.items() if f.missing or f.confidence < OCR_CONF_THRESHOLD or f.issues]


def _plain(v: Any) -> Any:
    if isinstance(v, list):
        return [x.model_dump() if isinstance(x, BaseModel) else x for x in v]
    return v


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in LIST_MODELS:
        return [LIST_MODELS[name](**x) for x in value]
    return value


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.5, 0.02 * max(abs(a), abs(b)))
    if isinstance(a, str) and isinstance(b, str):
        x, y = a.lower().replace(" ", ""), b.lower().replace(" ", "")
        return x in y or y in x
    if isinstance(a, list) and isinstance(b, list):
        def sig(lst):
            out = []
            for it in _plain(lst):
                if isinstance(it, dict):
                    out.append(tuple(sorted((k, v) for k, v in it.items() if k in {"diameter_mm", "quantity", "angle_deg"})))
                else:
                    out.append(str(it))
            return sorted(out)
        return sig(a) == sig(b)
    return a == b


def _item_key(name: str, it: Any) -> Any:
    d = it.model_dump() if isinstance(it, BaseModel) else dict(it)
    if name == "holes":
        return round(float(d.get("diameter_mm") or 0), 1)
    if name == "bends":
        return d.get("angle_deg")
    if name == "cutouts":
        return tuple(re.findall(r"\d+(?:\.\d+)?", str(d.get("description", "")))[:2])
    if name == "components":
        m = re.search(r"\bM\d+\b", str(d.get("designation", "")).upper())
        return m.group(0) if m else str(d.get("designation", "")).upper()[:12]
    return str(d)


def _describe(name: str, items: list) -> str:
    out = []
    for it in _plain(items):
        q = it.get("quantity", 1)
        if name == "holes":
            out.append(f"{q}×Ø{it.get('diameter_mm'):g}")
        elif name == "bends":
            out.append(f"{q}×{it.get('angle_deg')}°")
        else:
            out.append(f"{q}× {it.get('description') or it.get('designation')}")
    return ", ".join(out)


def _merge_list(ocr: Field, name: str, v_val: list, v_conf: float, evidence: str) -> Field:
    """Listes lues explicitement par l'OCR (« 2X Ø8.75 ») : on les garde ; ce que l'IA ajoute en plus
    devient une suggestion à vérifier, sans entrer dans le prix (l'IA confond par ex. un chanfrein 5x5 et un trou Ø5)."""
    o_keys = {_item_key(name, i) for i in ocr.value}
    v_keys = {_item_key(name, i) for i in v_val}
    extra = [i for i in v_val if _item_key(name, i) not in o_keys]
    unconfirmed = [i for i in ocr.value if _item_key(name, i) not in v_keys]
    issues = []
    if extra:
        issues.append(f"L'IA voit aussi : {_describe(name, extra)} (non ajouté) : à vérifier")
    if unconfirmed:
        issues.append(f"Lu par l'OCR mais non vu par l'IA : {_describe(name, unconfirmed)}")
    confirmed = not extra and not unconfirmed
    return ocr.model_copy(update={
        "confidence": max(ocr.confidence, OCR_CONF_THRESHOLD) if confirmed else min(max(ocr.confidence, 0.6), 0.65),
        "source": Source.OCR_VLM if not unconfirmed else Source.OCR,
        "issues": issues,
        "evidence": f"{ocr.evidence} | VLM : {evidence}",
    })


def merge(ocr: Field, name: str, ans: dict) -> Field:
    f, decision = _merge_core(ocr, name, ans)
    if ans.get("conflict") and ans.get("conflict") not in f.issues:  # l'IA s'est contredite (cf. vlm.apply_views) ou désaccord entre modèles
        kept = [i for i in f.issues if not i.startswith("Introuvable sur le plan (OCR + VLM)")]
        f = f.model_copy(update={"issues": [*kept, ans["conflict"]], "confidence": min(f.confidence, 0.5)})
        decision += " — l'IA se contredit (réponse directe ≠ calcul par les vues) : confiance abaissée, à vérifier"
    ia = {"etape": "ia", "fournisseur": ans.get("_provider") or vlm.provider_label(), "valeur_proposee": _plain(ans.get("value")),
          "confiance_annoncee": ans.get("confidence"), "justification": ans.get("evidence")}
    if ans.get("token_texts") is not None:  # stratégie « pointage » : textes OCR désignés par l'IA
        ia["textes_designes"] = ans["token_texts"]
    if ans.get("views"):
        ia["lecture_par_vues"] = ans["views"]
    if ans.get("conflict"):
        ia["contradiction"] = ans["conflict"]
    fusion = {"etape": "fusion", "decision": decision, "valeur": _plain(f.value), "source": f.source.value,
              "confiance": round(f.confidence, 3), "alertes": list(f.issues)}
    return f.model_copy(update={"trace": [*ocr.trace, ia, fusion]})


def _merge_core(ocr: Field, name: str, ans: dict) -> tuple[Field, str]:
    v_val = _coerce(name, ans.get("value"))
    v_conf = min(float(ans.get("confidence") or 0), VLM_CONF_CAP)
    evidence = ans.get("evidence") or ""
    if v_val is None or v_val == []:
        if ocr.missing:
            return (Field(source=Source.NONE, evidence=f"VLM : {evidence}" if evidence else None,
                          issues=["Introuvable sur le plan (OCR + VLM) : à saisir"]),
                    "Ni l'OCR ni l'IA ne trouvent cette information : à saisir")
        return ocr, "L'IA ne propose rien : la lecture OCR est conservée"
    if ocr.missing:
        return (Field(value=v_val, confidence=v_conf, source=Source.VLM, evidence=evidence,
                      boxes=list(ans.get("boxes") or []),
                      box=_union(ans.get("boxes")) if ans.get("boxes") else None),
                f"L'OCR n'avait rien trouvé : valeur de l'IA retenue (confiance plafonnée à {VLM_CONF_CAP}), à confirmer")
    if name in LIST_MODELS and ocr.confidence >= 0.5 and not _same(ocr.value, v_val):
        return (_merge_list(ocr, name, v_val, v_conf, evidence),
                "Liste : les éléments lus explicitement par l'OCR sont conservés ; les ajouts de l'IA sont signalés sans entrer dans le prix")
    if name == "bends" and ocr.confidence < 0.5:
        # OCR partiel (rayon lu dans les notes) : l'IA complète nombre et angle ; on garde le rayon lu
        r = next((b.radius_mm for b in ocr.value if b.radius_mm), None)
        for b in v_val:
            b.radius_mm = b.radius_mm or r
        consistent = all(b.radius_mm is None or r is None or abs(b.radius_mm - r) < 0.3 for b in v_val)
        return (Field(value=v_val, confidence=min(v_conf, 0.8 if consistent else 0.6), source=Source.VLM,
                      evidence=f"{ocr.evidence} | VLM : {evidence}", box=ocr.box, boxes=ocr.boxes,
                      issues=[] if consistent else ["Rayon proposé par l'IA différent de la note du plan"]),
                "OCR partiel (rayon lu dans les notes) : nombre et angle des plis fournis par l'IA, rayon de la note conservé")
    if _same(ocr.value, v_val):
        # OCR et VLM d'accord : on garde la lecture OCR (traçable) et on lève les doutes.
        return (ocr.model_copy(update={
            "confidence": max(ocr.confidence, v_conf, OCR_CONF_THRESHOLD),
            "source": Source.OCR_VLM, "issues": [], "evidence": f"{ocr.evidence} | VLM : {evidence}",
        }), "L'OCR et l'IA sont d'accord : la lecture OCR est confirmée")
    # Désaccord : on retient le VLM (l'OCR était déjà jugé incertain) et on demande une relecture.
    return (Field(
        value=v_val, confidence=min(v_conf, 0.6), source=Source.VLM, evidence=evidence,
        vlm_value=None, box=ocr.box, boxes=ocr.boxes,  # zone où l'OCR avait lu : utile pour vérifier
        issues=[f"OCR lisait {_plain(ocr.value)!r}, VLM lit {_plain(v_val)!r} : à vérifier"],
    ), "L'OCR et l'IA sont en désaccord : l'OCR était déjà incertain, la valeur de l'IA est retenue mais à vérifier")


def _union(boxes) -> Box:
    return Box(x0=min(b.x0 for b in boxes), y0=min(b.y0 for b in boxes), x1=max(b.x1 for b in boxes), y1=max(b.y1 for b in boxes))


def _numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d*\.?\d+", normalize(text))]


def locate_values(fields: dict[str, Field], tokens: list[OcrToken], inch: bool = False, max_zones: int = 3) -> None:
    """Les valeurs proposées par l'IA n'ont pas de position (le modèle regarde l'image entière).
    On cherche le(s) texte(s) lu(s) sur le plan qui portent cette valeur — « 22 » pour la hauteur,
    « 2 » pour l'épaisseur — pour pouvoir montrer d'où elle vient probablement."""
    k = 25.4 if inch else 1.0

    def num_hits(v: float) -> list[OcrToken]:
        exact, inside = [], []
        for t in tokens:
            nums = _numbers(t.text)
            if not any(abs(n * k - v) < 0.05 for n in nums):
                continue
            # un texte qui n'est QUE ce nombre (une cote) passe avant une phrase qui le contient
            (exact if re.fullmatch(r"\(?\s*\d*\.?\d+\s*\)?", normalize(t.text)) else inside).append(t)
        return (exact or inside)[:max_zones]

    def text_hits(v: str) -> list[OcrToken]:
        key = re.sub(r"\s+", " ", v.strip().lower())
        if len(key) < 3:
            return []
        return [t for t in tokens if key in t.text.lower() or (len(t.text) > 3 and t.text.lower() in key)][:max_zones]

    for name, f in fields.items():
        if f.boxes or f.box or f.missing:
            continue
        v = f.value
        hits: list[OcrToken] = []
        if isinstance(v, (int, float)):
            hits = num_hits(float(v))
        elif isinstance(v, str):
            hits = text_hits(v)
        elif isinstance(v, list) and name == "holes":
            for h in v:
                d = h.diameter_mm if isinstance(h, BaseModel) else h.get("diameter_mm")
                hits += [t for t in tokens if "Ø" in t.text and any(abs(n * k - d) < 0.05 for n in _numbers(t.text))][:2]
        if hits:
            f.boxes = [t.box for t in hits]
            f.box = Box(x0=min(b.x0 for b in f.boxes), y0=min(b.y0 for b in f.boxes),
                        x1=max(b.x1 for b in f.boxes), y1=max(b.y1 for b in f.boxes))
            f.evidence = (f.evidence + " | " if f.evidence else "") + "zone probable : « " + " », « ".join(t.text for t in hits) + " »"
            f.trace.append({"etape": "localisation", "explication": "valeur retrouvée parmi les textes lus sur le plan",
                            "textes": [t.text for t in hits]})


PREVIEW_MAX_SIDE = 1800


def extract(data: bytes, file_name: str, use_vlm: bool = True, with_preview: bool = False,
            ocr_engine: str | None = None, vlm_strategy: str | None = None,
            vlm_provider: str | None = None, vlm_model: str | None = None, vlm_effort: str | None = None) -> PartSpec:
    """vlm_strategy : « lecture » (l'IA lit les valeurs) ou « pointage » (l'IA désigne les textes OCR qui
    portent chaque information ; la valeur vient de l'OCR). Défaut : DEVIS_VLM_STRATEGY ou « pointage »."""
    t0 = time.time()
    log.info("Analyse de %s (%d Ko, IA %s)", file_name, len(data) // 1024, "oui" if use_vlm else "non")
    pages = load_pages(data, file_name)
    page = pages[0]  # V1 : une page = un plan
    vector = pdf_text_tokens(data, file_name)
    warnings: list[str] = []
    # PDF vectoriel (CAO) : texte exact. Mais un scan collé dans un PDF (image > 50 % de la page) doit passer à l'OCR,
    # même si la page porte quelques lignes de texte (pied de page, en-tête).
    if vector and len(vector[0]) >= 5 and raster_coverage(data, file_name) < 0.5:
        # PDF exporté de CAO : le texte est exact, pas besoin d'OCR. On lit toutes les pages
        # (le développé et ses notes de pli sont souvent en page 2), cartouche répété exclu.
        h, w = page.shape[:2]
        first = {(t.text, round(t.box.x0), round(t.box.y0)) for t in vector[0]}
        tokens = []
        for i, toks in enumerate(vector):
            for t in strip_border(toks, w, h):
                if i and (t.text, round(t.box.x0), round(t.box.y0)) in first:
                    continue
                box = t.box.model_copy(update={"y0": t.box.y0 + i * h, "y1": t.box.y1 + i * h})
                tokens.append(t.model_copy(update={"text": normalize(t.text), "box": box}))
        text_source = "pdf-text"
        if len(vector) > 1:
            warnings.append(f"{len(vector)} pages : texte lu sur toutes, image envoyée au VLM = page 1")
    else:
        engine = ocr_engine or default_engine()
        try:
            tokens = run_ocr(page, engine)
        except Exception as e:  # moteur distant indisponible (clé, réseau, quota) : repli local
            if engine == "rapid_v4":
                log.exception("OCR local en échec")
                raise
            log.warning("OCR « %s » indisponible (%s) : repli sur l'OCR local", engine, e)
            warnings.append(f"OCR « {engine} » indisponible ({e}) : OCR local utilisé")
            engine = "rapid_v4"
            tokens = run_ocr(page, engine)
        text_source = "ocr" if engine == "rapid_v4" else f"ocr:{engine}"
        if len(pages) > 1:
            warnings.append(f"{len(pages)} pages : seule la première est analysée")
    log.info("Texte lu : %d textes (%s) en %.1f s", len(tokens), text_source, time.time() - t0)
    fields = parse(tokens)
    warnings += run_checks(fields)

    spec = PartSpec(file_name=file_name, fields=fields, ocr_tokens=tokens, warnings=warnings, text_source=text_source)
    if with_preview:
        # Aperçu pour l'interface : les boîtes OCR sont dans le repère de `page`, d'où l'échelle.
        side = max(page.shape[:2])
        spec.preview_scale = min(1.0, PREVIEW_MAX_SIDE / side)
        spec.preview_png_b64 = base64.b64encode(to_png(page, PREVIEW_MAX_SIDE)).decode()
    todo = uncertain_fields(spec.fields)
    spec.vlm_fields_requested = todo
    _trace_routing(spec, todo, use_vlm, text_source)
    if not use_vlm or not todo:
        return _finish(spec)

    known = {k: _plain(f.value) for k, f in fields.items() if k not in todo and not f.missing}
    try:
        prov = vlm_provider or vlm.provider()
        model = vlm_model or vlm.default_model(prov)
        strategy = vlm_strategy or os.getenv("DEVIS_VLM_STRATEGY", "pointage")
        def run(prov, model):
            if strategy == "pointage":
                from .vlm_marks import ask_marks

                return ask_marks(page, tokens, prov, model, inch_plan=fields["units"].value == "in", effort=vlm_effort)
            return vlm.ask_vlm(page, todo, tokens, known, prov, model, vlm_effort)

        try:
            answers = run(prov, model)
        except vlm.VlmError as first:
            # Claude indisponible (crédit, réseau…) et clé Mistral présente : on bascule plutôt que d'abandonner
            other = "mistral" if prov == "claude" else "claude"
            if vlm_provider or not (os.getenv("MISTRAL_API_KEY") if other == "mistral" else vlm._claude_key()):
                raise
            log.warning("IA %s indisponible (%s) : bascule sur %s", prov, first, other)
            spec.warnings.append(f"IA {prov} indisponible ({first}) : bascule sur {other}")
            prov, model = other, vlm.default_model(other)
            answers = run(prov, model)
    except Exception as e:  # VlmError (API) ou réponse inexploitable : on garde la lecture OCR
        spec.vlm_error = str(e) if isinstance(e, vlm.VlmError) else f"{type(e).__name__}: {e}"
        if isinstance(e, vlm.VlmError):
            log.error("IA indisponible, lecture OCR seule conservée : %s", e)
        else:
            log.exception("Réponse de l'IA inexploitable, lecture OCR seule conservée")
        for k in todo:
            spec.fields[k].trace.append({"etape": "ia", "erreur": str(e), "consequence": "lecture OCR conservée telle quelle"})
        return _finish(spec)

    spec.vlm_used = True
    spec.vlm_provider = vlm.provider_label(prov, model) + f" — {strategy}"
    fields = spec.fields  # pydantic a copié le dict à la construction de PartSpec
    for name in todo:
        if name in answers:
            ans = answers[name]
            if name in vlm.DIM_FIELDS and answers.get("views"):
                ans = {**ans, "views": answers["views"]}  # pour le journal : lecture vue par vue de l'IA
            fields[name] = merge(fields[name], name, {**ans, "_provider": spec.vlm_provider})
    locate_values(fields, tokens, inch=fields["units"].value == "in")
    # Les contrôles repassent sur les valeurs fusionnées : un VLM incohérent est rattrapé ici.
    for f in fields.values():
        f.issues = [i for i in f.issues if "hors" not in i and "incohérent" not in i]
    spec.warnings = [w for w in spec.warnings if w.startswith("IA ")] + run_checks(fields)
    return _finish(spec)


def _trace_routing(spec: PartSpec, todo: list[str], use_vlm: bool, text_source: str) -> None:
    """Journal : pourquoi un champ est (ou n'est pas) envoyé à l'IA."""
    for k, f in spec.fields.items():
        if k in todo:
            why = ("absent" if f.missing else f"confiance {f.confidence:.2f} < seuil {OCR_CONF_THRESHOLD}"
                   if f.confidence < OCR_CONF_THRESHOLD else "alerte : " + "; ".join(f.issues))
            f.trace.append({"etape": "routage", "texte_source": text_source,
                            "decision": "envoyé à l'IA" if use_vlm else "aurait été envoyé à l'IA (IA désactivée)",
                            "raison": why})
        else:
            f.trace.append({"etape": "routage", "texte_source": text_source, "decision": "pas d'IA",
                            "raison": f"lecture jugée sûre (confiance {f.confidence:.2f} ≥ {OCR_CONF_THRESHOLD}, aucune alerte)"})


def _finish(spec: PartSpec) -> PartSpec:
    """Dernière étape du journal : la valeur retenue et ce qu'il reste à faire."""
    review = set(spec.needs_review())
    found = sum(1 for f in spec.fields.values() if not f.missing)
    log.info("Résultat : %d/%d informations trouvées, %d à vérifier (%s) ; IA : %s", found, len(spec.fields), len(review),
             ", ".join(sorted(review)) or "aucune", spec.vlm_provider or ("erreur" if spec.vlm_error else "non utilisée"))
    for k, f in spec.fields.items():
        f.trace.append({"etape": "resultat", "valeur": _plain(f.value), "source": f.source.value,
                        "confiance": round(f.confidence, 3), "alertes": list(f.issues),
                        "a_verifier": k in review})
    return spec

def make_quote(data: bytes, file_name: str, quantity: int | None = None, use_vlm: bool = True,
               client: str | None = None, params: dict | None = None):
    """Plan -> (PartSpec, Quote, PDF du devis)."""
    from .costing import compute_quote, load_params
    from .document import render_quote_pdf
    from .ingest import to_png

    spec = extract(data, file_name, use_vlm=use_vlm)
    conf = {k: f.confidence for k, f in spec.fields.items() if not f.missing}
    quote = compute_quote(spec.values(), quantity, params or load_params(), conf)
    thumb = to_png(load_pages(data, file_name)[0], 1400)
    pdf = render_quote_pdf(spec, quote, thumb, client)
    return spec, quote, pdf


__all__ = ["extract", "make_quote", "uncertain_fields", "FIELD_SPECS"]
