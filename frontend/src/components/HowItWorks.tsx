/** Page d'accueil : comment le logiciel lit un plan, et ce que l'utilisateur a à faire. */
export function HowItWorks({ onStart }: { onStart?: () => void }) {
  return (
    <section className="card guide">
      <h2>Comment ça marche</h2>
      <p className="muted">
        Le logiciel lit le plan, en déduit les informations utiles au chiffrage, puis vous montre ce qu'il a trouvé
        et <b>où il l'a lu</b>. Règle d'or : <b>mieux vaut une case vide qu'une valeur fausse</b>. Dans le doute,
        il ne devine pas, il vous le signale.
      </p>

      <h3>Ce que fait le logiciel</h3>
      <ol className="pipeline">
        <li>
          <b>Lecture du texte du plan</b>
          <span>PDF vectoriel : le texte est lu directement. Scan ou image : l'OCR local repère chaque texte,
            puis Mistral OCR le lit (OCR local seul sans clé Mistral).</span>
        </li>
        <li>
          <b>Règles métier</b>
          <span>Les textes lus sont interprétés : matière, épaisseur, Ø de trous, plis, cotes, traitement, quantités
            de la nomenclature…</span>
        </li>
        <li>
          <b>L'IA désigne, elle n'invente pas</b>
          <span>L'IA voit le plan avec ses textes numérotés et indique <i>lesquels</i> portent chaque information. Elle
            relie aussi les vues d'une même pièce (dessus, face, côté) pour les dimensions hors tout. La valeur vient
            toujours d'un texte réellement lu.</span>
        </li>
        <li>
          <b>Contrôles de cohérence</b>
          <span>Épaisseur plausible, désaccord entre lecture et IA, vues contradictoires… Tout doute laisse la case
            vide ou la marque « à vérifier ».</span>
        </li>
        <li>
          <b>Chiffrage et devis</b>
          <span>Le prix est calculé à partir des valeurs (paramètres atelier modifiables) et se met à jour à chaque
            correction. Le devis PDF se télécharge en un clic.</span>
        </li>
      </ol>

      <h3>Ce que vous faites</h3>
      <div className="guide-grid">
        <div>
          <b>1. Déposer</b>
          <p>Un ou plusieurs plans (PDF, scan, photo). Ils sont analysés l'un après l'autre et apparaissent à gauche
            avec leur avancement.</p>
        </div>
        <div>
          <b>2. Vérifier</b>
          <p>Les informations <span className="tag warn">à vérifier</span> sont en tête. Cliquez une information :
            le plan zoome sur l'endroit où elle a été lue.</p>
          <ul>
            <li><b>✓ C'est correct</b> : la valeur est juste.</li>
            <li><b>Modifier</b> : la corriger, ou la vider si l'information n'existe pas.</li>
            <li><b>Choisir sur le plan</b> : cliquer le texte qui donne la valeur, sans rien taper.</li>
            <li><b>Pourquoi ?</b> : le détail de chaque étape (lecture, règle, IA, contrôles) qui a mené à la valeur.</li>
          </ul>
        </div>
        <div>
          <b>3. Télécharger le devis</b>
          <p>Quand plus rien n'est à vérifier, le plan passe « prêt » et le devis PDF est disponible.</p>
        </div>
      </div>

      <h3>Sur le plan</h3>
      <ul className="guide-legend">
        <li><i className="sw ok" /> information lue · <i className="sw flag" /> à vérifier</li>
        <li><b>Molette</b> : zoomer à l'endroit de la souris · <b>glisser</b> : se déplacer · <b>double-clic</b> : zoom ×2 ·
          clic sur le pourcentage : pièce entière</li>
        <li><b>Tout le texte lu</b> : affiche chaque texte reconnu, pour voir ce que l'OCR a lu ou manqué.</li>
      </ul>

      <p className="muted small guide-learn">
        Vos validations et corrections sont enregistrées avec la proposition d'origine du logiciel
        dans la base de données. Elles servent à mesurer ses erreurs et à l'améliorer.
      </p>
      {onStart && <div className="actions"><button className="btn primary" onClick={onStart}>Ajouter un plan</button></div>}
    </section>
  );
}
