# DevisMaker : récapitulatif du projet

*Point d'étape avant déploiement. 6 novembre 2026.*

---

## 1. Le besoin

Le cas pratique Maji demande de produire un **devis de tôlerie à partir du plan d'une pièce** (PDF, scan ou photo).
Exemple fourni : `piece_003.pdf` (plan scanné, cartouche masqué) et `devis.pdf` (le devis attendu).

Ce qu'il faut sortir du plan :

- matière et épaisseur ;
- dimensions hors tout (longueur × largeur × hauteur) ;
- trous (Ø × quantité) et découpes ;
- plis (nombre, angle, rayon) ;
- traitement de surface, composants (inserts, écrous à sertir), désignation et tolérances.

On en déduit ensuite :

- le flan et la masse ;
- le temps laser, le temps de pliage et la main-d'œuvre ;
- le prix unitaire et le prix total.

**Principe directeur, posé dès le départ : mieux vaut aucune valeur qu'une valeur fausse.**
Une case vide se voit et se corrige. Une fausse cote (60 × 42 au lieu de 60 × 60) part dans le devis sans que personne ne la remarque.

---

## 2. La solution en une image

```
 Plan (PDF / scan / photo)
        │
        ▼
 ① Lecture du texte ─── PDF vectoriel : texte lu directement
        │               Scan / image : OCR local (repérage) + Mistral OCR (lecture)
        ▼
 ② Règles métier ────── « 2X Ø8.75 », « ép 2 », « R2 », « 90° », « S235 »… → champs typés
        │
        ▼
 ③ IA en « pointage » ─ l'IA voit le plan + les textes numérotés et répond « l'épaisseur, c'est le texte n°12 »
        │               → la valeur vient toujours d'un texte réellement lu, jamais inventée
        │               → l'IA relie les vues (dessus / face / côté) pour les dimensions hors tout
        ▼
 ④ Fusion + contrôles ─ accord OCR/IA, plausibilité (épaisseur 0,3–25 mm, Ø, vues contradictoires…)
        │               doute → case vide ou « à vérifier »
        ▼
 ⑤ Vérification ─────── l'utilisateur confirme, corrige, ou clique le bon texte sur le plan
        │               (chaque décision est enregistrée pour réapprendre)
        ▼
 ⑥ Chiffrage + PDF ──── prix recalculé en direct, devis PDF structuré
```

**Stack :**

- Python, avec un back **FastAPI** ;
- front **React** (Vite + TypeScript), servi par le back ;
- OCR local **RapidOCR** (PP-OCRv4/v5, ONNX) ;
- **Mistral OCR** ;
- IA **Claude** (Haiku 5.5) et **Mistral** en repli ;
- PDF avec **PyMuPDF** ;
- base **PostgreSQL** (SQLite en local) avec SQLAlchemy ;
- déploiement **Docker** (`docker compose` : application + base).

---

## 3. Ce que nous avons fait, étape par étape

### 3.1 Chaîne OCR → règles → IA (première version)
- Ingestion PDF et image : le texte vectoriel est lu directement quand il existe, l'OCR prend le relais quand la page est un scan, et les petites images sont agrandies.
- Le **parser à règles** (`devis/parser.py`) transforme les textes lus en champs. Chaque champ porte une valeur, une confiance, une source (OCR, IA, OCR+IA, défaut, utilisateur, aucune), la preuve (le texte lu), sa zone sur le plan et ses alertes.
- On ne fait appel à l'IA que si l'OCR n'a pas trouvé l'information ou n'en est pas sûr.
- Le chiffrage utilise des paramètres par défaut réalistes (`config/pricing.json`), modifiables dans l'interface.
- Le devis PDF est structuré.

### 3.2 Interface
- **Barre latérale unique** : ajouter des plans et suivre leur avancement (en attente, en analyse, à vérifier, prêt).
- **Pensée pour le parallèle** : file côté client, `MAX_PARALLEL` réglable. Le back reste séquentiel pour le POC ; l'interface l'indique (« les plans sont analysés l'un après l'autre »).
- **Voir où chaque valeur a été lue** : un clic sur une information ou une catégorie la surligne sur le plan, assombrit le reste et zoome dessus. Le bandeau propose « Voir toute la pièce ».
- **« Choisir sur le plan »** : l'utilisateur clique le texte reconnu au lieu de taper la valeur. Le texte est converti selon le champ (Ø, quantité, angle…), puis contrôlé.
- **« Pourquoi ? »** : le journal de décision complet d'une valeur (lecture → règle → routage → IA → fusion → contrôle → utilisateur), exportable en JSON.
- **Rien n'est caché** : toutes les informations restent visibles, celles à vérifier sont en tête.
- **Zoom** : molette à l'endroit de la souris (jusqu'à ×8), glisser pour se déplacer, double-clic.
- **Page « Comment ça marche »** à l'accueil.
- **Corrections diverses** : retour de défilement intempestif, « Choisir sur le plan » qui validait la saisie en cours, interface en cache après un nouveau build.

