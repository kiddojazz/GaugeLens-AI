// "Identified as": what GaugeLens worked out the clip shows, the printed text it rests on, and a way to correct it.
import { useState, type FormEvent } from "react";
import { api, type Identification } from "./api";
import { Badge, Card } from "./components";
import { Icon } from "./icons";

const KINDS: [string, string][] = [
  ["meter", "Utility meter (electricity, water, gas)"], ["inverter", "Inverter"], ["charge_controller", "Solar charge controller"],
  ["generator", "Generator"], ["clamp_meter", "Clamp meter"], ["multimeter", "Multimeter"],
  ["pressure_system", "Pressure system"], ["other", "Other"],
];
const CONFIDENCE_TONE = { high: "ok", medium: "info", low: "warn" } as const;

export function IdentificationCard({ identification, processing, editable = true }: { editable?: boolean; identification?: Identification | null; processing: boolean }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("");
  const [saved, setSaved] = useState<{ model: string; kind: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!identification) {
    return processing ? (
      <Card title="Equipment"><div className="row muted small"><span className="spinner" aria-hidden /> Identifying the equipment…</div></Card>
    ) : null;
  }
  const ident = identification;
  const printed = [ident.brand, ident.model].filter(Boolean).join(" ");

  function startEdit() {
    setName(saved?.model ?? (printed || ident.description));
    setKind(saved?.kind ?? ident.kind);
    setEditing(true);
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const updated = await api.correctEquipment(ident.equipment_id, { model: name.trim(), kind });
      setSaved({ model: updated.model ?? name, kind: updated.kind });
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Card title={<div className="row" style={{ gap: 8 }}><Icon name="equipment" /> <h2>Identified as</h2></div>}
          actions={<div className="row" style={{ gap: 6 }}>
            <Badge tone={CONFIDENCE_TONE[ident.confidence]}>{ident.confidence} confidence</Badge>
            {ident.created ? <Badge tone="info">New to GaugeLens</Badge> : <Badge>Recognised</Badge>}
          </div>}>
      {editing ? (
        <form className="stack" style={{ gap: 10 }} onSubmit={save}>
          <label className="field">Name<input value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} /></label>
          <label className="field">Type
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              {KINDS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
          </label>
          {error && <div className="error">{error}</div>}
          <div className="row"><button className="btn primary" type="submit">Save correction</button>
            <button className="btn" type="button" onClick={() => setEditing(false)}>Cancel</button></div>
          <span className="muted small">Applies to every later clip of this unit.</span>
        </form>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          <div>
            <div className="ident-name">{saved?.model ?? (printed || ident.description)}</div>
            <div className="muted small">{saved ? `${KINDS.find(([v]) => v === saved.kind)?.[1] ?? saved.kind} · corrected by you` : ident.description}</div>
          </div>
          {ident.displays.map((d, i) => (
            <div key={i} className="small">
              <strong>{d.label}</strong>
              <div className="row" style={{ gap: 6, marginTop: 4 }}>
                {d.values.map((v, j) => (
                  <span key={j} className="tag-plain">{v.register_code ? `${v.register_code} · ` : ""}{v.quantity}{v.unit ? ` (${v.unit})` : ""}</span>
                ))}
              </div>
            </div>
          ))}
          {ident.evidence.length > 0 && (
            <div className="small muted">From the printed text: {ident.evidence.slice(0, 5).map((e) => `“${e}”`).join(", ")}</div>
          )}
          {editable && <div><button className="btn small-btn" onClick={startEdit}>Correct this</button></div>}
        </div>
      )}
    </Card>
  );
}
