import { useEffect, useState } from "react";
import { api } from "./api";
import { AddPlans } from "./components/AddPlans";
import { HowItWorks } from "./components/HowItWorks";
import { JobList } from "./components/JobList";
import { JobView } from "./components/JobView";
import { Summary } from "./components/Summary";
import { JobsProvider, useJobs } from "./jobs";
import type { Pricing } from "./types";

export default function App() {
  const [pricing, setPricing] = useState<Pricing | null>(null);
  const [ai, setAi] = useState(false);
  const [samples, setSamples] = useState<string[]>([]);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    api.health().then((h) => setAi(h.vlm_available)).catch(() => setOffline(true));
    api.pricing().then(setPricing).catch(() => setOffline(true));
    api.samples().then(setSamples);
  }, []);

  return (
    <JobsProvider pricing={pricing} aiAvailable={ai} refreshPricing={() => api.pricing().then(setPricing)}>
      {offline && <div className="banner bad">Serveur injoignable : lancez l'API (uvicorn api.main:app --port 8000).</div>}
      <Shell samples={samples} />
    </JobsProvider>
  );
}

/** Une seule barre, à gauche : ajouter un plan, suivre les plans en cours, en ouvrir un. */
function Shell({ samples }: { samples: string[] }) {
  const { jobs, selected, select } = useJobs();
  const [adding, setAdding] = useState(false);
  const [help, setHelp] = useState(false);
  const empty = jobs.length === 0;
  const showAdd = empty || adding || !selected;

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="side-top">
          <div className="brand">DevisMaker</div>
          <button className={`btn primary block ${showAdd ? "current" : ""}`} onClick={() => { setAdding(true); setHelp(false); select(null); }}>
            + Nouveau plan
          </button>
          {!empty && <Summary />}
        </div>
        {empty ? <p className="muted small side-empty">Aucun plan pour l'instant. Les plans déposés apparaîtront ici avec leur avancement.</p>
          : <JobList onPick={() => { setAdding(false); setHelp(false); }} />}
        <div className="side-bottom">
          <button className={`btn link small ${help ? "current" : ""}`} onClick={() => setHelp(!help)}>? Comment ça marche</button>
        </div>
      </aside>
      <main className="main">
        {help ? (
          <div className="add-wrap"><HowItWorks onStart={() => { setHelp(false); setAdding(true); select(null); }} /></div>
        ) : showAdd ? (
          <div className="add-wrap">
            <AddPlans samples={samples} big={empty} onAdded={() => setAdding(false)}
              onCancel={selected ? () => setAdding(false) : undefined} />
            {empty && <HowItWorks />}
          </div>
        ) : (
          <JobView key={selected.id} job={selected} />
        )}
      </main>
    </div>
  );
}
