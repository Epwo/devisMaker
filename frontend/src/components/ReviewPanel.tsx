import { createContext, useContext, useEffect, useRef, useState } from "react";
import { ALL_FIELDS, FIELD_GROUPS, LIST_COLUMNS, MATERIALS, OPTION_LABELS, SOURCE_INFO, fieldDef, formatValue, isEmpty, isEssential, needsReview, reviewReason, type FieldDef } from "../fields";
import { useJobs } from "../jobs";
import type { Field, Job } from "../types";
import { zonesOf } from "./PlanViewer";
import { TraceModal } from "./TraceModal";

/** Ouvre le panneau « Pourquoi cette valeur ? » (journal de décision du champ). */
const WhyCtx = createContext<(key: string) => void>(() => {});

function WhyButton({ k }: { k: string }) {
  const open = useContext(WhyCtx);
  return <button className="btn link small why" title="Voir comment cette valeur a été obtenue (OCR, règles, IA…)"
    onClick={(e) => { e.stopPropagation(); open(k); }}>Pourquoi ?</button>;
}

type OnActive = (keys: string[]) => void;
type PickProps = { picking: string | null; onStartPick: (key: string) => void; onEndPick: () => void };
type CardProps = { job: Job; def: FieldDef; field: Field; active: string[]; scrollToActive: boolean; onActive: OnActive } & PickProps;

export function ReviewPanel({ job, active, scrollToActive, onActive, picking, onStartPick, onEndPick }: { job: Job; active: string[]; scrollToActive: boolean; onActive: OnActive } & PickProps) {
  const pp = { picking, onStartPick, onEndPick };
  const fields = job.ext!.fields;
  const [why, setWhy] = useState<string | null>(null);
  const card = (d: FieldDef) => <ReviewCard key={d.key} job={job} def={d} field={fields[d.key]} active={active} scrollToActive={scrollToActive} onActive={onActive} {...pp} />;
  const same = (keys: string[]) => keys.length > 0 && keys.join() === active.join();
  const defs = ALL_FIELDS.filter((d) => d.key in fields);
  // Les champs à vérifier restent affichés à leur place une fois traités (avec un ✓),
  // sinon la carte disparaîtrait sous les yeux de l'utilisateur dès la première saisie.
  const [pinned] = useState(() => new Set(defs.filter((d) => needsReview(fields[d.key])).map((d) => d.key)));
  const essentials = defs.filter((d) => isEssential(d, fields) && pinned.has(d.key));
  const optional = defs.filter((d) => !isEssential(d, fields) && pinned.has(d.key));
  const left = essentials.filter((d) => needsReview(fields[d.key])).length;
  const optLeft = optional.filter((d) => needsReview(fields[d.key])).length;

  return (
    <WhyCtx.Provider value={setWhy}>
    <section className="card review">
      <div className="sec-head">
        <h2>{left ? `À vérifier pour chiffrer (${left})` : "Informations de la pièce"}</h2>
        {essentials.length > 0 && <span className="muted small">{essentials.length - left} / {essentials.length} faits</span>}
      </div>

      {/* Catégories : surligne sur le plan tout ce qui a été lu pour la catégorie */}
      <div className="cat-bar">
        <span className="muted small">Voir sur le plan :</span>
        {FIELD_GROUPS.map((g) => {
          const keys = g.fields.map((d) => d.key).filter((k) => zonesOf(fields[k]).length);
          return (
            <button key={g.title} className={`btn chip ${same(keys) ? "on" : ""}`} disabled={!keys.length}
              title={keys.length ? `Où ont été lues : ${keys.map((k) => fieldDef(k)?.label).join(", ")}` : "Rien de lu sur le plan pour cette catégorie"}
              onClick={() => onActive(same(keys) ? [] : keys)}>
              {g.title}{keys.length > 0 && <span className="count">{keys.length}</span>}
            </button>
          );
        })}
      </div>

      {left > 0 ? (
        <p className="muted small intro">
          Ces informations comptent dans le prix et manquent ou sont incertaines.
          Corrigez-les, ou confirmez-les si elles sont justes : le prix se met à jour aussitôt.
        </p>
      ) : (
        <div className="all-good">✓ Tout ce qui compte pour le prix est vérifié. Le devis est prêt.</div>
      )}
      {essentials.map(card)}

      {/* Tout le reste, toujours visible (rien de replié) : valeurs lues, facultatives, introuvables */}
      <div className="all">
        <div className="sec-head sub-head">
          <h2>Toutes les informations de la pièce</h2>
          {optLeft > 0 && <span className="muted small">{optLeft} facultative{optLeft > 1 ? "s" : ""} non trouvée{optLeft > 1 ? "s" : ""}</span>}
        </div>
        {FIELD_GROUPS.map((g) => {
          const rows = g.fields.filter((d) => d.key in fields && !essentials.includes(d));
          if (!rows.length) return null;
          return (
            <div key={g.title} className="grp">
              <h3>{g.title}</h3>
              {rows.map((d) => <ReadRow key={d.key} job={job} def={d} field={fields[d.key]} active={active} scrollToActive={scrollToActive} onActive={onActive} {...pp} />)}
            </div>
          );
        })}
      </div>
    </section>
    {why && fields[why] && <TraceModal k={why} field={fields[why]} onClose={() => setWhy(null)} />}
    </WhyCtx.Provider>
  );
}

