import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type * as React from "react";
import { SOURCE_INFO, fieldDef, formatValue, isEmpty, needsReview } from "../fields";
import { pickToken } from "../pick";
import type { Box, Extraction, Field, OcrToken } from "../types";

const MAX_ZOOM = 8;

/** Comment le texte du plan a été obtenu, en clair. */
export function readingLabel(src: string): string {
  if (src === "pdf-text") return "Texte lu directement dans le PDF";
  if (src === "ocr:mistral_board") return "Texte reconnu sur l'image : repérage OCR local + lecture Mistral OCR";
  if (src.startsWith("ocr:mistral")) return "Texte reconnu sur l'image : Mistral OCR";
  return "Texte reconnu sur l'image : OCR local (PP-OCR)";
}

/** « Claude (claude-haiku-5-5) — pointage » -> « Claude Haiku 5.5, pointage » */
export function aiLabel(p: string): string {
  const m = p.match(/^(\w+) \(([^)]+)\)(?: — (\w+))?/);
  if (!m) return p;
  const pretty = (id: string) => id.replace(/-latest$/, "").replace(/^claude-/, "")
    .replace(/-(\d+)-(\d+)$/, " $1.$2").replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const models = m[2].split("+").map(pretty);
  const who = models.length > 1 ? `${m[1]}, consensus ${models.join(" + ")}` : m[1] === "Claude" ? `Claude ${models[0]}` : models[0];
  return m[3] ? `${who} (${m[3]})` : who;
}

/** Zones où un champ a été lu (une par texte), ou l'enveloppe à défaut. */
export const zonesOf = (f?: Field): Box[] => (f?.boxes?.length ? f.boxes : f?.box ? [f.box] : []);

/**
 * Aperçu du plan.
 * - cadres verts / rouges : où chaque information a été lue (ok / à vérifier)
 * - sélection d'un champ ou d'une catégorie : ses zones sont surlignées, le reste
 *   du plan est assombri et la vue zoome dessus.
 */
export interface PickState { key: string; added: string[] }

