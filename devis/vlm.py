"""Secours VLM : un modèle de vision (Claude ou Mistral) lit le plan pour les champs
que l'OCR n'a pas trouvés ou dont il n'est pas sûr.

Un seul appel par plan : on envoie l'image + les textes OCR (comme indices) et
on demande uniquement les champs incertains, avec une sortie JSON contrainte
par schéma (structured outputs).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import logging

import anthropic

from .ingest import to_png
from .schemas import FIELD_SPECS, OcrToken

log = logging.getLogger(__name__)

MODEL = os.getenv("DEVIS_VLM_MODEL", "claude-haiku-5-5")  # le moins cher ; Opus est surdimensionné pour le pointage
EFFORT = os.getenv("DEVIS_VLM_EFFORT", "low")  # Haiku 5.5 @low : 8/8 sur piece_003, 0,25 c$/plan, 12 s
IMAGE_MAX_SIDE = int(os.getenv("DEVIS_VLM_IMAGE_MAX_SIDE", "2400"))

SYSTEM = """Tu es technicien méthodes en tôlerie / découpe laser. Tu lis des plans techniques \
(scans, projection européenne ou américaine) pour préparer un devis de fabrication.

Règles :
- Réponds uniquement pour les champs demandés, à partir de ce qui est visible sur le plan.
- Si une information n'est pas présente ni déductible du plan (cartouche masqué, matière non indiquée...), \
mets value à null. N'invente jamais une référence, un client ou une matière.
- Tu peux déduire ce qui est géométriquement certain (ex. nombre de plis visible sur la vue de profil, \
épaisseur cotée sur une vue, encombrement à partir des cotes). Indique alors la déduction dans evidence.
- Le flan développé peut être estimé (longueurs droites + plis avec rayon intérieur et facteur K ≈ 0,4) : \
écris "estimé" dans evidence et une confiance ≤ 0,7.
- Encombrement (length_mm ≥ width_mm ≥ height_mm) = dimensions HORS-TOUT de la pièce finie pliée, c'est-à-dire sa boîte englobante. Utilise les cotes extérieures qui couvrent toute la pièce dans chaque vue (en général la plus grande cote de chaque direction), jamais une cote de position de trou ou une cote partielle. Recoupe les vues : une même dimension apparaît souvent dans deux vues (ex. vue de dessus et vue de profil). Pour une pièce pliée, la hauteur est la cote hors-tout perpendiculaire à la tôle sur la vue de profil.
- Épaisseur : petite cote (souvent 1 à 6 mm) placée sur la tranche de la tôle dans la vue de profil.
- Trous : uniquement les cotes avec le symbole Ø (ou M pour un taraudage). « 2x 5x5 » ou « 20x20 » sont des chanfreins / découpes d'angle, pas des trous.
- Plis : compte-les sur la vue de profil (chaque changement de direction de la tôle = 1 pli).
- Dimensions en mm, angles en degrés. Quantités par pièce.
- Les textes OCR fournis sont des indices bruités (Ø souvent lu 0, virgules décimales, lettres confondues) : \
le plan fait foi.
- confidence ∈ [0,1] : 0,95+ uniquement si la valeur est lue sans ambiguïté sur le plan."""


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _value_schema(kind: str) -> dict:
    num, integer, string = {"type": "number"}, {"type": "integer"}, {"type": "string"}
    if kind == "string":
        return _nullable(string)
    if kind == "number":
        return _nullable(num)
    if kind == "string_list":
        return {"type": "array", "items": string}
    if kind == "holes":
        return {"type": "array", "items": _obj({
            "diameter_mm": num, "quantity": integer,
            "kind": {"type": "string", "enum": ["through", "threaded", "insert"]},
            "tolerance": _nullable(string),
        })}
    if kind == "cutouts":
        return {"type": "array", "items": _obj({"description": string, "quantity": integer})}
    if kind == "bends":
        return {"type": "array", "items": _obj({"angle_deg": _nullable(num), "radius_mm": _nullable(num), "quantity": integer})}
    if kind == "components":
        return {"type": "array", "items": _obj({"designation": string, "quantity": integer})}
    raise ValueError(kind)


VIEW_TYPES = ["dessus", "face", "cote", "isometrique", "autre"]
DIM_FIELDS = ("length_mm", "width_mm", "height_mm")


def build_schema(field_names: list[str], with_views: bool = True) -> dict:
    props = {
        name: _obj({
            "value": _value_schema(FIELD_SPECS[name]["type"]),
            "confidence": {"type": "number"},
            "evidence": {"type": "string"},
        })
        for name in field_names
    }
    if with_views:
        # Lecture vue par vue des cotes hors-tout : l'encombrement est ensuite recalculé par le code,
        # plus fiable que de demander directement « largeur » (les modèles confondent cote partielle et hors-tout).
        props["views"] = {"type": "array", "items": _obj({
            "view": {"type": "string", "enum": VIEW_TYPES},
            "overall_horizontal_mm": _nullable({"type": "number"}),
            "overall_vertical_mm": _nullable({"type": "number"}),
            "evidence": {"type": "string"},
        })}
    return _obj(props)


def bbox_from_views(views: list[dict]) -> tuple[list[float], str] | None:
    """Encombrement (L >= l >= H) à partir des cotes hors-tout de chaque vue (projection européenne :
    dessus = X x Y, face = X x Z, côté = Y x Z). None si les vues ne suffisent pas."""
    axes: dict[str, list[float]] = {"X": [], "Y": [], "Z": []}
    mapping = {"dessus": ("X", "Y"), "face": ("X", "Z"), "cote": ("Y", "Z")}
    used = []
    for v in views or []:
        if v.get("view") not in mapping:
            continue
        hx, vx = mapping[v["view"]]
        h, vv = v.get("overall_horizontal_mm"), v.get("overall_vertical_mm")
        if isinstance(h, (int, float)) and h > 0:
            axes[hx].append(float(h))
        if isinstance(vv, (int, float)) and vv > 0:
            axes[vx].append(float(vv))
        if h or vv:
            used.append(f"{v['view']} {h or '?'} x {vv or '?'}")
    if not all(axes.values()):
        return None
    dims = sorted((max(a) for a in axes.values()), reverse=True)
    return dims, "déduit des cotes hors-tout des vues : " + ", ".join(used)


def apply_views(answers: dict, field_names: list[str]) -> dict:
    """Remplace l'encombrement proposé directement par celui recalculé à partir des vues, s'ils diffèrent."""
    res = bbox_from_views(answers.get("views") or [])
    if not res:
        return answers
    dims, why = res
    for name, val in zip(DIM_FIELDS, dims):
        if name not in field_names:
            continue
        cur_ans = answers.get(name) or {}
        cur = cur_ans.get("value")
        if not isinstance(cur, (int, float)):
            answers[name] = {"value": val, "confidence": 0.8, "evidence": why}
        elif abs(cur - val) > 0.5:
            # L'IA se contredit (réponse directe ≠ calcul par les vues) : on garde le calcul par les vues
            # mais avec une confiance basse, pour que l'utilisateur tranche (bouton « Choisir sur le plan »).
            answers[name] = {"value": val, "confidence": 0.5,
                             "evidence": f"{why} — réponse directe de l'IA : {cur:g}",
                             "conflict": f"L'IA hésite entre {val:g} et {cur:g} mm : à vérifier"}
    return answers


def build_prompt(field_names: list[str], tokens: list[OcrToken], known: dict[str, Any]) -> str:
    ocr_lines = "\n".join(
        f"- ({t.box.x0:.0f},{t.box.y0:.0f}){' [vertical]' if t.vertical else ''} {t.text}  (conf {t.conf:.2f})"
        for t in sorted(tokens, key=lambda t: (t.box.y0, t.box.x0))
    )
    wanted = "\n".join(f"- {n} : {FIELD_SPECS[n]['desc']}" for n in field_names)
    known_txt = json.dumps(known, ensure_ascii=False, default=str, indent=1) if known else "{}"
    return f"""Voici un plan de pièce de tôlerie à deviser.

