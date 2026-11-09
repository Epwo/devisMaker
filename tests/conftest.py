import os

# Tests déterministes et hors ligne : OCR local, pas d'appel Mistral/Claude même si des clés sont dans .env
os.environ["DEVIS_OCR_ENGINE"] = "rapid_v4"

# Base de données jetable (SQLite) : les tests n'écrivent jamais dans data/ ni dans PostgreSQL
import tempfile  # noqa: E402

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="devis_test_"), "test.db").replace("\\", "/")
