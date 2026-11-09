"""Génération du devis PDF (HTML -> PDF avec PyMuPDF Story, sans dépendance externe)."""
from __future__ import annotations

import html
import io
from datetime import datetime
from typing import Any, Optional

import pymupdf

from .costing import Quote
from .schemas import PartSpec

SRC_LABEL = {"ocr": "OCR", "vlm": "IA", "ocr+vlm": "OCR+IA", "default": "défaut", "user": "saisi", "none": "—"}

CSS = """
* { font-family: sans-serif; }
body { font-size: 9pt; color: #222; }
h1 { font-size: 16pt; margin: 0 0 2px 0; }
h2 { font-size: 11pt; margin: 12px 0 4px 0; color: #1f3b73; border-bottom: 1px solid #1f3b73; }
.sub { color: #777; font-size: 8pt; }
table { width: 100%; border-collapse: collapse; }
td, th { border: 1px solid #ccc; padding: 3px 5px; vertical-align: top; }
th { color: #1f3b73; text-align: left; border-bottom: 2px solid #1f3b73; }
td.k { font-weight: bold; color: #444; }
td.n { text-align: right; }
.warn { color: #b45309; }
.low { color: #b91c1c; }
.total td { font-weight: bold; color: #1f3b73; }
"""
# NB : pas de background-color dans les tableaux — PyMuPDF Story redessine les fonds
# de la page précédente lors d'un saut de page.


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _fmt(v: Any) -> str:
    if v is None or v == [] or v == "":
        return '<span class="low">non renseigné</span>'
    if isinstance(v, float):
        return f"{v:g}"
    return _e(v)


def _eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


HOLE_KIND = {"through": "débouchant", "threaded": "taraudé", "insert": "pour insert"}
_SHAPES = (("chanfrein", "chanfrein"), ("oblong", "oblong"), ("rainure", "oblong"), ("carr", "carré"),
           ("rect", "rectangulaire"), ("encoche", "encoche"), ("rayon", "arrondi"))


def _cut_shape(desc: str) -> str:
    """Forme d'une découpe à partir de sa description lue sur le plan (« chanfrein 20x20 mm » -> « chanfrein »)."""
    d = desc.lower()
    return next((shape for word, shape in _SHAPES if word in d), "autre")


def _cut_dims(desc: str) -> str:
    """Dimensions d'une découpe : la description sans le mot de forme (« chanfrein 20x20 mm » -> « 20 × 20 mm »)."""
    d = desc.strip()
    for word, _ in _SHAPES:
        if d.lower().startswith(word):
            d = d.split(" ", 1)[1] if " " in d else ""
            break
    return d.replace("x", " × ").replace("  ", " ") or "—"