Champs à déterminer (l'OCR ne les a pas trouvés ou n'en est pas sûr) :
{wanted}

Champs déjà lus avec certitude par l'OCR (contexte, ne pas renvoyer) :
{known_txt}

Textes détectés par l'OCR (coordonnées en px sur l'image, indices bruités) :
{ocr_lines}

Pour les listes (trous, plis, découpes, composants), renvoie la liste complète telle que tu la lis sur le plan, \
pas seulement les éléments manquants.

Champ « views » (toujours) : pour CHAQUE vue du plan (dessus, face/profil, côté, isométrique), donne le type de vue \
et ses cotes HORS-TOUT horizontale et verticale (distance entre les extrémités de la pièce dans cette vue, cotée ou \
somme de cotes ; sinon null). Jamais une cote de position de trou ni une cote d'une seule aile."""


class VlmError(RuntimeError):
    pass


def _view_crops(page_img) -> list:
    """Vues du dessin recadrées (positions données par Mistral OCR si disponible), pour que le modèle lise
    les petites cotes. Liste vide sinon : seule la page entière est envoyée."""
    if not os.getenv("MISTRAL_API_KEY"):
        return []
    try:
        from .ocr_mistral import view_regions

        regions = view_regions(page_img)
    except Exception:
        return []
    h, w = page_img.shape[:2]
    crops = []
    for b in sorted(regions, key=lambda b: (b.y1 - b.y0) * (b.x1 - b.x0), reverse=True)[:4]:
        x0, y0 = max(0, int(b.x0) - 15), max(0, int(b.y0) - 15)
        x1, y1 = min(w, int(b.x1) + 15), min(h, int(b.y1) + 15)
        if x1 - x0 > 150 and y1 - y0 > 150:
            crops.append(page_img[y0:y1, x0:x1])
    return crops


