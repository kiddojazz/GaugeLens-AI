import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { Card, ErrorBox } from "../components";
import { useApi } from "../hooks";

export default function UploadPage() {
  const navigate = useNavigate();
  const equipment = useApi(() => api.equipment(), []);
  const [equipmentId, setEquipmentId] = useState("auto");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const primary = (equipment.data ?? []).filter((e) => !e.linked_equipment_id);   // a clamp meter rides along with its inverter

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!files.length) return;
    setBusy(true);
    setError(null);
    try {
      const { clip_id } = await api.upload(files, equipmentId);
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
          <p>A 30–120 second phone video of the equipment's displays, or one or more photos. SAM 3.1 finds each display.</p>
        </div>
      </div>
      <div className="grid-2">
        <Card title="New clip">
          <form className="stack" onSubmit={submit}>
            <label className="field">
              Equipment
              <span className="muted small" style={{ fontWeight: 400 }}>GaugeLens reads the nameplate and displays to work out what it is; pick a registered unit only to skip that.</span>
              <select value={equipmentId} onChange={(e) => setEquipmentId(e.target.value)}>
                <option value="auto">Identify automatically (recommended)</option>
                {primary.map((e) => (
                  <option key={e.equipment_id} value={e.equipment_id}>{e.equipment_id} — {e.model ?? e.kind}</option>
                ))}
              </select>
            </label>
            <label className="field">
              Video or photos
              <span className="muted small" style={{ fontWeight: 400 }}>One video (MP4 or MOV, up to 100 MB), or up to 10 photos (JPEG, PNG or WebP). Photos of a screen that cycles pages are read as one clip, in order.</span>
              <input type="file" multiple accept="video/mp4,video/quicktime,.mp4,.mov,.m4v,.3gp,image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp"
                     onChange={(e) => setFiles(Array.from(e.target.files ?? []))} required />
            </label>
            {files.length > 1 && <span className="muted small">{files.length} photos selected</span>}
            <ErrorBox error={error ?? equipment.error} />
            <div className="row">
              <button className="btn primary" type="submit" disabled={busy || !files.length}>
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
