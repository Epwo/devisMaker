"""Banc d'essai : stratégies × modèles de vision sur les plans de samples/ (vérité : tests/fixtures/benchmark_truth.json).

    python scripts/bench_vlm.py                 # tout
    python scripts/bench_vlm.py --only ocr_seul pointage:claude-opus-5-5
    python scripts/bench_vlm.py --plans piece_003.pdf

Notation par champ : ✓ juste (+1) · ✗ FAUX (−2) · — vide (0).
« Mieux vaut aucune valeur qu'une fausse » : un ✗ coûte plus qu'un — ; precision = justes / (justes + faux).
Résultats : out/bench_vlm.md (+ out/bench_vlm.json avec toutes les valeurs).
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from devis.pipeline import extract  # noqa: E402
from devis import vlm  # noqa: E402
from devis.vlm_marks import material_of  # noqa: E402

TRUTH = json.loads((ROOT / "tests/fixtures/benchmark_truth.json").read_text(encoding="utf-8"))["plans"]
SAMPLES = ROOT / "samples"

MODELS = [
    ("claude", "claude-opus-5-5"), ("claude", "claude-sonnet-5-5"), ("claude", "claude-haiku-5-5"),
    ("claude", "claude-fable-5-1"), ("mistral", "mistral-large-4"), ("mistral", "mistral-medium-latest"),
    ("mistral", "mistral-small-latest"), ("mistral", "ministral-14b-latest"), ("mistral", "ministral-8b-latest"),
]
# (stratégie, fournisseur, modèle, effort, moteur OCR) ; effort = réflexion Claude ; OCR None = moteur par défaut
CONSENSUS = "mistral-small-latest+ministral-14b-latest"
OCR_ENGINES = ["rapid_v4", "ppocr_v5", "ppocr_v5_srv", "mistral_board", "mistral"]
CONFIGS = [("ocr_seul", None, None, None, None)] + [(s, p, m, None, None) for p, m in MODELS for s in ("lecture", "pointage")] + [
    ("pointage", "claude", m, e, None) for m in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5") for e in ("low", "medium")] + [
    # consensus : plusieurs modèles bon marché, on ne garde que les valeurs sur lesquelles ils s'accordent
    ("pointage", "mistral", m, None, None) for m in (CONSENSUS, "mistral-small-latest+mistral-medium-latest",
                                                     "mistral-small-latest+ministral-14b-latest+ministral-8b-latest")] + [
    # comparatif des moteurs OCR : seul, puis avec l'IA par défaut (consensus)
    (s, p, m, None, o) for o in OCR_ENGINES for s, p, m in (("ocr_seul", None, None), ("pointage", "mistral", CONSENSUS))]
FIELDS = ["designation", "material", "thickness_mm", "length_mm", "width_mm", "height_mm",
          "flat_length_mm", "flat_width_mm", "holes", "bends"]


def cfg_name(c) -> str:
    base = c[0] if c[0] == "ocr_seul" else f"{c[0]}:{c[2]}" + (f"@{c[3]}" if c[3] else "")
    return base + (f" [ocr={c[4]}]" if c[4] else "")


# --------------------------------------------------------------------------- notation

def score_field(k: str, exp, got) -> tuple[str, str]:
    """('ok'|'faux'|'vide', détail)."""
    empty = got is None or got == "" or got == []
    if exp is None:  # l'information n'est pas sur le plan
        return ("ok", "vide attendu") if empty else ("faux", f"inventé : {got}")
    if k == "holes":
        if exp == []:
            return ("ok", "aucun trou") if empty else ("faux", f"{got}")
        if empty:
            return "vide", ""
        g = sorted((round(float(h["diameter_mm"]), 1), int(h.get("quantity") or 1)) for h in got)
        e = sorted((round(float(d), 1), int(q)) for d, q in exp)
        return ("ok", "") if g == e else ("faux", ", ".join(f"{q}×Ø{d:g}" for d, q in g))
    if k == "bends":
        if empty or all(b.get("quantity") is None for b in got):  # nombre de plis inconnu = pas de valeur
            return "vide", ""
        n = sum(int(b.get("quantity") or 1) for b in got)
        angs = {round(float(b["angle_deg"])) for b in got if b.get("angle_deg") is not None}
        if n != exp["count"]:
            return "faux", f"{n} pli(s)"
        if exp.get("angles") and angs and not angs <= {round(a) for a in exp["angles"]} | {180 - round(a) for a in exp["angles"]}:
            return "faux", f"angles {sorted(angs)}"
        return "ok", ""
    if empty:
        return "vide", ""
    if k == "material":
        fam = material_of(str(got)) or str(got).lower()
        ok = fam == exp or (exp == "acier galvanisé" and fam == "acier")
        return ("ok", "") if ok else ("faux", str(got))
    if k == "designation":
        a, b = str(exp).lower(), str(got).lower()
        return ("ok", "") if (a in b or (len(b) >= 4 and b in a)) else ("faux", str(got))
    try:
        g = float(got)
    except (TypeError, ValueError):
        return "faux", str(got)
    return ("ok", "") if abs(g - exp) <= max(0.5, 0.02 * exp) else ("faux", f"{g:g}")


# --------------------------------------------------------------------------- exécution

def run_one(plan: str, cfg) -> dict:
    strategy, prov, model, effort, ocr = cfg
    data = (SAMPLES / plan).read_bytes()
    t0 = time.time()
    err = None
    for attempt in range(3):
        try:
            spec = extract(data, plan, use_vlm=strategy != "ocr_seul",
                           vlm_strategy=None if strategy == "ocr_seul" else strategy, vlm_provider=prov, vlm_model=model,
                           vlm_effort=effort, ocr_engine=ocr)
            err = spec.vlm_error
            if err and re.search(r"429|rate|limit|overloaded|529|timeout", err, re.I) and attempt < 2:
                time.sleep(15 * (attempt + 1))
                continue
            break
        except Exception as e:  # noqa: BLE001
            err, spec = f"{type(e).__name__}: {e}", None
            break
    dt = time.time() - t0
    values = spec.values() if spec else {}
    usage = vlm.last_usage() if strategy != "ocr_seul" else None
    res = {"plan": plan, "config": cfg_name(cfg), "time_s": round(dt, 1), "error": err, "fields": {}, "usage": usage,
           "ocr": OCR_STATS.get((ocr or "auto", plan)), "text_source": spec.text_source if spec else None}
    for k, exp in TRUTH[plan]["fields"].items():
        verdict, detail = score_field(k, exp, values.get(k))
        res["fields"][k] = {"verdict": verdict, "detail": detail, "got": values.get(k), "expected": exp,
                            "source": spec.fields[k].source.value if spec else None}
    return res


OCR_STATS: dict[tuple[str, str], dict] = {}


def main():
    args = sys.argv[1:]
    only = args[args.index("--only") + 1:] if "--only" in args else None
    if only and "--plans" in only:
        only = only[:only.index("--plans")]
    plans = args[args.index("--plans") + 1:] if "--plans" in args else list(TRUTH)
    for flag in ("--only", "--merge"):
        if flag in plans:
            plans = plans[:plans.index(flag)]
    if only and "--merge" in only:
        only = only[:only.index("--merge")]
    configs = [c for c in CONFIGS if not only or cfg_name(c) in only or c[0] in only]
    sys.stdout.reconfigure(encoding="utf-8")

    # 1) OCR seul, séquentiel, par moteur : chauffe le cache OCR et mesure temps + pages Mistral OCR
    from devis import ocr_mistral

    results = []
    engines = list(dict.fromkeys(c[4] for c in configs))
    for eng in engines:
        cfg = ("ocr_seul", None, None, None, eng)
        for p in plans:
            before = ocr_mistral.PAGES_SENT[0]
            r = run_one(p, cfg)
            pages = ocr_mistral.PAGES_SENT[0] - before
            OCR_STATS[(eng or "auto", p)] = {"seconds": r["time_s"], "mistral_pages": pages,
                                             "cost_usd": round(pages * ocr_mistral.PRICE_PER_PAGE_USD, 4)}
            r["ocr"] = OCR_STATS[(eng or "auto", p)]
            print(f"[ocr {eng or 'auto':13s}] {p:34s} {r['time_s']:5.1f}s  pages Mistral OCR : {pages}")
            if cfg in configs:
                results.append(r)
    # 2) IA : en parallèle
    jobs = [(p, c) for c in configs if c[0] != "ocr_seul" for p in plans]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(run_one, p, c): (p, c) for p, c in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            results.append(r)
            v = [x["verdict"] for x in r["fields"].values()]
            print(f"[{i:3d}/{len(jobs)}] {r['config']:38s} {r['plan']:34s} ✓{v.count('ok')} ✗{v.count('faux')} —{v.count('vide')} "
                  f"{r['time_s']:5.1f}s {('ERR ' + r['error'][:60]) if r['error'] else ''}")
    if "--merge" in sys.argv:  # remplace seulement les configurations rejouées, garde les autres
        old = json.loads((ROOT / "out" / "bench_vlm.json").read_text(encoding="utf-8"))["results"]
        redone = {(r["config"], r["plan"]) for r in results}
        results = [r for r in old if (r["config"], r["plan"]) not in redone] + results
    report(results, plans)


def report(results: list[dict], plans: list[str]):
    by_cfg: dict[str, list[dict]] = {}
    for r in results:
        by_cfg.setdefault(r["config"], []).append(r)
    rows = []
    for cfg, rs in by_cfg.items():
        v = [f["verdict"] for r in rs for f in r["fields"].values()]
        ok, bad, empty = v.count("ok"), v.count("faux"), v.count("vide")
        costs = [r["usage"]["cost_usd"] for r in rs if (r.get("usage") or {}).get("cost_usd") is not None]
        ocr_s = [r["ocr"]["seconds"] for r in rs if r.get("ocr") and r.get("text_source", "").startswith("ocr")]
        ocr_c = [r["ocr"]["cost_usd"] for r in rs if r.get("ocr")]
        ai_s = [r["usage"]["seconds"] for r in rs if (r.get("usage") or {}).get("seconds") is not None]
        rows.append({"config": cfg, "ok": ok, "faux": bad, "vide": empty, "n": len(v), "score": ok - 2 * bad,
                     "precision": ok / (ok + bad) if ok + bad else 0, "couverture": (ok + bad) / len(v) if v else 0,
                     "temps_moyen": sum(ai_s) / len(ai_s) if ai_s else sum(r["time_s"] for r in rs) / len(rs),
                     "cout_plan": sum(costs) / len(costs) if costs else None,
                     "ocr_s": sum(ocr_s) / len(ocr_s) if ocr_s else None,
                     "ocr_cout": sum(ocr_c) / len(ocr_c) if ocr_c else None,
                     "erreurs": sum(1 for r in rs if r["error"])})
    rows.sort(key=lambda x: (-x["score"], -x["precision"]))
    md = ["# Banc d'essai — compréhension des plans", "",
          f"{len(plans)} plans, {sum(len(TRUTH[p]['fields']) for p in plans)} champs notés par configuration. "
          "✓ juste (+1) · ✗ faux (−2) · — vide (0). Precision = justes / valeurs données.", "",
          "| rang | configuration | score | ✓ | ✗ faux | — vide | precision | couverture | temps IA / plan | coût IA / plan "
          "| temps OCR (scans) | coût OCR / plan | erreurs |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(rows, 1):
        cost = f"{x['cout_plan'] * 100:.2f} c$" if x.get("cout_plan") is not None else "?"
        ocr_s = f"{x['ocr_s']:.0f} s" if x.get("ocr_s") is not None else "?"
        ocr_c = f"{x['ocr_cout'] * 100:.2f} c$" if x.get("ocr_cout") is not None else "?"
        ia_s = f"{x['temps_moyen']:.0f} s" if not x["config"].startswith("ocr_seul") else "—"
        md.append(f"| {i} | {x['config']} | **{x['score']}** | {x['ok']} | {x['faux']} | {x['vide']} | "
                  f"{x['precision']:.0%} | {x['couverture']:.0%} | {ia_s} | {cost if ia_s != '—' else '—'} "
                  f"| {ocr_s} | {ocr_c} | {x['erreurs']} |")
    # détail par plan pour les 4 meilleures configurations + OCR seul
    best = [x["config"] for x in rows[:4]] + (["ocr_seul"] if "ocr_seul" in by_cfg and "ocr_seul" not in [x["config"] for x in rows[:4]] else [])
    sym = {"ok": "✓", "faux": "✗", "vide": "—"}
    for p in plans:
        md += ["", f"### {p}", f"_{TRUTH[p].get('source', '')}_", "",
               "| champ | attendu | " + " | ".join(best) + " |", "|---|---|" + "---|" * len(best)]
        for k, exp in TRUTH[p]["fields"].items():
            cells = []
            for cfg in best:
                r = next((r for r in by_cfg.get(cfg, []) if r["plan"] == p), None)
                if not r:
                    cells.append("")
                    continue
                f = r["fields"][k]
                cells.append(sym[f["verdict"]] + (f" {f['detail']}" if f["verdict"] == "faux" else ""))
            md.append(f"| {k} | {json.dumps(exp, ensure_ascii=False) if not isinstance(exp, (int, float, str)) else exp} | " + " | ".join(cells) + " |")
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    (out / "bench_vlm.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out / "bench_vlm.json").write_text(json.dumps({"summary": rows, "results": results}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n" + "\n".join(md[:len(rows) + 6]))


if __name__ == "__main__":
    main()