def provider() -> str | None:
    """DEVIS_VLM_PROVIDER (claude | mistral) ; par défaut le premier dont la clé est configurée.
    Sans clé Claude, Mistral est utilisé (même si Claude est demandé) ; sans aucune clé : None (lecture OCR seule)."""
    has = {"claude": bool(_claude_key()), "mistral": bool(os.getenv("MISTRAL_API_KEY"))}
    p = (os.getenv("DEVIS_VLM_PROVIDER") or "auto").strip().lower()
    if p in has and has[p]:
        return p
    # Claude d'abord : tous les modèles Claude (y compris Haiku 5.5, le moins cher) comprennent les vues de piece_003
    # (8/8), les petits modèles Mistral non. Si Claude échoue (crédit, réseau), le pipeline bascule sur le consensus
    # Mistral, qui fait le moins d'erreurs (mais laisse plus de champs vides).
    if _claude_key():
        return "claude"
    if os.getenv("MISTRAL_API_KEY"):
        return "mistral"
    return None


def _claude_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")


# Consensus de deux petits modèles : 95 % de précision au banc d'essai pour 0,26 c$ / plan (Large 4 : 5 c$, 91 s)
MISTRAL_MODEL = os.getenv("DEVIS_MISTRAL_VLM_MODEL", "mistral-small-latest+ministral-14b-latest")


def default_model(prov: str | None) -> str | None:
    return {"claude": MODEL, "mistral": MISTRAL_MODEL}.get(prov or "")


def provider_label(prov: str | None = None, model: str | None = None) -> str | None:
    prov = prov or provider()
    if not prov:
        return None
    return f"{'Claude' if prov == 'claude' else 'Mistral'} ({model or default_model(prov)})"


# --------------------------------------------------------------------------- appel générique image(s) + texte -> JSON

Part = tuple  # ("text", str) | ("image", np.ndarray)


def _png_b64(img, max_side=None) -> str:
    return base64.b64encode(to_png(img, max_side)).decode()


VLM_CACHE = Path(__file__).resolve().parents[1] / ".cache" / "vlm"


