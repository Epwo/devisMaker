"""Modèles de données de l'extraction.

Chaque information extraite d'un plan est un `Field` qui garde sa valeur, son
niveau de confiance, sa source (OCR, VLM, règle, utilisateur) et la preuve
(texte OCR / justification VLM). C'est ce qui permet ensuite de n'envoyer au
VLM que ce qui est incertain, et d'afficher à l'utilisateur ce qu'il doit
vérifier.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field as PField


class Source(str, Enum):
    OCR = "ocr"          # lu directement par l'OCR + règles
    VLM = "vlm"          # fourni par le modèle vision (Claude)
    OCR_VLM = "ocr+vlm"  # OCR confirmé par le VLM
    DEFAULT = "default"  # valeur par défaut métier
    USER = "user"        # corrigé à la main
    NONE = "none"        # rien trouvé


class Box(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class OcrToken(BaseModel):
    text: str
    conf: float
    box: Box
    vertical: bool = False  # lu sur l'image tournée (cote verticale)
    alt: Optional[str] = None  # autre lecture du même texte (OCR local vs Mistral) quand elles diffèrent


class Hole(BaseModel):
    diameter_mm: float
    quantity: int = 1
    kind: str = "through"  # through | threaded | insert
    tolerance: Optional[str] = None


class Cutout(BaseModel):
    description: str  # ex. "chanfrein 20x20"
    quantity: int = 1


class Bend(BaseModel):
    angle_deg: Optional[float] = None
    radius_mm: Optional[float] = None
    quantity: Optional[int] = 1  # None = nombre de plis inconnu (seul le rayon a été lu)


class Component(BaseModel):
    designation: str
    quantity: int = 1


class Field(BaseModel):
    value: Any = None
    confidence: float = 0.0
    source: Source = Source.NONE
    evidence: Optional[str] = None
    box: Optional[Box] = None                      # enveloppe de toutes les zones
    boxes: list[Box] = PField(default_factory=list)  # une zone par texte lu (pour surligner sur le plan)
    issues: list[str] = PField(default_factory=list)
    vlm_value: Any = None  # valeur proposée par le VLM quand elle diffère de l'OCR
    # Journal de décision : chaque étape (lecture, règle, contrôle, IA, fusion, utilisateur) qui a mené à la valeur
    trace: list[dict[str, Any]] = PField(default_factory=list)

    @property
    def missing(self) -> bool:
        return self.value is None or self.value == [] or self.value == ""


# Liste des champs extraits, avec leur type JSON (pour le schéma VLM) et une
# description métier. L'ordre est celui de l'affichage.
FIELD_SPECS: dict[str, dict[str, Any]] = {
    "units": {"type": "string", "desc": "Unité des cotes du plan : 'mm' ou 'in' (les valeurs renvoyées sont toujours en mm)"},
    "reference": {"type": "string", "desc": "Référence / numéro de plan de la pièce (cartouche)"},
    "designation": {"type": "string", "desc": "Désignation de la pièce (ex. SUPPORT)"},
    "client": {"type": "string", "desc": "Nom du client / donneur d'ordre (cartouche, logo)"},
    "material": {"type": "string", "desc": "Matière (ex. acier, inox, aluminium)"},
    "material_grade": {"type": "string", "desc": "Nuance matière (ex. S235, DC01, 304L, 5754)"},
    "thickness_mm": {"type": "number", "desc": "Épaisseur de la tôle en mm"},
    "surface_treatment": {"type": "string", "desc": "Traitement / finition (peinture, zingage, cataphorèse...)"},
    "length_mm": {"type": "number", "desc": "Encombrement de la pièce finie : plus grande dimension en mm"},
    "width_mm": {"type": "number", "desc": "Encombrement de la pièce finie : deuxième dimension en mm"},
    "height_mm": {"type": "number", "desc": "Encombrement de la pièce finie : hauteur (après pliage) en mm"},
    "flat_length_mm": {"type": "number", "desc": "Longueur du flan développé (avant pliage) en mm, estimée si non cotée"},
    "flat_width_mm": {"type": "number", "desc": "Largeur du flan développé (avant pliage) en mm"},
    "holes": {"type": "holes", "desc": "Trous : diamètre, quantité, type (through/threaded/insert)"},
    "cutouts": {"type": "cutouts", "desc": "Chanfreins, encoches, découpes particulières avec quantité"},
    "bends": {"type": "bends", "desc": "Plis : angle (°), rayon intérieur (mm), nombre"},
    "components": {"type": "components", "desc": "Composants rapportés de la nomenclature (écrous à sertir, inserts...) hors pièce principale"},
    "tolerance_standard": {"type": "string", "desc": "Norme de tolérances générales (ex. ISO 2768-m)"},
    "notes": {"type": "string_list", "desc": "Notes du plan utiles à la fabrication"},
}

LIST_FIELDS = {"holes", "cutouts", "bends", "components", "notes"}


class PartSpec(BaseModel):
    """Résultat de l'extraction pour un plan."""

    file_name: str
    fields: dict[str, Field]
    ocr_tokens: list[OcrToken] = PField(default_factory=list)
    text_source: str = "ocr"  # "ocr" (scan) ou "pdf-text" (PDF vectoriel)
    preview_png_b64: Optional[str] = None  # aperçu du plan pour l'interface
    preview_scale: float = 1.0             # px aperçu / px des boîtes OCR
    vlm_used: bool = False
    vlm_provider: Optional[str] = None  # ex. « Mistral (mistral-large-latest) »
    vlm_fields_requested: list[str] = PField(default_factory=list)
    vlm_error: Optional[str] = None
    warnings: list[str] = PField(default_factory=list)
    plan_id: Optional[str] = None      # empreinte du fichier (table plans)
    analysis_id: Optional[int] = None  # id de l'analyse en base (journal d'apprentissage)

    def needs_review(self) -> list[str]:
        # une valeur proposée par l'IA seule doit être confirmée par l'utilisateur
        return [k for k, f in self.fields.items()
                if f.source != Source.USER and (f.missing or f.confidence < 0.7 or f.issues or f.source == Source.VLM)]

    def values(self) -> dict[str, Any]:
        out = {}
        for k, f in self.fields.items():
            v = f.value
            if isinstance(v, list):
                v = [x.model_dump() if isinstance(x, BaseModel) else x for x in v]
            out[k] = v
        return out