### 3.3 OCR : recherche du meilleur moteur
Banc d'essai sur piece_003 (`out/bench_ocr.md`), 28 annotations à lire :

| Moteur | Annotations lues | Temps | Coût |
|---|---|---|---|
| RapidOCR PP-OCRv4 (local, 2 passes : normale + tournée pour les cotes verticales) | 26/28 | 16 s | 0 |
| PP-OCRv5 / v5 server (local) | 25/28 | 12 s / 53 s | 0 |
| Mistral OCR sur la page entière | 19/28 (perd les cotes isolées) | 9 s | 0,4 c$ |
| **« Planche » : repérage local + lecture Mistral OCR** | **28/28** | 20 s | ~0,65 c$ |

Ce qu'on a constaté : Mistral OCR lit très bien le texte, mais sur un dessin il ignore les cotes isolées. Les moteurs locaux, eux, *trouvent* tout mais lisent moins bien.
→ Solution « planche » : l'OCR local repère chaque texte, on découpe ces textes, on les numérote sur une planche, et Mistral OCR les lit. Quand l'OCR local et Mistral lisent deux choses différentes, on garde les deux lectures et la plus plausible est choisie au contrôle (exemple : épaisseur « 21 » lue par Mistral au lieu de « 2 »).

### 3.4 IA : deux stratégies comparées
- **« Lecture »** : l'IA lit elle-même les valeurs sur l'image. Elle remplit beaucoup, mais **invente** (cotes hors tout fausses, trous en trop).
- **« Pointage »** (set-of-marks), **retenue** : l'IA désigne les numéros des textes OCR qui portent chaque information, et la valeur est extraite par nos règles à partir de ce texte. L'IA ne peut donc pas produire une valeur qui n'existe pas sur le plan.
- Le prompt « multi-vues » explique à l'IA que les 3 vues de piece_003 sont *la même pièce* (dessus = longueur × largeur, face = longueur × hauteur…). Avant ce prompt, la largeur et la hauteur restaient vides ou fausses.
- **Mode consensus** (`modèle_a+modèle_b`) : deux modèles en parallèle, on ne garde que ce sur quoi ils s'accordent.

### 3.5 Banc d'essai complet (`out/bench_vlm.md`)
On a constitué **11 plans réels** (piece_003, sujet de bac pro eduscol avec corrigé chiffré, plans Komacut, Lasermaster, TM Laser, universités, voir `samples/SOURCES.md`) et écrit **à la main** leur vérité terrain (74 champs, `null` = doit rester vide).
Notation : juste +1, **faux −2**, vide 0. Une erreur coûte deux fois plus qu'un oubli, ce qui applique le principe directeur.

31 configurations testées. Extrait :

| Configuration | Score | Faux | Précision | Coût IA / plan | Temps IA | piece_003 |
|---|---|---|---|---|---|---|
| Mistral Large 4, pointage | 52 | 3 | 95 % | 5,7 c$ | **107 s** | 7/8 |
| Claude Sonnet 5.5 @low, pointage | 49 | 5 | 92 % | 3,5 c$ | 7 s | 8/8 |
| Claude Opus 5.5, pointage | 48 | 4 | 93 % | 7,5 c$ | 10 s | 8/8 |
| **Claude Haiku 5.5 @low, pointage (défaut)** | **47** | 4 | 93 % | **0,25 c$** | 12 s | **8/8** |
| Consensus Mistral Small 4 + Ministral 14B (repli) | 36 | **2** | 95 % | 0,27 c$ | 16 s | 5/8, 0 faux |
| Claude Opus 5.5, *lecture* | 40 | 3 | 94 % | — | 38 s | — |
| Mistral Medium, *lecture* | 9 | 20 | 71 % | — | 10 s | — |
| OCR seul, sans IA | 26 | 3 | 91 % | 0 | — | 4/8 |

Enseignements :

1. **Le pointage bat la lecture** pour tous les modèles : moins d'inventions.
2. **Haiku 5.5 en effort « low »** fait presque aussi bien qu'Opus, pour 1/30ᵉ du prix, et obtient 8/8 sur piece_003. Augmenter l'effort de réflexion n'aide pas le pointage.
3. Mistral Large 4 a le meilleur score, mais il est trop lent (près de 2 minutes par plan) et coûte 20 fois plus.
4. Le **consensus de petits modèles Mistral** est celui qui se trompe le moins, mais il laisse beaucoup de cases vides. C'est un bon repli.
5. La **lecture « planche » de Mistral OCR** apporte un vrai gain : score 36 contre 27 en OCR local avec la même IA.