function SourceTag({ field }: { field: Field }) {
  const s = isEmpty(field.value) && field.source !== "user" ? SOURCE_INFO.none : SOURCE_INFO[field.source];
  return <span className={`src ${s.tone}`} title={field.evidence ? `Lu : ${field.evidence}` : undefined}>{s.label}</span>;
}

/** Indique qu'une zone du plan correspond à ce champ (cliquer la carte la montre). */
function Locate({ field }: { field: Field }) {
  if (!zonesOf(field).length) return null;
  return <span className="locate" title="Cliquez la carte pour voir sur le plan où cette information a été lue">⌖ sur le plan</span>;
}

/** Sélectionne le champ sur le plan au clic ou à l'entrée dans un de ses champs de saisie. */
const selectHandlers = (key: string, active: string[], onActive: OnActive) => {
  const pick = () => { if (!(active.length === 1 && active[0] === key)) onActive([key]); };
  return { onClick: pick, onFocus: pick };
};

/** Fait défiler la carte en vue uniquement quand la sélection vient d'un clic sur le plan. */
function useScrollWhenPicked(ref: React.RefObject<HTMLElement | null>, isOn: boolean, scrollToActive: boolean) {
  useEffect(() => {
    if (!isOn || !scrollToActive || !ref.current) return;
    const r = ref.current.getBoundingClientRect();
    if (r.top < 70 || r.bottom > window.innerHeight) window.scrollBy({ top: r.top - window.innerHeight / 3, behavior: "smooth" });
  }, [isOn, scrollToActive]); // eslint-disable-line react-hooks/exhaustive-deps
}

