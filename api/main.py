"""API DevisMaker (sert aussi l'interface compilée).

uvicorn api.main:app --port 8000
POST /extract            multipart : file, use_vlm, preview, include_ocr -> champs extraits + analysis_id
POST /feedback           décision de l'utilisateur sur un champ (journal d'apprentissage)
POST /quote/values       valeurs (corrigées) -> chiffrage
POST /quote/document     extraction validée -> devis PDF (archivé en base)
POST /quote              fichier -> extraction + devis en un appel (JSON ou PDF)
GET|PUT /pricing         paramètres atelier en vigueur / nouvelle version ; GET /pricing/versions
GET  /quotes, /quotes/{id}/pdf, /feedback/stats, /logs, /health, /samples
"""

from __future__ import annotations

import base64
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote as urlquote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Modules importés après le chargement du .env (ils lisent leurs variables à l'import)
from devis import db, learning  # noqa: E402
from devis.costing import Quote, compute_quote, current_pricing, load_params, save_pricing  # noqa: E402
from devis.document import render_quote_pdf  # noqa: E402
from devis.logs import current_plan, recent as recent_logs, setup_logging  # noqa: E402
from devis.ocr import default_engine  # noqa: E402
from devis.pipeline import extract, make_quote  # noqa: E402
from devis.schemas import FIELD_SPECS, PartSpec  # noqa: E402
from devis.vlm import provider as vlm_provider, provider_label as vlm_provider_label  # noqa: E402

setup_logging()
log = logging.getLogger("devis.api")


@asynccontextmanager
async def lifespan(_app):
    # Crée le schéma au démarrage ; si la base est injoignable, l'analyse reste possible (rien n'est archivé).
    try:
        db.engine()
        log.info("Base de données : %s", db.database_url().split("@")[-1])
        log.info("OCR : %s · IA : %s", default_engine(), vlm_provider_label() or "aucune clé configurée")
    except Exception:
        log.exception("Base de données injoignable")
    yield


app = FastAPI(title="DevisMaker", version="0.3.0", lifespan=lifespan)

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".tif", ".tiff", ".bmp"}
MAX_BYTES = 30 * 1024 * 1024
Quantity = Field(None, ge=1, le=100_000, description="Quantité de pièces (1 à 100 000)")


def _disposition(kind: str, file_name: str) -> str:
    """En-tête Content-Disposition sûr pour tout nom de fichier (accents, cyrillique…) : ASCII + forme UTF-8."""
    ascii_name = file_name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{urlquote(file_name)}"


async def _read(file: UploadFile) -> tuple[bytes, str]:
    name = file.filename or "plan"
    if Path(name).suffix.lower() not in ALLOWED:
        raise HTTPException(
            415,
            f"Format non supporté ({Path(name).suffix}). Formats : {sorted(ALLOWED)}",
        )
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Fichier trop volumineux (30 Mo max)")
    return data, name


def _spec_json(spec, include_ocr: bool) -> dict:
    out = spec.model_dump(mode="json", exclude=None if include_ocr else {"ocr_tokens"})
    out["values"] = spec.values()
    out["needs_review"] = spec.needs_review()
    return out


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": os.environ.get("DEVIS_VERSION", "dev"),
        "vlm_available": vlm_provider() is not None,
        "vlm_provider": vlm_provider_label(),
        "database": db.ping(),
    }


@app.get("/fields")
def fields():
    return FIELD_SPECS


@app.get("/pricing")
def pricing():
    return load_params()


class PricingIn(BaseModel):
    params: dict[str, Any]
    comment: Optional[str] = None


@app.put("/pricing")
def update_pricing(body: PricingIn):
    """Nouvelle version des paramètres atelier pour tous les devis à venir (les anciens gardent la leur)."""
    _, cur = current_pricing()
    merged = load_params(body.params) if body.params else cur
    ref = load_params({})  # structure attendue : celle de la version en vigueur
    for k in ("materials", "laser", "bending", "labor", "programming", "defaults", "sanity"):
        if not isinstance(merged.get(k), dict) or not merged[k]:
            raise HTTPException(422, f"Paramètre invalide : {k} (objet attendu)")
    for k in ("margin_rate", "scrap_rate", "default_quantity"):
        if not isinstance(merged.get(k), (int, float)) or merged[k] < 0:
            raise HTTPException(422, f"Paramètre invalide : {k} (nombre ≥ 0 attendu)")
    for name, m in merged["materials"].items():  # chaque matière doit rester chiffrable
        if not isinstance(m, dict) or any(not isinstance(m.get(x), (int, float)) or m[x] <= 0
                                          for x in ("density_kg_dm3", "price_eur_kg", "laser_speed_factor")):
            raise HTTPException(422, f"Matière « {name} » : densité, prix au kg et facteur de vitesse laser (> 0) requis")
    unknown = set(merged) - set(ref) - set(cur)
    if unknown:
        raise HTTPException(422, f"Paramètres inconnus : {sorted(unknown)}")
    return {"version": save_pricing(merged, body.comment)}


