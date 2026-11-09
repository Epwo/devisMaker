# DevisMaker

Devis automatique de pièces de tôlerie à partir de leur plan (PDF, scan ou image).

**Chaîne :** plan → lecture du texte (texte du PDF, ou OCR) → règles → l'IA *désigne* les textes qui portent
chaque information (« pointage ») → contrôles de cohérence → vérification par l'utilisateur → chiffrage → devis PDF.
Principe : **mieux vaut aucune valeur qu'une fausse** — ce qui est incertain est signalé « à vérifier ».

---

## Installation (une fois)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
npm --prefix frontend install
npm --prefix frontend run build
cp .env.example .env        # puis renseigner les clés (voir plus bas)
```

## Lancer l'application

```bash
.venv/Scripts/python -m uvicorn api.main:app --port 8000
```

Puis ouvrir **http://localhost:8000** (si l'interface semble ancienne après un build : `Ctrl+F5`).

Base de données : SQLite par défaut (`data/devismaker.db`, créée au premier lancement), ou PostgreSQL avec
`DATABASE_URL=postgresql+psycopg://utilisateur:motdepasse@hôte:5432/devismaker`.

## Déploiement (Docker)

```bash
cp .env.example .env            # clés API + POSTGRES_PASSWORD obligatoire
docker compose up -d --build
```

→ http://localhost:8000 (port modifiable avec `APP_PORT`). Deux conteneurs :

| Conteneur | Image | Rôle |
|---|---|---|
| `app` | `devismaker` (`Dockerfile`, multi-étapes : build React puis Python 3.12) | API + interface compilée, 1 worker uvicorn, utilisateur non root, modèles OCR inclus dans l'image |
| `db` | `postgres:17.2-alpine` | base de données, **non exposée** hors du réseau Docker |

Volumes : `pgdata` (base) et `cache` (réponses OCR/IA déjà payées). Les clés restent dans `.env`, jamais dans l'image
(`.dockerignore` exclut aussi `data/`, `.cache/`, `out/`). Santé : `GET /health` (IA disponible, base joignable).

```bash
docker compose logs -f app                          # journaux
docker compose exec app python scripts/migrate_learning_json.py   # importer un ancien journal JSON
docker compose down                                 # arrêter (les volumes sont conservés)
```

Pour travailler sur l'interface avec rechargement à chaud (l'API doit tourner sur :8000) :

```bash
npm --prefix frontend run dev
```

→ http://localhost:5173

## Ligne de commande

```bash
.venv/Scripts/python scripts/extract.py samples/piece_003.pdf                  # extraction (OCR + IA)
.venv/Scripts/python scripts/extract.py samples/piece_003.pdf --no-vlm         # sans IA
.venv/Scripts/python scripts/extract.py samples/piece_003.pdf --json           # valeurs en JSON
.venv/Scripts/python scripts/extract.py samples/piece_003.pdf --quote --qty 25 --pdf out/devis.pdf
```

Remove cache : 
```
 Remove-Item -Recurse -Force .cache\mistral, 
.cache\vlm
```

## Tests

```bash
.venv/Scripts/python -m pytest -q
```

Hors ligne et déterministes (OCR local forcé, aucun appel Mistral/Claude).

## Bancs d'essai

```bash
.venv/Scripts/python scripts/bench_ocr.py                    # moteurs OCR seuls sur piece_003
.venv/Scripts/python scripts/bench_vlm.py                    # toutes les configurations (long, payant)
.venv/Scripts/python scripts/bench_vlm.py --only "pointage:mistral-small-latest+ministral-14b-latest" --merge
.venv/Scripts/python scripts/bench_vlm.py --plans piece_003.pdf --only ocr_seul
```

- Plans de test : `samples/` (sources dans `samples/SOURCES.md`), vérité terrain : `tests/fixtures/benchmark_truth.json`.
- Notation : juste +1 · **faux −2** · vide 0. Résultats : `out/bench_vlm.md` (+ `out/bench_vlm.json`).
- `--merge` ne remplace que les configurations rejouées. Les réponses IA sont en cache (`.cache/vlm`, `.cache/mistral`) :
  rejouer une même requête ne coûte rien.

---