# Prix publics (USD par million de tokens, entrée / sortie) — pour estimer le coût d'une analyse
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
    "mistral-large-4": (1.36, 4.18), "mistral-medium-latest": (1.5, 7.5),
    "mistral-small-latest": (0.15, 0.60), "ministral-14b-latest": (0.20, 0.20), "ministral-8b-latest": (0.15, 0.15),
}

_LAST = threading.local()


def last_usage() -> dict | None:
    """Consommation du dernier appel IA de ce thread : tokens, coût estimé (USD), servi depuis le cache ou non."""
    return getattr(_LAST, "usage", None)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    p = PRICES.get(model)
    return round((input_tokens * p[0] + output_tokens * p[1]) / 1e6, 5) if p else None


def call_vision_json(prov: str, model: str, system: str, parts: list[Part], schema: dict, max_tokens: int = 32000,
                     effort: str | None = None) -> dict:
    """Appel avec cache disque : une même requête (modèle, effort, consignes, images, schéma) n'est payée qu'une fois.
    DEVIS_VLM_CACHE=0 pour le désactiver."""
    effort = effort or EFFORT
    h = hashlib.sha1()
    keys = (prov, model, system, json.dumps(schema, sort_keys=True))
    if prov == "claude" and effort != "high":
        keys += (effort,)
    for x in keys:
        h.update(str(x).encode())
    for k, v in parts:
        h.update(v.encode() if k == "text" else v.tobytes())
    path = VLM_CACHE / f"{h.hexdigest()}.json"
    use_cache = os.getenv("DEVIS_VLM_CACHE", "1") != "0"
    if use_cache and path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(stored, dict) and "_data" in stored:
            _LAST.usage = {**(stored.get("_usage") or {}), "cache": True}
            log.info("IA %s/%s : réponse en cache (%s)", prov, model, path.stem[:8])
            return stored["_data"]
        _LAST.usage = {"model": model, "cache": True}
        return stored
    t0 = time.time()
    n_img = sum(1 for k, _ in parts if k != "text")
    log.info("IA %s/%s : appel (%d image(s), effort %s)…", prov, model, n_img, effort if prov == "claude" else "-")
    try:
        out, (tin, tout) = _call_vision_json(prov, model, system, parts, schema, max_tokens, effort)
    except VlmError as e:
        log.warning("IA %s/%s : ÉCHEC après %.1f s : %s", prov, model, time.time() - t0, e)
        raise
    except Exception:
        log.exception("IA %s/%s : erreur inattendue après %.1f s", prov, model, time.time() - t0)
        raise
    cost = cost_usd(model, tin, tout)
    log.info("IA %s/%s : réponse en %.1f s, %d tokens entrée / %d sortie, %s", prov, model, time.time() - t0,
             tin, tout, f"{cost * 100:.3f} c$" if cost is not None else "coût inconnu")
    _LAST.usage = {"model": model, "effort": effort if prov == "claude" else None, "input_tokens": tin,
                   "output_tokens": tout, "cost_usd": cost_usd(model, tin, tout),
                   "seconds": round(time.time() - t0, 1), "cache": False}
    if use_cache:
        VLM_CACHE.mkdir(parents=True, exist_ok=True)
        usage = {k: v for k, v in _LAST.usage.items() if k != "cache"}
        path.write_text(json.dumps({"_data": out, "_usage": usage}, ensure_ascii=False), encoding="utf-8")
    return out


