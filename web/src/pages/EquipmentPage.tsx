import { api, type Display } from "../api";
import { Card, Empty, ErrorBox, Loading } from "../components";
import { roleLabel } from "../format";
import { useApi } from "../hooks";

function DisplayTable({ display }: { display: Display }) {
  return (
    <div style={{ marginTop: 8 }}>
      <h3 style={{ marginBottom: 6 }}>{display.label} <span className="muted small">· found by SAM 3.1 as “{display.sam_prompt}”</span></h3>
      <table>
        <thead><tr><th>Quantity</th><th>Role</th><th>Unit</th><th className="num">Display step</th></tr></thead>
        <tbody>
          {display.instruments.map((i) => (
            <tr key={i.instrument_id}>
              <td>{roleLabel(i.physics_role)}</td>
              <td className="mono muted small">{i.physics_role}</td>
              <td>{i.unit}</td>
              <td className="num">{i.resolution ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function EquipmentPage() {
  const { data, error, loading } = useApi(() => api.equipment(), []);
  const ids = new Set((data ?? []).map((e) => e.equipment_id));
  // a clamp meter exists to measure its inverter, so it's shown inside that unit's card
  const primary = (data ?? []).filter((e) => !e.linked_equipment_id || !ids.has(e.linked_equipment_id));
  const attachedTo = (id: string) => (data ?? []).filter((e) => e.linked_equipment_id === id);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Equipment</h1>
          <p>What each display shows, and the ratings the physics rules use.</p>
        </div>
      </div>
      <ErrorBox error={error} />
      {error ? null : loading && !data ? <Loading /> : !primary.length ? <Card><Empty>No equipment registered.</Empty></Card> : (
        <div className="stack">
          {primary.map((e) => (
            <Card key={e.equipment_id} title={<div><h2>{e.equipment_id}</h2><div className="muted small">{e.model ?? "—"}</div></div>}
                  actions={<div className="row" style={{ gap: 6 }}>
                  {e.profile?.source === "auto" && <span className="badge">{e.profile.corrected ? "Identified, corrected" : "Identified automatically"}</span>}
                  <span className="badge">{e.kind.replace(/_/g, " ")}</span></div>}>
              <dl className="kv" style={{ marginBottom: 14 }}>
                {e.nominal_battery_v != null && <><dt>Battery system</dt><dd className="mono">{e.nominal_battery_v} V</dd></>}
                {e.rated_va != null && <><dt>Rating</dt><dd className="mono">{e.rated_va} VA</dd></>}
                {e.idle_w != null && <><dt>Idle draw</dt><dd className="mono">{e.idle_w} W</dd></>}
                {e.controller_type && <><dt>Controller</dt><dd>{e.controller_type.toUpperCase()}</dd></>}
                {e.linked_equipment_id && <><dt>Measures</dt><dd>{e.linked_equipment_id}</dd></>}
                {e.profile?.limits && Object.keys(e.profile.limits).length > 0 && (
                  <><dt>Nameplate limits</dt><dd className="mono">{Object.entries(e.profile.limits).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(" · ")}</dd></>
                )}
              </dl>
              {e.displays.map((d) => <DisplayTable key={d.display_id} display={d} />)}
              {attachedTo(e.equipment_id).map((m) => (
                <div key={m.equipment_id} className="attached">
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <div><strong>With {m.equipment_id}</strong> <span className="muted small">· {m.model ?? m.kind.replace(/_/g, " ")}, a second instrument the physics rules cross-check against</span></div>
                    <span className="badge">{m.kind.replace(/_/g, " ")}</span>
                  </div>
                  {m.displays.map((d) => <DisplayTable key={d.display_id} display={d} />)}
                </div>
              ))}
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