export function PlanViewer({ ext, active, onPick, pick, onToken, onEndPick }: {
  ext: Extraction; active: string[]; onPick: (keys: string[]) => void;
  pick: PickState | null; onToken: (t: OcrToken) => void; onEndPick: () => void;
}) {
  const [zoom, setZoom] = useState(1);
  const [showText, setShowText] = useState(false);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const k = ext.preview_scale;

  // Échap quitte le mode « choisir sur le plan »
  useEffect(() => {
    if (!pick) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onEndPick();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pick, onEndPick]);

  // Zones en px de l'aperçu ; on écarte celles des pages suivantes (PDF multi-pages, non affichées)
  const toPx = (b: Box) => ({ x0: b.x0 * k, y0: b.y0 * k, x1: b.x1 * k, y1: b.y1 * k });
  const onPage = (b: Box) => !!size && b.y0 * k < size.h;
  const activeZones = active.flatMap((key) => zonesOf(ext.fields[key]).filter(onPage).map(toPx));
  const offPage = active.some((key) => zonesOf(ext.fields[key]).some((b) => !onPage(b)));

  // Zoom automatique sur la sélection
  const target = useRef<{ cx: number; cy: number } | null>(null);
  const focusSelection = () => {
    if (!size || !activeZones.length) { target.current = null; setZoom(1); return; } // rien à montrer : pièce entière
    const x0 = Math.min(...activeZones.map((b) => b.x0)), x1 = Math.max(...activeZones.map((b) => b.x1));
    const y0 = Math.min(...activeZones.map((b) => b.y0)), y1 = Math.max(...activeZones.map((b) => b.y1));
    const span = Math.max((x1 - x0) / size.w, (y1 - y0) / size.h);
    target.current = { cx: (x0 + x1) / 2 / size.w, cy: (y0 + y1) / 2 / size.h };
    setZoom(span < 0.12 ? 2.5 : span < 0.3 ? 1.75 : 1.25);
    centerOn(); // si le zoom ne change pas, recentrer quand même
  };
  const showWhole = () => { target.current = null; setZoom(1); };
  useEffect(focusSelection, [active.join(","), size]); // eslint-disable-line react-hooks/exhaustive-deps

  // Position calculée à partir du zoom (et non mesurée) : fiable même pendant le re-rendu
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;
  const centerOn = () => requestAnimationFrame(() => {
    const el = scroller.current, t = target.current;
    if (!el || !t || !size) return;
    const stageW = el.clientWidth * zoomRef.current, stageH = (stageW * size.h) / size.w;
    el.scrollTo({ left: t.cx * stageW - el.clientWidth / 2, top: t.cy * stageH - el.clientHeight / 2, behavior: "smooth" });
  });
  // Zoom manuel ancré sur un point (molette / double-clic : le point sous la souris ne bouge pas)
  const anchor = useRef<{ fx: number; fy: number; ox: number; oy: number } | null>(null);
  useLayoutEffect(() => {
    const el = scroller.current, a = anchor.current;
    if (a && el && size) {
      anchor.current = null;
      const stageW = el.clientWidth * zoom, stageH = (stageW * size.h) / size.w;
      el.scrollLeft = a.fx * stageW - a.ox;
      el.scrollTop = a.fy * stageH - a.oy;
      return;
    }
    centerOn();
  }, [zoom]); // eslint-disable-line react-hooks/exhaustive-deps
  const zoomAt = (next: number, clientX?: number, clientY?: number) => {
    const el = scroller.current;
    const z = Math.min(MAX_ZOOM, Math.max(1, Math.round(next * 100) / 100));
    if (!el || !size || z === zoomRef.current) return;
    const r = el.getBoundingClientRect();
    const ox = clientX === undefined ? el.clientWidth / 2 : clientX - r.left;
    const oy = clientY === undefined ? el.clientHeight / 2 : clientY - r.top;
    const stageW = el.clientWidth * zoomRef.current, stageH = (stageW * size.h) / size.w;
    anchor.current = { fx: (el.scrollLeft + ox) / stageW, fy: (el.scrollTop + oy) / stageH, ox, oy };
    target.current = null;
    setZoom(z);
  };
  const zoomAtRef = useRef(zoomAt);
  zoomAtRef.current = zoomAt;

  // Molette = zoom (listener non passif pour empêcher le défilement de la page)
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomAtRef.current(zoomRef.current * Math.pow(1.0015, -e.deltaY), e.clientX, e.clientY);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // Glisser = déplacer le plan zoomé (un vrai glisser n'est pas pris pour un clic sur un cadre)
  const drag = useRef<{ x: number; y: number; sl: number; st: number; moved: boolean } | null>(null);
  const [dragging, setDragging] = useState(false);
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 || zoomRef.current <= 1) return;
    const el = scroller.current!;
    drag.current = { x: e.clientX, y: e.clientY, sl: el.scrollLeft, st: el.scrollTop, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current, el = scroller.current;
    if (!d || !el) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 5) return;
    if (!d.moved) { d.moved = true; setDragging(true); el.setPointerCapture?.(e.pointerId); }
    el.scrollLeft = d.sl - dx;
    el.scrollTop = d.st - dy;
  };
  const endDrag = () => {
    if (drag.current?.moved) setTimeout(() => setDragging(false), 0);
    drag.current = null;
  };
  const swallowClickAfterDrag = (e: React.MouseEvent) => { if (dragging) { e.stopPropagation(); e.preventDefault(); } };

  const pct = (b: { x0: number; y0: number; x1: number; y1: number }, pad = 5) => size && ({
    left: `${((b.x0 - pad) / size.w) * 100}%`, top: `${((b.y0 - pad) / size.h) * 100}%`,
    width: `${((b.x1 - b.x0 + 2 * pad) / size.w) * 100}%`, height: `${((b.y1 - b.y0 + 2 * pad) / size.h) * 100}%`,
  });

  return (
    <section className="card plan">
      <div className="plan-bar">
        <span className="muted small" title={`Lecture : ${ext.text_source}${ext.vlm_provider ? ` · IA : ${ext.vlm_provider}` : ""}`}>
          {readingLabel(ext.text_source)}
          {ext.vlm_used && ext.vlm_provider ? <> · IA : {aiLabel(ext.vlm_provider)}</> : ext.vlm_used ? " · IA" : " · sans IA"}
          {ext.vlm_error && <span className="warn-text" title={ext.vlm_error}> · IA indisponible : {ext.vlm_error}</span>}
        </span>
        <span className="spacer" />
        <label className="check small"><input type="checkbox" checked={showText} onChange={(e) => setShowText(e.target.checked)} /> Tout le texte lu</label>
        <button className="btn icon" onClick={() => zoomAt(zoom / 1.5)} disabled={zoom <= 1} aria-label="Dézoomer">−</button>
        <button className="btn link small zoom" onClick={showWhole} title="Revenir à la pièce entière">{Math.round(zoom * 100)} %</button>
        <button className="btn icon" onClick={() => zoomAt(zoom * 1.5)} disabled={zoom >= MAX_ZOOM} aria-label="Zoomer">+</button>
      </div>

      {pick ? (
        <div className="selection picking">
          <div className="sel-main">
            <b>Choisir sur le plan : {fieldDef(pick.key)?.label}</b>
          </div>
          <div className="small sel-where">
            {pick.added.length
              ? <>Ajouté : {pick.added.join(" · ")}. Cliquez un autre texte, ou terminez.</>
              : <>Cliquez le texte qui donne la valeur. Les textes utilisables sont en bleu ; zoomez à la molette si besoin. (Échap pour annuler)</>}
          </div>
          <div className="sel-actions">
            <button className={`btn small ${pick.added.length ? "primary" : ""}`} onClick={onEndPick}>{pick.added.length ? "Terminé" : "Annuler"}</button>
          </div>
        </div>
      ) : active.length > 0 && <Selection ext={ext} active={active} offPage={offPage} shown={activeZones.length}
        zoomed={zoom > 1} onWhole={showWhole} onFocus={focusSelection}
        onClear={() => { onPick([]); showWhole(); }} />}

      <div className={`plan-scroll ${pick ? "is-picking" : ""} ${zoom > 1 ? "can-pan" : ""} ${dragging ? "panning" : ""}`} ref={scroller}
        onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={endDrag} onPointerCancel={endDrag}
        onClickCapture={swallowClickAfterDrag}
        onDoubleClick={(e) => { if (!(e.target as HTMLElement).closest("button")) zoomAt(zoom * 2, e.clientX, e.clientY); }}>
        <div className="plan-stage" style={{ width: `${zoom * 100}%` }}>
          {ext.preview_png_b64 && (
            <img src={`data:image/png;base64,${ext.preview_png_b64}`} alt="Plan" draggable={false}
              onLoad={(e) => setSize({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} />
          )}
          {size && !pick && showText && ext.ocr_tokens.filter((t) => onPage(t.box)).map((t, i) => (
            <div key={"t" + i} className="pbox token" style={pct(toPx(t.box), 3)!} title={`${t.text} (${Math.round(t.conf * 100)} %)`} />
          ))}

          {/* Mode « choisir sur le plan » : chaque texte reconnu devient cliquable */}
          {size && pick && ext.ocr_tokens.filter((t) => onPage(t.box)).map((t, i) => {
            const { res } = pickToken(pick.key, t, ext.fields.units?.value);
            return (
              <button key={"p" + i} className={`pbox pick ${res.ok ? "ok" : "no"}`} style={pct(toPx(t.box), 4)!}
                disabled={!res.ok} onClick={() => onToken(t)}
                title={res.ok ? `« ${t.text} » → ${res.label}` : `« ${t.text} » : ${res.reason}`} />
            );
          })}

          {/* Projecteur : assombrit tout sauf les zones sélectionnées */}
          {size && !pick && activeZones.length > 0 && (
            <svg className="spotlight" viewBox={`0 0 ${size.w} ${size.h}`} preserveAspectRatio="none" aria-hidden>
              <defs>
                <mask id="spot">
                  <rect width={size.w} height={size.h} fill="white" />
                  {activeZones.map((b, i) => <rect key={i} x={b.x0 - 8} y={b.y0 - 8} width={b.x1 - b.x0 + 16} height={b.y1 - b.y0 + 16} rx={6} fill="black" />)}
                </mask>
              </defs>
              <rect width={size.w} height={size.h} fill="rgba(15,23,42,.45)" mask="url(#spot)" />
            </svg>
          )}

          {size && !pick && Object.entries(ext.fields).flatMap(([key, f]) => zonesOf(f).filter(onPage).map((b, i) => (
            <button key={key + i} data-field={key}
              className={`pbox field ${needsReview(f) ? "flag" : ""} ${active.includes(key) ? "on" : active.length ? "dim" : ""}`}
              style={pct(toPx(b))!} title={fieldDef(key)?.label ?? key} onClick={() => onPick([key])} />
          )))}
        </div>
      </div>
      <div className="plan-legend muted small">
        <span><i className="sw ok" /> information lue</span>
        <span><i className="sw flag" /> à vérifier</span>
        <span>Cliquez une information (à droite) pour voir où elle a été lue.</span>
        <span>Molette : zoomer · glisser : se déplacer · double-clic : zoom ×2</span>
      </div>
    </section>
  );
}

/** Bandeau : ce qui est sélectionné, sa valeur et d'où elle vient. */
function Selection({ ext, active, offPage, shown, zoomed, onWhole, onFocus, onClear }: {
  ext: Extraction; active: string[]; offPage: boolean; shown: number; zoomed: boolean;
  onWhole: () => void; onFocus: () => void; onClear: () => void;
}) {
  const single = active.length === 1 ? ext.fields[active[0]] : null;
  let where: string;
  const probable = single?.evidence?.match(/zone probable : (.*)$/)?.[1];
  if (shown && probable) where = `Proposé par l'IA — zone probable sur le plan : ${probable}`;
  else if (shown && single?.evidence) where = `Lu ici : « ${single.evidence.split(" | VLM")[0]} »`;
  else if (shown) where = `${shown} zone${shown > 1 ? "s" : ""} surlignée${shown > 1 ? "s" : ""} sur le plan`;
  else if (offPage) where = "Lu en page 2 ou suivante du PDF (non affichée ici)";
  else if (single?.source === "vlm") where = "Proposé par l'IA à partir de l'ensemble du plan : pas de zone précise";
  else if (single?.source === "user") where = "Saisi par vous";
  else if (single && isEmpty(single.value)) where = "Introuvable sur le plan";
  else where = "Aucune zone de lecture pour cette sélection";
  if (shown && offPage) where += " (+ page suivante du PDF)";

  return (
    <div className={`selection ${shown ? "" : "nozone"}`}>
      <div className="sel-main">
        <b>{active.length === 1 ? fieldDef(active[0])?.label : active.map((a) => fieldDef(a)?.label).join(", ")}</b>
        {single && <span className="sel-val">{formatValue(active[0], single.value)}</span>}
        {single && <span className="muted small">· {SOURCE_INFO[isEmpty(single.value) && single.source !== "user" ? "none" : single.source].label}</span>}
      </div>
      <div className="muted small sel-where" title={single?.evidence ?? undefined}>{where}</div>
      <div className="sel-actions">
        {zoomed
          ? <button className="btn small" onClick={onWhole}>⤢ Voir toute la pièce</button>
          : shown > 0 && <button className="btn small" onClick={onFocus}>⌕ Zoomer sur la zone</button>}
        <button className="btn link small" onClick={onClear} aria-label="Fermer la sélection" title="Fermer la sélection">✕</button>
      </div>
    </div>
  );
}