def _call_vision_json(prov: str, model: str, system: str, parts: list[Part], schema: dict, max_tokens: int = 32000,
                      effort: str = EFFORT) -> tuple[dict, tuple[int, int]]:
    """Un appel modèle de vision avec sortie JSON contrainte par schéma, Claude ou Mistral."""
    if prov == "mistral":
        from mistralai.client import Mistral

        content = [{"type": "text", "text": v} if k == "text" else
                   {"type": "image_url", "image_url": "data:image/png;base64," + _png_b64(v, IMAGE_MAX_SIDE)} for k, v in parts]
        try:
            r = Mistral(api_key=os.environ["MISTRAL_API_KEY"]).chat.complete(
                model=model, temperature=0, top_p=1, max_tokens=max(max_tokens, 32000),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                response_format={"type": "json_schema", "json_schema": {"name": "plan", "schema": schema, "strict": True}},
            )
        except Exception as e:  # erreurs HTTP / réseau du SDK Mistral
            raise VlmError(f"Mistral : {str(e)[:200]}") from e
        choice = r.choices[0]
        if choice.finish_reason == "length":
            raise VlmError("Réponse tronquée (max_tokens)")
        text = choice.message.content
        if isinstance(text, list):
            text = "".join(getattr(c, "text", "") or "" for c in text)
        usage = (getattr(r.usage, "prompt_tokens", 0) or 0, getattr(r.usage, "completion_tokens", 0) or 0)
    elif prov == "claude":
        client = anthropic.Anthropic(api_key=_claude_key())
        content = [{"type": "text", "text": v} if k == "text" else
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_b64(v, IMAGE_MAX_SIDE)}}
                   for k, v in parts]
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
                      output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
        # Repli serveur en cas de refus du filtre de sécurité (Opus 5.5 / Sonnet 5.5 / Fable 5.1 sur l'API Claude)
        if any(m in model for m in ("opus-5-5", "sonnet-5-5", "fable-5-1")):
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            try:
                with client.beta.messages.stream(**kwargs) as stream:
                    resp = stream.get_final_message()
            except anthropic.BadRequestError as e:
                if "grammar is too large" not in str(e) and "union types" not in str(e):
                    raise
                # Schéma trop gros pour la sortie contrainte : on le donne dans la consigne et on valide nous-mêmes
                kwargs["output_config"] = {"effort": effort}
                kwargs["messages"] = [{"role": "user", "content": content + [{"type": "text", "text":
                    "Réponds UNIQUEMENT par un objet JSON valide conforme à ce schéma JSON (aucun texte autour) :\n"
                    + json.dumps(schema, ensure_ascii=False)}]}]
                with client.beta.messages.stream(**kwargs) as stream:
                    resp = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise VlmError("Clé API Claude invalide ou absente (CLAUDE_API_KEY)") from e
        except anthropic.RateLimitError as e:
            raise VlmError("Limite de débit de l'API Claude atteinte, réessayer plus tard") from e
        except anthropic.APIStatusError as e:
            raise VlmError(f"Erreur API Claude ({e.status_code}) : {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise VlmError("API Claude injoignable (réseau)") from e
        if resp.stop_reason == "refusal":
            raise VlmError("Le modèle a refusé la requête")
        if resp.stop_reason == "max_tokens":
            raise VlmError("Réponse tronquée (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        u = resp.usage
        usage = (u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0), u.output_tokens)
    else:
        raise VlmError("Aucune clé d'IA configurée (CLAUDE_API_KEY / ANTHROPIC_API_KEY ou MISTRAL_API_KEY)")
    text = text or ""
    try:
        return json.loads(text), usage
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")  # réponse non contrainte : premier objet JSON du texte
        try:
            return (json.loads(text[a:b + 1]) if a >= 0 and b > a else json.loads(text)), usage
        except json.JSONDecodeError as e:
            raise VlmError(f"JSON VLM invalide : {e}") from e


# --------------------------------------------------------------------------- stratégie « lecture » : l'IA lit les valeurs

def ask_vlm(page_img, field_names: list[str], tokens: list[OcrToken], known: dict[str, Any],
            prov: str | None = None, model: str | None = None, effort: str | None = None) -> dict[str, dict]:
    """Retourne {champ: {"value", "confidence", "evidence"}}."""
    prov = prov or provider()
    model = (model or default_model(prov)).split("+")[0]  # la lecture n'utilise qu'un modèle (pas de consensus)
    parts: list[Part] = [("image", page_img)]
    for i, c in enumerate(_view_crops(page_img)):
        parts += [("text", f"Vue {i + 1} du plan, agrandie :"), ("image", c)]
    parts.append(("text", build_prompt(field_names, tokens, known)))
    return apply_views(call_vision_json(prov, model, SYSTEM, parts, build_schema(field_names), effort=effort), field_names)
