"""Prépare les modèles OCR locaux (build Docker) : charge chaque moteur et lit une image de test.

RapidOCR 3 télécharge ses modèles PP-OCRv5 au premier usage. Les lancer ici, sous l'utilisateur qui fera tourner
l'application, garantit qu'ils sont présents et lisibles : l'application n'a besoin d'aucun accès réseau ni droit
d'écriture pour l'OCR local. Échoue (code 1) si un moteur ne lit rien.
"""
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from devis.ocr import ENGINES  # noqa: E402

PLAN = Path(__file__).resolve().parents[1] / "samples" / "tmlaser_came_contact.png"
img = cv2.imread(str(PLAN), cv2.IMREAD_GRAYSCALE)
if img is None:
    sys.exit(f"plan de test introuvable : {PLAN}")

failed = []
for name in ("rapid_v4", "ppocr_v5", "ppocr_v5_srv"):  # rapid_v4 + ppocr_v5 = détection locale de mistral_board
    tokens = ENGINES[name](img)
    print(f"{name:13s} {len(tokens)} texte(s) lus")
    if not tokens:
        failed.append(name)
if failed:
    sys.exit(f"OCR local inopérant : {', '.join(failed)}")
print("modèles OCR prêts")
