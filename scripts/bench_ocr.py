"""Benchmark des moteurs OCR sur piece_003.pdf (vérité terrain : tests/fixtures/piece_003_truth.json).

    python scripts/bench_ocr.py [moteur ...]

Mesures (OCR + règles, sans IA) :
- annotations lues : part des 28 annotations du plan retrouvées dans le texte reconnu ;
- champs justes : valeurs extraites par le parseur comparées à la vérité terrain ;
- temps.
Écrit le tableau dans out/bench_ocr.md.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from devis.ocr import ENGINES, normalize  # noqa: E402
from devis.pipeline import extract  # noqa: E402

TRUTH = json.loads((ROOT / "tests/fixtures/piece_003_truth.json").read_text(encoding="utf-8"))
PLAN = ROOT / "samples" / "piece_003.pdf"


def compact(s: str) -> str:
    return re.sub(r"[^0-9A-Z.Ø+()]", "", normalize(s).upper())


def annotation_recall(tokens) -> tuple[int, list[str]]:
    pieces = [(compact(t.text), {compact(p) for p in t.text.split()}) for t in tokens]
    used = [0] * len(pieces)
    found, missed = 0, []
    for ann in TRUTH["annotations"]:
        a = compact(ann)
        short = re.fullmatch(r"[\d.]{1,4}", a) is not None
        hit = None
        for i, (whole, words) in enumerate(pieces):
            if used[i] >= (len(words) if short else 1):
                continue
            if (short and (a == whole or a in words)) or (not short and a in whole):
                hit = i
                break
        if hit is None:
            missed.append(ann)
        else:
            if short:  # une cote courte (« 20 ») consomme le token ; un texte long peut en contenir plusieurs
                used[hit] += 1
            found += 1
    return found, missed


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def field_score(values: dict) -> dict[str, str]:
    """✓ juste, ✗ faux, — absent."""
    t = TRUTH["fields"]
    out = {}

    def num(key):
        got, exp = _num(values.get(key)), t[key]
        out[key] = "—" if got is None else ("✓" if abs(got - exp) <= 0.5 else f"✗ {got:g}")

    for k in ("thickness_mm", "length_mm", "width_mm", "height_mm"):
        num(k)
    holes = values.get("holes") or []
    got = sorted((round(float(h["diameter_mm"]), 2), int(h.get("quantity") or 1)) for h in holes)
    exp = sorted((h["diameter_mm"], h["quantity"]) for h in t["holes"])
    out["holes"] = "—" if not got else ("✓" if got == exp else "✗ " + ", ".join(f"{q}×Ø{d:g}" for d, q in got))
    cuts = values.get("cutouts") or []
    got = sorted((tuple(float(x) for x in re.findall(r"\d+(?:\.\d+)?", c["description"])[:2]), c.get("quantity", 1)) for c in cuts)
    exp = sorted(((5.0, 5.0), 2), ((20.0, 20.0), 2)) if False else sorted([((5.0, 5.0), 2), ((20.0, 20.0), 2)])
    out["cutouts"] = "—" if not got else ("✓" if got == exp else f"✗ {len(got)} lu(s)")
    bends = values.get("bends") or []
    n = sum(int(b.get("quantity") or 1) for b in bends if b.get("angle_deg") is not None)
    out["bends"] = "—" if not bends else ("✓" if n == 2 and all(b.get("angle_deg") == 90 for b in bends) else
                                         f"✗ R{bends[0].get('radius_mm')} seul" if n == 0 else f"✗ {n} pli(s)")
    comps = values.get("components") or []
    ok = any("M6" in c["designation"].upper() and c.get("quantity") == 2 for c in comps)
    out["components"] = "—" if not comps else ("✓" if ok else "✗")
    tol = values.get("tolerance_standard")
    out["tolerance_standard"] = "—" if not tol else ("✓" if tol == t["tolerance_standard"] else f"✗ {tol}")
    des = values.get("designation") or ""
    out["designation"] = "—" if not des else ("✓" if "SUPPORT" in des.upper() else f"✗ {des}")
    return out


def main(engines: list[str], use_vlm: bool = False):
    data = PLAN.read_bytes()
    rows, details = [], []
    for e in engines:
        t0 = time.time()
        try:
            spec = extract(data, PLAN.name, use_vlm=use_vlm, ocr_engine=e)
        except Exception as ex:  # moteur indisponible (clé, dépendance…)
            rows.append((e, f"erreur : {ex}"))
            continue
        dt = time.time() - t0
        found, missed = annotation_recall(spec.ocr_tokens)
        fs = field_score(spec.values())
        ok = sum(v == "✓" for v in fs.values())
        rows.append((e, found, len(TRUTH["annotations"]), ok, len(fs), dt, fs))
        details.append(f"### {e}\n- annotations manquées : {', '.join(missed) or 'aucune'}\n"
                       f"- champs : " + " · ".join(f"{k} {v}" for k, v in fs.items()))
        print(f"{e:14s} annotations {found}/{len(TRUTH['annotations'])}  champs {ok}/{len(fs)}  {dt:5.1f}s  manquées: {missed}")

    keys = ["thickness_mm", "length_mm", "width_mm", "height_mm", "holes", "cutouts", "bends", "components", "tolerance_standard", "designation"]
    md = ["| moteur | annotations lues | champs justes (sans IA) | temps | " + " | ".join(keys) + " |",
          "|---|---|---|---|" + "---|" * len(keys)]
    for r in rows:
        if len(r) == 2:
            md.append(f"| {r[0]} | {r[1]} |")
            continue
        e, found, tot, ok, n, dt, fs = r
        md.append(f"| {e} | {found}/{tot} | {ok}/{n} | {dt:.0f} s | " + " | ".join(fs[k] for k in keys) + " |")
    out = ROOT / "out" / ("bench_ocr_vlm.md" if use_vlm else "bench_ocr.md")
    out.parent.mkdir(exist_ok=True)
    out.write_text("# Benchmark OCR — piece_003.pdf\n\n" + "\n".join(md) + "\n\n" + "\n\n".join(details) + "\n", encoding="utf-8")
    print("\n" + "\n".join(md))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args or list(ENGINES), use_vlm="--vlm" in sys.argv)