@app.get("/pricing/versions")
def pricing_versions():
    from sqlalchemy import select

    with db.session() as s:
        rows = s.scalars(select(db.PricingVersion).order_by(db.PricingVersion.id.desc())).all()
        return [{"version": v.id, "date": v.created_at, "commentaire": v.comment} for v in rows]


@app.post("/extract")
async def extract_endpoint(
    file: UploadFile = File(...),
    use_vlm: bool = Form(True),
    include_ocr: bool = Form(False),
    preview: bool = Form(False),
):
    data, name = await _read(file)
    current_plan.set(name)
    t0 = time.perf_counter()
    try:
        spec = await run_in_threadpool(extract, data, name, use_vlm, preview)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    spec.plan_id = learning.plan_id(data)
    try:  # garde le plan et la proposition de la machine, pour apprendre des corrections
        spec.analysis_id = await run_in_threadpool(
            learning.save_analysis, data, name, _spec_json(spec, True), round(time.perf_counter() - t0, 1))
    except Exception:  # la base ne doit jamais empêcher d'analyser un plan
        log.exception("Enregistrement de l'analyse impossible")
    return _spec_json(spec, include_ocr)


class FeedbackIn(BaseModel):
    analysis_id: int
    field: str
    value: Any = None
    action: str  # confirmation | saisie | choix_sur_plan
    detail: Optional[dict[str, Any]] = None


@app.post("/feedback")
def feedback(body: FeedbackIn):
    """Une action de l'utilisateur sur un champ (validation, correction, infirmation, ajout)."""
    if body.field not in FIELD_SPECS:
        raise HTTPException(422, f"Champ inconnu : {body.field}")
    if body.action not in ("confirmation", "saisie", "choix_sur_plan"):
        raise HTTPException(422, f"Action inconnue : {body.action}")
    try:
        return learning.record(body.analysis_id, body.field, body.value, body.action, body.detail)
    except LookupError:
        raise HTTPException(404, "Analyse inconnue")


@app.get("/feedback/stats")
def feedback_stats():
    return learning.stats()