## Configuration (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `MISTRAL_API_KEY` | — | OCR Mistral + IA Mistral |
| `CLAUDE_API_KEY` (ou `ANTHROPIC_API_KEY`) | — | IA Claude |
| `DEVIS_OCR_ENGINE` | `auto` | `auto` = `mistral_board` si clé Mistral, sinon `rapid_v4` (local, gratuit, hors ligne) |
| `DEVIS_VLM_STRATEGY` | `pointage` | `pointage` (l'IA désigne les textes OCR) ou `lecture` (l'IA lit les valeurs) |
| `DEVIS_VLM_PROVIDER` | `auto` | Claude (Haiku 5.5) si clé, sinon Mistral ; bascule sur l'autre en cas d'échec (ex. crédit Claude épuisé) |
| `DEVIS_MISTRAL_VLM_MODEL` | `mistral-small-latest+ministral-14b-latest` | `a+b` = consensus : une valeur n'est gardée que si les modèles s'accordent |
| `DEVIS_VLM_MODEL` | `claude-haiku-5-5` | modèle Claude |
| `DEVIS_VLM_EFFORT` | `low` | effort de réflexion Claude (`low`, `medium`, `high`) — `low` suffit en « pointage » |
| `DEVIS_VLM_CACHE` | `1` | `0` pour désactiver le cache des réponses IA |
| `DEVIS_LEARNING` | `1` | `0` pour ne pas enregistrer les plans ni les corrections (voir « Journal d'apprentissage ») |
| `DATABASE_URL` | SQLite `data/devismaker.db` | base de données (PostgreSQL en déploiement) |
| `POSTGRES_PASSWORD` | — | **obligatoire** avec `docker compose` (`POSTGRES_USER`, `POSTGRES_DB` facultatifs) |
| `DEVIS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DEVIS_LOG_FILE` | — | fichier de journaux tournant (5 × 5 Mo), ex. `data/logs/devismaker.log` |
| `DEVIS_MARKS_VIEW_CROPS` | `0` | `1` = envoyer aussi les vues agrandies (n'aide pas les modèles testés) |

Prix et taux atelier du chiffrage : en base, **versionnés** (table `pricing_versions`). `config/pricing.json` ne sert que de
valeurs initiales au premier démarrage. Dans l'interface (panneau « Paramètres atelier ») : modifier pour un dossier, ou
« Enregistrer pour tout l'atelier » (nouvelle version ; les devis déjà émis gardent la leur).

### Configuration par défaut et pourquoi

Mesurée sur 11 plans (`out/bench_vlm.md`) :

| | Score | Faux | Précision | Coût / plan | Temps |
|---|---|---|---|---|---|
| **Mistral OCR « planche » + Claude Haiku 5.5 effort low** (défaut) — 8/8 sur piece_003 | 47 | 4 | 93 % | ~0,9 c$ | ~40 s |
| Mistral OCR « planche » + consensus Small 4 + Ministral 14B (repli si Claude indisponible) | 36 | **2** | **95 %** | ~1 c$ | ~50 s |
| Claude Opus 5.5 (même stratégie) | 48 | 4 | 93 % | ~8 c$ | ~50 s |
| OCR local `rapid_v4` + même consensus (gratuit côté OCR) | 27 | 5 | 88 % | 0,26 c$ | ~25 s |
| Mistral Large 4 seul | 49 | 4 | 93 % | 5 c$ | ~2 min |

Sur `piece_003` (3 vues de la même pièce), tous les modèles Claude — y compris **Haiku 5.5**, le moins cher —
obtiennent 8/8 ; les petits modèles Mistral ne relient pas les vues (épaisseur, largeur, plis laissés vides).
D'où l'ordre par défaut : **Claude Haiku 5.5 en « pointage »**, puis le consensus Mistral si Claude ne répond pas.

Le consensus Mistral privilégie le moins d'erreurs pour un coût minime ; les champs laissés vides sont à vérifier dans l'interface
(clic « Choisir sur le plan »).

---

## Journaux (logs)

Le serveur journalise chaque requête d'API, chaque appel Mistral OCR et IA (réponse en cache ou appel réel, durée,
tokens, coût), les **échecs** (clé invalide, quota, réseau, réponse illisible), les **replis** (OCR local, bascule
Claude → Mistral) et le résumé de chaque analyse. Chaque ligne indique le plan en cours :

```
2026-11-08 16:26:04 INFO    [komacut_bent_part.jpg] devis.vlm : IA claude/claude-haiku-5-5 : réponse en 10.5 s, 13711 tokens entrée / 1995 sortie, 0.237 c$
2026-11-08 16:26:15 WARNING [-] devis.api : POST /extract refusé (415) : Format non supporté (.md)
```

- dans le terminal d'uvicorn, ou `docker compose logs -f app` ;
- `GET /logs?level=WARNING&limit=100&plan=piece_003` : les 1000 dernières lignes en JSON, filtrables (niveau minimal, plan) ;
- dans un fichier si `DEVIS_LOG_FILE` est défini.

## Base de données

`devis/db.py` (SQLAlchemy). Schéma créé au démarrage (pour faire évoluer le schéma en production : Alembic).

| Table | Contenu |
|---|---|
| `plans` | fichier du plan (empreinte, nom, contenu sauf si `DEVIS_LEARNING=0`) |
| `analyses` | proposition de la machine pour une analyse : champs, textes lus, IA utilisée, durée, champs à vérifier |
| `decisions` | action de l'utilisateur sur un champ, avec la proposition d'origine et `machine_unsure` ; type `validation` (juste), `correction` (remplacée), `infirmation` (effacée : l'information n'existe pas), `ajout` (la machine n'avait rien) |
| `pricing_versions` | paramètres atelier historisés |
| `quotes` | devis émis : valeurs, paramètres utilisés (version + ajustements), lignes de prix, PDF |

C'est le **journal d'apprentissage** : pour une même analyse et un même champ, la dernière décision fait foi.
Résumé : `GET /feedback/stats`. Ancien journal en fichiers (`data/learning/`) : `scripts/migrate_learning_json.py`.

Schéma de l'architecture (pipeline, front, API, base, APIs externes) : `docs/architecture.tldr`, à ouvrir sur
[tldraw.com](https://www.tldraw.com) (menu → Ouvrir un fichier).

---

## Organisation du code

```
api/main.py           API FastAPI (+ sert l'interface compilée)
devis/
  ingest.py           PDF / image -> image, texte vectoriel des PDF
  ocr.py              moteurs OCR locaux (PP-OCRv4/v5) et choix du moteur
  ocr_mistral.py      OCR Mistral « planche » (détection locale + lecture Mistral)
  parser.py           règles : texte lu -> champs (trous, plis, matière…)
  checks.py           contrôles de cohérence métier
  vlm.py              appels IA (Claude / Mistral), cache, coût ; stratégie « lecture »
  vlm_marks.py        stratégie « pointage » (textes numérotés) et consensus
  logs.py             journaux (console, fichier, GET /logs)
  db.py               base de données (SQLAlchemy : PostgreSQL ou SQLite)
  learning.py         journal d'apprentissage (plans, analyses, décisions)
  pipeline.py         orchestration + fusion OCR/IA + journal de décision (« Pourquoi ? »)
  costing.py          chiffrage
  document.py         devis PDF
frontend/             interface React (Vite + TypeScript)
scripts/              CLI d'extraction, bancs d'essai, migration du journal JSON
docs/architecture.tldr  schéma d'architecture (tldraw)
Dockerfile, docker-compose.yml  déploiement
samples/              plans de test ; refs/ = documents sources
tests/                tests (pytest)
```

## API

| Méthode | Route | Rôle |
|---|---|---|
| `POST` | `/extract` | fichier → champs extraits (valeur, confiance, source, zones, journal) |
| `POST` | `/quote/values` | valeurs (corrigées) → chiffrage |
| `POST` | `/quote/document` | extraction validée → devis PDF |
| `POST` | `/quote` | fichier → extraction + devis (JSON ou PDF) |
| `POST` | `/feedback` | décision de l'utilisateur sur un champ (`analysis_id`, journal d'apprentissage) |
| `GET` / `PUT` | `/pricing` | paramètres atelier en vigueur / nouvelle version ; historique : `GET /pricing/versions` |
| `GET` | `/quotes`, `/quotes/{id}/pdf` | devis émis, PDF archivé |
| `GET` | `/logs` | derniers journaux du serveur (`level`, `limit`, `plan`) |
| `GET` | `/health`, `/samples`, `/feedback/stats` | état (IA, base), plans d'exemple, statistiques des corrections |