function ReviewCard({ job, def, field, active, scrollToActive, onActive, picking, onStartPick, onEndPick }: CardProps) {
  const { confirmField } = useJobs();
  const ref = useRef<HTMLDivElement>(null);
  const isOn = active.includes(def.key);
  useScrollWhenPicked(ref, isOn, scrollToActive);
  const missing = isEmpty(field.value);
  const done = !needsReview(field);
  const state = done ? "done" : missing ? "missing" : "doubt";
  return (
    <div ref={ref} className={`rcard ${state} ${isOn ? "on" : ""} ${picking === def.key ? "picking" : ""}`} {...selectHandlers(def.key, active, onActive)}>
      <div className="rcard-head">
        <b>{done && "✓ "}{def.label}</b>
        <span className="spacer" />
        <Locate field={field} />
        <SourceTag field={field} />
        <WhyButton k={def.key} />
      </div>
      {!done && <p className="reason">{reviewReason(field)}</p>}
      <div className="input-row">
        <div className="grow"><FieldInput job={job} def={def} field={field} autoFocus={false} /></div>
        <PickButton def={def} picking={picking} onStartPick={onStartPick} onEndPick={onEndPick} />
      </div>
      <div className="rcard-foot">
        <span className="muted small">{def.help}</span>
        {!done && !missing && <button className="btn ok small" onClick={() => confirmField(job.id, def.key)}>✓ C'est correct</button>}
      </div>
    </div>
  );
}

function ReadRow({ job, def, field, active, scrollToActive, onActive, picking, onStartPick, onEndPick }: CardProps) {
  const { confirmField } = useJobs();
  const [edit, setEdit] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const isOn = active.includes(def.key);
  useScrollWhenPicked(ref, isOn, scrollToActive);
  const doubt = needsReview(field) && !isEmpty(field.value);
  return (
    <div ref={ref} className={`rrow ${isOn ? "on" : ""} ${doubt ? "doubt" : ""}`} {...selectHandlers(def.key, active, onActive)}
      title={doubt ? reviewReason(field) : undefined}>
      <span className="rrow-label">{def.label}</span>
      {edit ? (
        <div className="rrow-edit input-row">
          <div className="grow"><FieldInput job={job} def={def} field={field} autoFocus onDone={() => setEdit(false)} /></div>
          <PickButton def={def} picking={picking} onStartPick={(k) => { setEdit(false); onStartPick(k); }} onEndPick={onEndPick} />
        </div>
      ) : (
        <>
          <span className="rrow-val">{formatValue(def.key, field.value)}</span>
          <Locate field={field} />
          <SourceTag field={field} />
          <WhyButton k={def.key} />
          {doubt && <button className="btn ok small" onClick={(e) => { e.stopPropagation(); confirmField(job.id, def.key); }}>✓ Confirmer</button>}
          {def.kind !== "readonly" && <button className="btn link small" onClick={() => setEdit(true)}>Modifier</button>}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- saisie

function FieldInput({ job, def, field, autoFocus, onDone }: { job: Job; def: FieldDef; field: Field; autoFocus: boolean; onDone?: () => void }) {
  const { setField } = useJobs();
  const commit = (v: unknown) => { setField(job.id, def.key, v); onDone?.(); };

  if (def.kind === "readonly") return <span>{formatValue(def.key, field.value)}</span>;
  if (def.kind === "list") return <ListEditor rows={(field.value as any[]) ?? []} cols={LIST_COLUMNS[def.key]} onChange={(rows) => setField(job.id, def.key, rows)} onDone={onDone} />;
  if (def.kind === "notes") {
    return <textarea rows={3} defaultValue={((field.value as string[]) ?? []).join("\n")} autoFocus={autoFocus}
      onBlur={(e) => { if (!pickJustPressed()) commit(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean)); }} />;
  }
  const parse = (raw: string) => {
    const s = raw.trim();
    if (!s) return null;
    if (def.kind === "num") { const n = parseFloat(s.replace(",", ".")); return Number.isFinite(n) ? n : null; }
    return s;
  };
  return (
    <div className="inp">
      <input
        key={String(field.value)}
        type={def.kind === "num" ? "number" : "text"}
        step="any"
        inputMode={def.kind === "num" ? "decimal" : undefined}
        list={def.kind === "material" ? "materials" : undefined}
        defaultValue={field.value == null ? "" : String(field.value)}
        placeholder="À saisir"
        autoFocus={autoFocus}
        onBlur={(e) => {
          if (pickJustPressed()) return; // on passe en sélection sur le plan : ne pas valider la saisie en cours
          const v = parse(e.target.value); if (v !== field.value) commit(v); else onDone?.();
        }}
        onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") onDone?.(); }}
      />
      {def.unit && <span className="unit">{def.unit}</span>}
      {def.kind === "material" && <datalist id="materials">{MATERIALS.map((m) => <option key={m} value={m} />)}</datalist>}
    </div>
  );
}

function ListEditor({ rows, cols, onChange, onDone }: {
  rows: any[]; cols: { key: string; label: string; type: string | string[] }[]; onChange: (rows: any[]) => void; onDone?: () => void;
}) {
  const update = (i: number, key: string, raw: string, type: string | string[]) => {
    const v = Array.isArray(type) ? raw : type === "int" ? Math.max(1, parseInt(raw) || 1) : type === "num" ? (raw === "" ? null : parseFloat(raw.replace(",", "."))) : raw;
    onChange(rows.map((r, j) => (j === i ? { ...r, [key]: v } : r)));
  };
  const add = () => onChange([...rows, Object.fromEntries(cols.map((c) => [c.key, Array.isArray(c.type) ? c.type[0] : c.type === "int" ? 1 : c.type === "text" ? "" : null]))]);
  return (
    <div className="list-ed">
      {rows.length > 0 && (
        <table>
          <thead><tr>{cols.map((c) => <th key={c.key}>{c.label}</th>)}<th /></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                {cols.map((c) => (
                  <td key={c.key}>
                    {Array.isArray(c.type) ? (
                      <select value={r[c.key] ?? c.type[0]} onChange={(e) => update(i, c.key, e.target.value, c.type)}>
                        {c.type.map((o) => <option key={o} value={o}>{OPTION_LABELS[o] ?? o}</option>)}
                      </select>
                    ) : (
                      <input type={c.type === "text" ? "text" : "number"} step="any" defaultValue={r[c.key] ?? ""}
                        onBlur={(e) => { if (!pickJustPressed()) update(i, c.key, e.target.value, c.type); }} />
                    )}
                  </td>
                ))}
                <td><button className="btn link danger" onClick={() => onChange(rows.filter((_, j) => j !== i))} aria-label="Supprimer">✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="list-actions">
        <button className="btn small" onClick={add}>+ Ajouter une ligne</button>
        {rows.length === 0 && <button className="btn small" onClick={() => onChange([])}>Il n'y en a pas</button>}
        {onDone && <button className="btn link small" onClick={onDone}>Fermer</button>}
      </div>
    </div>
  );
}

/** Instant où l'on a appuyé sur « Choisir sur le plan » : la saisie en cours ne doit PAS être validée par
 *  la perte de focus qui suit (sinon la valeur tapée est enregistrée et la sélection ne démarre pas). */
let pickPressedAt = 0;
const pickJustPressed = () => Date.now() - pickPressedAt < 1000;

/** Remplir le champ en cliquant un texte reconnu sur le plan, au lieu de le taper. */
function PickButton({ def, picking, onStartPick, onEndPick }: { def: FieldDef } & PickProps) {
  if (def.kind === "readonly") return null;
  const on = picking === def.key;
  return (
    <button className={`btn small pick-btn ${on ? "on" : ""}`}
      onMouseDown={(e) => { pickPressedAt = Date.now(); e.preventDefault(); /* garde le focus : pas de validation */ }}
      onClick={(e) => { e.stopPropagation(); if (on) onEndPick(); else onStartPick(def.key); }}
      title="Cliquer le texte correspondant sur le plan pour remplir ce champ">
      {on ? "Annuler" : "⌖ Choisir sur le plan"}
    </button>
  );
}
