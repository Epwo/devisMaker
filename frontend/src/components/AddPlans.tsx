import { useRef, useState, type DragEvent } from "react";
import { api } from "../api";
import { MAX_PARALLEL, useJobs } from "../jobs";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.jfif,.webp,.tif,.tiff,.bmp";

export function AddPlans({ samples, onAdded, onCancel, big }: {
  samples: string[]; onAdded: () => void; onCancel?: () => void; big?: boolean;
}) {
  const { addFiles, aiAvailable, pricing } = useJobs();
  const [files, setFiles] = useState<File[]>([]);
  const [over, setOver] = useState(false);
  const [quantity, setQuantity] = useState<number>(pricing?.default_quantity ?? 10);
  const [client, setClient] = useState("");
  const [useAi, setUseAi] = useState(true);
  const input = useRef<HTMLInputElement>(null);

  const take = (list: FileList | File[] | null) => {
    if (!list) return;
    const ok = [...list].filter((f) => ACCEPT.split(",").some((ext) => f.name.toLowerCase().endsWith(ext)));
    setFiles((prev) => [...prev, ...ok.filter((f) => !prev.some((p) => p.name === f.name && p.size === f.size))]);
  };
  const onDrop = (e: DragEvent) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files); };

  const start = () => {
    addFiles(files, { useAi: useAi && aiAvailable, quantity, client: client.trim() });
    setFiles([]);
    onAdded();
  };

  return (
    <section className={`card add ${big ? "add-big" : ""}`}>
      {big && (
        <>
          <h1>Chiffrer des pièces à partir de leurs plans</h1>
          <ol className="howto">
            <li><b>Déposez</b> un ou plusieurs plans</li>
            <li><b>Vérifiez</b> les quelques points signalés</li>
            <li><b>Téléchargez</b> le devis</li>
          </ol>
        </>
      )}
      {!big && <h2>Ajouter des plans</h2>}

      <div
        className={`drop ${over ? "over" : ""} ${files.length ? "filled" : ""}`}
        onClick={() => input.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
        role="button"
        tabIndex={0}
      >
        <input ref={input} type="file" accept={ACCEPT} multiple hidden onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
        <div className="drop-icon">⇪</div>
        <div className="drop-title">Glissez vos plans ici</div>
        <div className="muted">ou cliquez pour les choisir — PDF, scan ou image, plusieurs à la fois</div>
      </div>

      {files.length > 0 && (
        <ul className="file-list">
          {files.map((f, i) => (
            <li key={f.name + i}>
              <span className="file-name">{f.name}</span>
              <span className="muted small">{(f.size / 1024).toFixed(0)} Ko</span>
              <button className="btn link" onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label="Retirer">✕</button>
            </li>
          ))}
        </ul>
      )}

      <div className="row">
        <label className="fld">Quantité par pièce
          <input type="number" min={1} value={quantity} onChange={(e) => setQuantity(Math.min(100000, Math.max(1, +e.target.value || 1)))} />
        </label>
        <label className="fld grow">Client
          <input value={client} onChange={(e) => setClient(e.target.value)} placeholder="facultatif" />
        </label>
      </div>

      <label className={`check ${aiAvailable ? "" : "disabled"}`}>
        <input type="checkbox" checked={useAi && aiAvailable} disabled={!aiAvailable} onChange={(e) => setUseAi(e.target.checked)} />
        <span>
          Faire compléter par l'IA ce que le plan ne permet pas de lire
          {!aiAvailable && <span className="muted small"> — indisponible (clé API non configurée)</span>}
        </span>
      </label>

      <div className="actions">
        {onCancel && <button className="btn" onClick={onCancel}>Annuler</button>}
        <button className="btn primary big" disabled={!files.length} onClick={start}>
          {files.length > 1 ? `Analyser les ${files.length} plans` : "Analyser le plan"}
        </button>
      </div>
      {files.length > MAX_PARALLEL && (
        <p className="muted small center">
          {MAX_PARALLEL === 1 ? "Les plans sont analysés l'un après l'autre" : `${MAX_PARALLEL} plans sont analysés en même temps`} :
          vous pouvez vérifier les premiers pendant que les suivants sont traités.
        </p>
      )}

      {samples.length > 0 && (
        <div className="samples">
          <span className="muted small">Essayer avec un exemple :</span>
          {samples.map((s) => (
            <button key={s} className="btn chip" onClick={async () => take([await api.sampleFile(s)])}>{s}</button>
          ))}
          <button className="btn chip" onClick={async () => take(await Promise.all(samples.map(api.sampleFile)))}>tous</button>
        </div>
      )}
    </section>
  );
}
