// Small display helpers shared by pages.

export const shortId = (id: string | null | undefined) => (id ? id.replace(/^[a-z]+_/, "").slice(-8) : "—");

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

const ROLE_LABELS: Record<string, string> = {
  battery_v: "Battery voltage",
  battery_i: "Battery current",
  battery_soc_pct: "Battery charge",
  output_w: "Output power",
  output_va: "Apparent power",
  load_pct: "Load",
  ac_v: "AC voltage",
  ac_hz: "AC frequency",
  energy_kwh: "Energy",
  voltage_v: "Voltage",
  current_a: "Current",
  power_kw: "Power",
  frequency_hz: "Frequency",
};

export const roleLabel = (role: string | null | undefined) => (role ? ROLE_LABELS[role] ?? role.replace(/_/g, " ") : "—");

/** "inverter.dc_power_balance" -> "DC power balance" */
export function ruleLabel(ruleId: string | null | undefined): string {
  if (!ruleId) return "—";
  const name = ruleId.split(".").slice(1).join(".") || ruleId;
  return name
    .replace(/_/g, " ")
    .replace(/\b(dc|ac|va|pv|pf|soc)\b/gi, (m) => m.toUpperCase())
    .replace(/^./, (c) => c.toUpperCase());
}

export const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)} %`);

export const num = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : Number.isInteger(v) ? String(v) : v.toFixed(digits);
