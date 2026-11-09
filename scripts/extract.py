"""CLI : python scripts/extract.py plan.pdf [--no-vlm] [--json] [--quote [--qty N] [--pdf out.pdf]]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from devis.pipeline import extract, make_quote  # noqa: E402


def print_spec(spec) -> None:
    print(f"Plan : {spec.file_name}   texte : {spec.text_source} ({len(spec.ocr_tokens)} tokens)")
    print(f"VLM {'utilisé' if spec.vlm_used else 'à solliciter'} pour : {', '.join(spec.vlm_fields_requested) or '—'}")
    if spec.vlm_error:
        print(f"VLM non utilisé : {spec.vlm_error}")
    print()
    vals = spec.values()
    for k, f in spec.fields.items():
        flag = "⚠" if (f.missing or f.confidence < 0.7 or f.issues) else "✓"
        print(f"{flag} {k:19s} {f.source.value:8s} {f.confidence:4.2f}  {vals[k]}")
        for i in f.issues:
            print(f"    ↳ {i}")
    for w in spec.warnings:
        print(f"! {w}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--no-vlm", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quote", action="store_true", help="calcule le devis")
    ap.add_argument("--qty", type=int, default=None)
    ap.add_argument("--pdf", default=None, help="chemin du devis PDF à écrire")
    a = ap.parse_args()

    p = Path(a.plan)
    if not a.quote:
        spec = extract(p.read_bytes(), p.name, use_vlm=not a.no_vlm)
        if a.json:
            print(json.dumps({"values": spec.values(), "needs_review": spec.needs_review()},
                             ensure_ascii=False, indent=2, default=str))
        else:
            print_spec(spec)
        return

    spec, quote, pdf = make_quote(p.read_bytes(), p.name, a.qty, use_vlm=not a.no_vlm)
    print_spec(spec)
    print("\n--- Devis ---")
    for ln in quote.lines:
        print(f"{ln.unit_cost:9.2f} €  {ln.label}  ({ln.detail})")
    print(f"Coût unitaire {quote.unit_cost:.2f} € | prix unitaire {quote.unit_price:.2f} € | "
          f"total {quote.total_price:.2f} € pour {quote.quantity} pcs | masse {quote.mass_kg * 1000:.0f} g")
    for h in quote.assumptions:
        print(f"  hyp. {h}")
    for w in quote.warnings:
        print(f"  ! {w}")
    if a.pdf:
        Path(a.pdf).write_bytes(pdf)
        print(f"PDF : {a.pdf}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
