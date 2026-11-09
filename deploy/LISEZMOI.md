# DevisMaker : tester sur votre infrastructure

Image publiée : `docker.io/CHANGER/devismaker` (linux/amd64 et linux/arm64).
Il faut **Docker 24+ avec Compose v2**. Les deux fichiers de ce dossier suffisent, le code source n'est pas nécessaire.

```bash
cp .env.example .env      # renseigner POSTGRES_PASSWORD (+ clés API si souhaité)
docker compose up -d
```

Ouvrir ensuite **http://localhost:8000**. Le premier démarrage prend environ 30 s.
Les plans d'exemple sont proposés dans l'interface.

## Clés API

| Configuration | Résultat |
|---|---|
| aucune clé | OCR local seul, sans IA : gratuit et hors ligne, mais moins d'informations trouvées |
| `MISTRAL_API_KEY` | lecture Mistral OCR, et l'IA Mistral sert de repli |
| `CLAUDE_API_KEY` + `MISTRAL_API_KEY` | configuration recommandée (celle du banc d'essai), environ 0,3 centime par plan |

Les clés restent dans votre `.env`. L'image n'en contient aucune.

## Vérifier et suivre

```bash
curl http://localhost:8000/health        # version, IA disponible, base joignable
docker compose logs -f app               # journaux (appels IA, échecs, durées)
docker compose pull && docker compose up -d   # passer à une nouvelle version
docker compose down                      # arrêter (données conservées ; -v pour tout effacer)
```

- API documentée : http://localhost:8000/docs
- Journaux récents : http://localhost:8000/logs?level=WARNING

Les données restent dans deux volumes : `pgdata` pour la base (analyses, décisions, devis, paramètres atelier)
et `cache` pour les réponses OCR et IA déjà payées.