@app.post("/quote")
async def quote_endpoint(
    file: UploadFile = File(...),
    quantity: Optional[int] = Form(None),
    use_vlm: bool = Form(True),
    client: Optional[str] = Form(None),
    format: str = Form("json"),
):
    data, name = await _read(file)
    current_plan.set(name)
    if quantity is not None and not 1 <= quantity <= 100_000:
        raise HTTPException(422, "Quantité entre 1 et 100 000")
    try:
        spec, quote, pdf = await run_in_threadpool(
            make_quote, data, name, quantity, use_vlm, client
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if format == "pdf":
        return Response(
            pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": _disposition("inline", f"devis_{Path(name).stem}.pdf")},
        )
    return {"extraction": _spec_json(spec, False), "quote": quote.model_dump()}


class ValuesIn(BaseModel):
    values: dict[str, Any]
    quantity: Optional[int] = Quantity
    pricing_overrides: Optional[dict[str, Any]] = None
    confidences: Optional[dict[str, float]] = None


@app.post("/quote/values", response_model=Quote)
def quote_from_values(body: ValuesIn):
    """Rechiffrage après correction manuelle des champs (écran de validation)."""
    return compute_quote(
        body.values,
        body.quantity,
        load_params(body.pricing_overrides),
        body.confidences,
    )


class DocumentIn(BaseModel):
    extraction: dict[str, Any]  # objet renvoyé par /extract, éventuellement corrigé
    quantity: Optional[int] = Quantity
    client: Optional[str] = None
    pricing_overrides: Optional[dict[str, Any]] = None


@app.post("/quote/document")
def quote_document(body: DocumentIn):
    """Devis PDF à partir de l'extraction validée dans l'interface."""
    try:
        spec = PartSpec.model_validate(body.extraction)
    except ValidationError as e:
        raise HTTPException(422, f"Extraction invalide : {e.errors()[:3]}") from e
    conf = {k: f.confidence for k, f in spec.fields.items() if not f.missing}
    version, _ = current_pricing()
    quote = compute_quote(
        spec.values(), body.quantity, load_params(body.pricing_overrides), conf
    )
    thumb = base64.b64decode(spec.preview_png_b64) if spec.preview_png_b64 else None
    pdf = render_quote_pdf(spec, quote, thumb, body.client)
    headers = {"Content-Disposition": _disposition("attachment", f"devis_{Path(spec.file_name).stem}.pdf")}
    try:  # archive du devis émis : valeurs, paramètres et PDF exacts
        with db.session() as s:
            known = spec.analysis_id is not None and s.get(db.Analysis, spec.analysis_id) is not None
            row = db.Quote(analysis_id=spec.analysis_id if known else None, file_name=spec.file_name,
                           client=body.client, quantity=quote.quantity, pricing_version_id=version,
                           pricing_overrides=body.pricing_overrides or {}, values=spec.values(),
                           quote=quote.model_dump(mode="json"), unit_price=quote.unit_price,
                           total_price=quote.total_price, pdf=pdf)
            s.add(row)
            s.flush()
            headers["X-Quote-Id"] = str(row.id)
    except Exception:
        log.exception("Archivage du devis impossible")
    return Response(pdf, media_type="application/pdf", headers=headers)


@app.get("/quotes")
def list_quotes(limit: int = 50):
    """Devis émis, du plus récent au plus ancien."""
    from sqlalchemy import select

    with db.session() as s:
        rows = s.scalars(select(db.Quote).order_by(db.Quote.id.desc()).limit(min(limit, 500))).all()
        return [{"id": q.id, "date": q.created_at, "plan": q.file_name, "client": q.client, "quantite": q.quantity,
                 "prix_unitaire": q.unit_price, "total": q.total_price, "analyse": q.analysis_id,
                 "parametres_version": q.pricing_version_id} for q in rows]


@app.get("/quotes/{quote_id}/pdf")
def quote_pdf(quote_id: int):
    with db.session() as s:
        q = s.get(db.Quote, quote_id)
        if q is None:
            raise HTTPException(404, "Devis introuvable")
        return Response(q.pdf, media_type="application/pdf",
                        headers={"Content-Disposition": _disposition("inline", f"devis_{q.id}_{Path(q.file_name).stem}.pdf")})


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@app.get("/samples")
def list_samples():
    return sorted(p.name for p in SAMPLES.glob("*") if p.suffix.lower() in ALLOWED)


@app.get("/samples/{name}")
def get_sample(name: str):
    p = (SAMPLES / name).resolve()
    if (
        p.parent != SAMPLES.resolve()
        or p.suffix.lower() not in ALLOWED
        or not p.is_file()
    ):
        raise HTTPException(404, "Exemple introuvable")
    return FileResponse(p)


# Interface React compilée (cd frontend && npm run build), après les routes API pour ne pas les masquer.
# En développement : npm run dev (Vite, port 5173, proxy vers cette API).
FRONT = Path(__file__).resolve().parents[1] / "frontend" / "dist"


@app.middleware("http")
async def log_requests(request, call_next):
    """Une ligne par appel d'API : méthode, route, statut, durée. Les fichiers de l'interface ne sont pas journalisés."""
    path = request.url.path
    api = not (path == "/" or path.startswith("/assets") or path.endswith((".js", ".css", ".ico", ".svg", ".png")))
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("%s %s : erreur interne", request.method, path)
        raise
    if api and path != "/logs":
        dt = time.perf_counter() - t0
        lvl = logging.ERROR if response.status_code >= 500 else logging.WARNING if response.status_code >= 400 else logging.INFO
        log.log(lvl, "%s %s → %d (%.2f s)", request.method, path, response.status_code, dt)
    return response


@app.exception_handler(HTTPException)
async def log_http_error(request, exc: HTTPException):
    """Les refus (fichier non supporté, analyse inconnue…) sont journalisés avec leur raison."""
    from fastapi.exception_handlers import http_exception_handler

    log.warning("%s %s refusé (%d) : %s", request.method, request.url.path, exc.status_code, exc.detail)
    return await http_exception_handler(request, exc)


@app.get("/logs")
def logs(level: str = "INFO", limit: int = 200, plan: Optional[str] = None):
    """Derniers journaux du serveur (appels OCR/IA, erreurs, replis, requêtes), du plus récent au plus ancien."""
    return recent_logs(level, min(limit, 1000), plan)


@app.middleware("http")
async def no_cache_index(request, call_next):
    # index.html référence les fichiers JS/CSS versionnés : il ne doit jamais rester en cache,
    # sinon le navigateur garde l'ancienne interface après un nouveau build.
    response = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


if FRONT.is_dir():
    app.mount("/", StaticFiles(directory=FRONT, html=True), name="web")
