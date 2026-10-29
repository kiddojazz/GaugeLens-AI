import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, ErrorBox } from "../components";
import { useApi } from "../hooks";

export default function UploadPage() {
  const navigate = useNavigate();
  const equipment = useApi(() => api.equipment(), []);
  const [equipmentId, setEquipmentId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const primary = (equipment.data ?? []).filter((e) => !e.linked_equipment_id);   // a clamp meter rides along with its inverter

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!file || !equipmentId) return;
    setBusy(true);
    setError(null);
    try {
      const { clip_id } = await api.upload(file, equipmentId);
      navigate(`/clips/${clip_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Upload clip</h1>
          <p>A 30–120 second phone video of the equipment's displays. Hold the phone steady; SAM 3.1 finds and tracks each display.</p>
        </div>
      </div>
      <div className="grid-2">
        <Card title="New clip">
          <form className="stack" onSubmit={submit}>
            <label className="field">
              Equipment
              <select value={equipmentId} onChange={(e) => setEquipmentId(e.target.value)} required>
                <option value="">Select equipment…</option>
                {primary.map((e) => (
                  <option key={e.equipment_id} value={e.equipment_id}>{e.equipment_id} — {e.model ?? e.kind}</option>
                ))}
              </select>
            </label>
            <label className="field">
              Video (MP4 or MOV, up to 200 MB)
              <input type="file" accept="video/mp4,video/quicktime,.mp4,.mov,.m4v,.3gp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} required />
            </label>
            <ErrorBox error={error ?? equipment.error} />
            <div className="row">
              <button className="btn primary" type="submit" disabled={busy || !file || !equipmentId}>
                {busy ? "Uploading…" : "Upload and process"}
              </button>
              <span className="muted small">Processing starts immediately; you'll see progress live.</span>
            </div>
          </form>
        </Card>
        <Card title="How to film">
          <ol className="tips">
            <li><strong>Get every display in frame.</strong> Include the inverter screen and, if you have one, the clamp meter on the battery cable.</li>
            <li><strong>Hold steady for 30–60 seconds.</strong> Lean on something or use a stand; SAM tracks small movements.</li>
            <li><strong>Let the screen cycle.</strong> Many inverters show one page at a time; wait for each page to appear.</li>
            <li><strong>Change the load once.</strong> Switch a kettle or iron on partway through; physics checks are strongest across a change.</li>
            <li><strong>Avoid glare.</strong> Tilt the phone slightly if a light reflects on the screen.</li>
          </ol>
          <p className="muted small" style={{ marginBottom: 0 }}>Stay outside enclosures. Film only what's visible from the front panel.</p>
        </Card>
      </div>
    </>
  );
}