### 3.6 Fiabilité et traçabilité
- **Cache disque** des réponses IA et OCR : rejouer une analyse ne coûte rien, et le banc d'essai est reproductible.
- **Bascule automatique** : si Claude ne répond pas (crédit épuisé, panne), on passe au consensus Mistral, et l'interface affiche quelle IA a été utilisée.
- **Valeurs proposées par l'IA seule** : elles doivent toujours être confirmées d'un clic.
- **Journal d'apprentissage** (base de données, tables `plans`, `analyses`, `decisions`) : chaque plan, la proposition de la machine et chaque décision de l'utilisateur sont enregistrés. Les décisions sont de quatre types : validation, correction, infirmation, ajout. On sait aussi si la machine était incertaine. C'est la base pour réapprendre et pour enrichir le banc d'essai avec de vrais cas.
- **Paramètres atelier versionnés** en base : un devis émis garde les prix du jour, et chaque devis PDF est archivé.
- **Journaux du serveur** : chaque appel OCR et IA (durée, coût, échec), chaque repli et chaque requête, dans la console et sur `GET /logs`.
- **24 tests automatiques**, hors ligne et déterministes.

---

## 4. La configuration retenue

| Étape | Choix | Pourquoi |
|---|---|---|
| Texte du plan | Texte du PDF si vectoriel, sinon **OCR « planche »** (repérage local + Mistral OCR) | 28/28 annotations lues sur piece_003, +9 points au banc d'essai |
| Compréhension | **Claude Haiku 5.5, effort low, en pointage** | 8/8 sur piece_003, 93 % de précision, 0,25 c$, 12 s |
| Repli | **Consensus Mistral Small 4 + Ministral 14B** | Le moins d'erreurs, prend le relais si Claude ne répond pas |
| Sans clé API | OCR local + règles | Fonctionne hors ligne, sans IA |
| Décision finale | **L'utilisateur**, guidé vers les seuls points douteux | Principe « pas de valeur plutôt qu'une fausse » |

**Coût total estimé : environ 1 c$ par plan** (OCR + IA), pour 30 à 40 s d'analyse.

---

## 5. Limites connues

- **Back séquentiel** : un plan à la fois. L'interface gère déjà le parallèle, il faudra une vraie file côté serveur (job + statut).
- **Une seule page affichée** : sur un PDF multi-pages, on lit toutes les pages, mais on ne montre que la première.
- **Prix simulés** : `config/pricing.json` donne des ordres de grandeur, il reste à le calibrer avec les tarifs réels de l'atelier.
- **Banc d'essai encore petit** : 11 plans au moment des mesures, 10 aujourd'hui (le plan Lasermaster a été retiré des exemples). Le journal d'apprentissage servira à l'agrandir avec des cas réels.
- **Restent difficiles** : les plans très chargés (développés, nomenclatures), les plans en pouces, les matières absentes du cartouche.
- **Pas d'authentification** et pas de gestion multi-utilisateur ; `/logs`, `/quotes` et `PUT /pricing` sont ouverts à tous ceux qui accèdent à l'application.
- **Images Docker pas encore testées** : la virtualisation n'est pas disponible sur le poste de développement.

---

## 6. Avant de déployer : points à trancher

1. **Hébergement et secrets** : les clés `CLAUDE_API_KEY` et `MISTRAL_API_KEY` doivent être des variables d'environnement du serveur, jamais dans l'image.
2. **Données clients** : la base (table `plans`) conserve les plans des clients. Il faut choisir où les stocker, combien de temps, qui y a accès (RGPD et confidentialité). L'enregistrement se coupe avec `DEVIS_LEARNING=0`.
3. **Envoi des plans à des API externes** : Mistral et Anthropic. Il faut le dire aux clients, ou n'utiliser que l'OCR local pour certains d'entre eux.
4. **Accès** : au minimum une authentification simple devant l'application.
5. **Parallélisme** : une file de traitement côté serveur, ou plusieurs workers uvicorn, si plusieurs utilisateurs travaillent en même temps.
6. **Cache** : `.cache/` grossit avec le temps. Prévoir un volume persistant et un nettoyage.
7. **Empaquetage** : ✅ `Dockerfile` + `docker-compose.yml` (application + PostgreSQL, healthcheck sur `/health`). Reste à construire et tester les images sur une machine avec virtualisation.
8. **Suivi des coûts** : le coût par plan est déjà calculé, il reste à l'exposer (par jour, par client).

---

## 7. Où regarder

| Pour… | Fichier |
|---|---|
| Lancer, configurer, commandes | `README.md` |
| Résultats détaillés plan par plan | `out/bench_vlm.md`, `out/bench_ocr.md` |
| Devis générés | `out/devis_*.pdf` |
| Vérité terrain | `tests/fixtures/benchmark_truth.json` |
| Plans de test et leurs sources | `samples/`, `samples/SOURCES.md` |
| Chaîne complète | `devis/pipeline.py` |
| Stratégie « pointage » | `devis/vlm_marks.py` |
| OCR « planche » | `devis/ocr_mistral.py` |
| Paramètres de prix | `config/pricing.json` (valeurs initiales), puis la base |
| Schéma d'architecture | `docs/architecture.tldr` (tldraw.com) |
| Réponses au brief | `REPONSES_CAS_PRATIQUE.md` |
| Déploiement | `Dockerfile`, `docker-compose.yml` |