def render_quote_pdf(spec: PartSpec, quote: Quote, plan_png: Optional[bytes] = None, client: Optional[str] = None) -> bytes:
    v = spec.values()
    f = spec.fields

    def row(label: str, key: str, unit: str = "") -> str:
        fld = f.get(key)
        val = _fmt(v.get(key)) + (f" {unit}" if unit and v.get(key) not in (None, "", []) else "")
        tag = ""
        if fld is not None:
            src = SRC_LABEL.get(fld.source.value, fld.source.value)
            cls = "low" if (fld.missing or fld.confidence < 0.7 or fld.issues) else "sub"
            tag = f' <span class="{cls}">[{src} {fld.confidence:.0%}]</span>'
        return f'<tr><td class="k" width="32%">{label}</td><td>{val}{tag}</td></tr>'

    # Découpes : trous (forme ronde) puis découpes décrites sur le plan (chanfreins, oblongs…)
    cut_rows = [
        (_e("rond"), f"Ø {_fmt(h.get('diameter_mm'))} mm", _e(HOLE_KIND.get(h.get("kind") or "through", h.get("kind"))), _fmt(h.get("quantity", 1)))
        for h in v.get("holes") or [] if isinstance(h, dict)
    ] + [
        (_e(_cut_shape(c.get("description") or "")), _e(_cut_dims(c.get("description") or "")), "", _fmt(c.get("quantity", 1)))
        for c in v.get("cutouts") or [] if isinstance(c, dict)
    ]
    cuts = "".join(
        f"<tr><td>{shape}</td><td>{dims}</td><td>{kind}</td><td class='n'>{qty}</td></tr>" for shape, dims, kind, qty in cut_rows
    ) or '<tr><td colspan="4" class="low">aucune découpe détectée</td></tr>'
    bends = "".join(
        f"<tr><td class='n'>{_fmt(b.get('angle_deg')) + ('°' if b.get('angle_deg') is not None else '')}</td>"
        f"<td class='n'>{('R' + _fmt(b.get('radius_mm'))) if b.get('radius_mm') is not None else _fmt(None)}</td>"
        f"<td class='n'>{_fmt(b.get('quantity', 1))}</td></tr>"
        for b in v.get("bends") or []
    ) or '<tr><td colspan="3">aucun pli</td></tr>'
    lines = "".join(
        f"<tr><td>{_e(l.category)}</td><td>{_e(l.label)}<br/><span class='sub'>{_e(l.detail)}</span></td>"
        f"<td class='n'>{_eur(l.unit_cost)}</td></tr>"
        for l in quote.lines
    )
    review = [k for k in spec.needs_review()]
    checks = "".join(f"<li class='warn'>{_e(w)}</li>" for w in quote.warnings + spec.warnings)
    hyps = "".join(f"<li>{_e(h)}</li>" for h in quote.assumptions)
    img = '<p style="text-align:center"><img src="plan.png" width="420"/></p>' if plan_png else ""

    body = f"""
<h1>DEVIS — TÔLERIE / DÉCOUPE LASER</h1>
<div class="sub">Généré le {datetime.now():%d/%m/%Y à %H:%M} — plan : {_e(spec.file_name)} — lecture : {_e(spec.text_source)}{' + IA' if spec.vlm_used else ''}</div>
{img}
<h2>Identification</h2>
<table>
{row('Référence', 'reference')}
<tr><td class="k" width="32%">Client</td><td>{_fmt(client or v.get('client'))}</td></tr>
{row('Désignation', 'designation')}
<tr><td class="k" width="32%">Quantité</td><td>{quote.quantity} pièce(s)</td></tr>
</table>
<h2>Matière & traitement</h2>
<table>
{row('Matière', 'material')}{row('Nuance', 'material_grade')}{row('Épaisseur', 'thickness_mm', 'mm')}{row('Traitement', 'surface_treatment')}
</table>
<h2>Dimensions</h2>
<table>
{row('Longueur', 'length_mm', 'mm')}{row('Largeur', 'width_mm', 'mm')}{row('Hauteur', 'height_mm', 'mm')}
<tr><td class="k" width="32%">Flan retenu (avec marge)</td><td>{quote.blank_mm[0]:g} × {quote.blank_mm[1]:g} mm</td></tr>
<tr><td class="k" width="32%">Longueur de découpe</td><td>{quote.cut_length_mm / 1000:.2f} m</td></tr>
<tr><td class="k" width="32%">Masse estimée</td><td>{quote.mass_kg * 1000:.0f} g</td></tr>
</table>
<h2>Découpes</h2>
<table><tr><th>Forme</th><th>Dimensions</th><th>Type</th><th>Qté</th></tr>{cuts}</table>
<h2>Plis</h2>
<table><tr><th>Angle</th><th>Rayon (mm)</th><th>Qté</th></tr>{bends}</table>
<h2>Chiffrage (par pièce)</h2>
<table>
<tr><th>Poste</th><th>Détail</th><th>Coût</th></tr>
{lines}
<tr class="total"><td colspan="2">Coût de revient unitaire</td><td class="n">{_eur(quote.unit_cost)}</td></tr>
<tr class="total"><td colspan="2">Prix de vente unitaire HT (marge {quote.margin_rate:.0%})</td><td class="n">{_eur(quote.unit_price)}</td></tr>
<tr class="total"><td colspan="2">Total HT — {quote.quantity} pièce(s)</td><td class="n">{_eur(quote.total_price)}</td></tr>
</table>
<h2>Hypothèses & points à vérifier</h2>
<ul>{hyps}{checks}</ul>
{'<p class="low">Champs à relire : ' + _e(', '.join(review)) + '</p>' if review else ''}
<p class="sub">Tolérances : {_fmt(v.get('tolerance_standard'))}. Devis indicatif établi automatiquement à partir du plan ; prix basés sur des paramètres par défaut.</p>
"""
    archive = pymupdf.Archive()
    if plan_png:
        archive.add(plan_png, "plan.png")
    story = pymupdf.Story(html=f"<html><body>{body}</body></html>", user_css=CSS, archive=archive)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page_rect = pymupdf.paper_rect("a4")
    where = page_rect + (36, 36, -36, -36)
    more = True
    while more:
        dev = writer.begin_page(page_rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()
