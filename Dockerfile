# DevisMaker : une image = API FastAPI + interface React compilée (servie par l'API).
# Construction locale : docker compose build      Publication multi-architecture : scripts/publish_image.sh

# ---------------------------------------------------------------- 1. interface React
# Le JS compilé ne dépend pas de l'architecture : compilé une fois sur la machine de build (pas d'émulation)
FROM --platform=$BUILDPLATFORM node:20.18.1-alpine3.20 AS front
WORKDIR /front
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- 2. API Python
FROM python:3.12.8-slim-bookworm AS app

ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="DevisMaker" \
      org.opencontainers.image.description="POC : devis de tôlerie à partir d'un plan (OCR + IA + chiffrage)" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV DEVIS_VERSION=${APP_VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# OpenCV (tiré par RapidOCR) a besoin de libGL / glib
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Utilisateur non root, propriétaire des seuls dossiers où l'application écrit : .cache (réponses OCR/IA), data
# (SQLite si pas de PostgreSQL) et le dossier des modèles RapidOCR, qu'il re-télécharge s'il juge un fichier
# absent ou invalide (sinon : « Permission denied » sur ch_PP-OCRv5_det_mobile.onnx et repli sur l'OCR local).
RUN useradd --create-home --uid 1000 devis \
 && models="$(python -c 'import os, rapidocr; print(os.path.join(os.path.dirname(rapidocr.__file__), "models"))')" \
 && mkdir -p .cache data "$models" \
 && chown -R devis:devis .cache data "$models"

# Modèles OCR téléchargés au build, par l'utilisateur qui fera tourner l'application, puis vraie lecture d'un plan
# avec chaque moteur (dont ceux de « mistral_board ») : un modèle manquant ou illisible fait échouer le build.
COPY devis/__init__.py devis/ocr.py devis/schemas.py devis/
COPY scripts/warmup_ocr.py scripts/
COPY samples/tmlaser_came_contact.png samples/
USER devis
RUN python scripts/warmup_ocr.py

# Reste de l'application, propriété de root : en lecture seule pour l'utilisateur devis
USER root
COPY api/ api/
COPY devis/ devis/
COPY config/ config/
COPY scripts/ scripts/
COPY samples/ samples/
COPY --from=front /front/dist frontend/dist
USER devis

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# 1 worker : l'analyse d'un plan est lourde (OCR) et le POC traite un plan à la fois
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
