// Line icons (24px grid, stroke = currentColor). One set for navigation and for agent trace steps.
import type { Span } from "./api";

const paths = {
  incidents: <path d="M12 3 2 20h20L12 3Zm0 6v5m0 3v.01" />,
  clips: <path d="M4 5h16v14H4zM10 9l5 3-5 3V9Z" />,
  upload: <path d="M12 16V4m0 0-4 4m4-4 4 4M4 16v3h16v-3" />,
  equipment: <path d="M4 7h16v10H4zM8 7V4h8v3M8 12h3m3 0h2" />,
  traces: <path d="M6 4v6m0 0a2 2 0 1 0 0 4m0-4a2 2 0 1 1 0 4m0 0v6M6 12h6m0 0a2 2 0 1 0 4 0 2 2 0 0 0-4 0Zm4 0h4" />,
  evals: <path d="M9 4h6v3H9zM7 5H5v16h14V5h-2M8.5 13.5l2 2 5-5" />,
  agent: <path d="M8 9h8a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-4a3 3 0 0 1 3-3Zm4-5v5M9.5 14h.01M14.5 14h.01M2 13v2m20-2v2" />,
  context: <path d="M12 3 3 8l9 5 9-5-9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5" />,
  llm: <path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3Zm6 11 .9 2.1L21 17l-2.1.9L18 20l-.9-2.1L15 17l2.1-.9L18 14Z" />,
  tool: <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4 2.5-2.5Z" />,
  readings: <path d="M4 14a8 8 0 1 1 16 0M12 14l4-5M4 18h16" />,
  physics: <path d="M12 4v16M5 20h14M6 8h12M6 8l-3 6h6L6 8Zm12 0-3 6h6l-3-6Z" />,
  relook: <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z" />,
  frame: <path d="M4 5h16v14H4zM4 15l4-4 4 4 3-3 5 5M15.5 9h.01" />,
  manual: <path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2V5Zm0 14a2 2 0 0 1 2-2h13M8 7h7" />,
  guardrail: <path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6l-8-3Zm-3.5 9 2.5 2.5 4.5-4.5" />,
  fallback: <path d="M12 3 2 20h20L12 3Zm0 6v5m0 3v.01" />,
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  cross: <path d="M6 6l12 12M18 6 6 18" />,
  play: <path d="M7 4.5v15l12-7.5-12-7.5Z" />,
  arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
  chevron: <path d="M9 6l6 6-6 6" />,
  chat: <path d="M4 5h16v11H9l-5 4V5Zm4 5h8m-8 3h5" />,
  send: <path d="M4 12 20 4l-5 16-3-7-8-1Z" />,
};
export type IconName = keyof typeof paths;

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {paths[name]}
    </svg>
  );
}

const TOOL_ICONS: Record<string, IconName> = {
  get_readings: "readings", get_physics_checks: "physics", get_relook_attempts: "relook", view_frame: "frame",
  search_manual: "manual",
};

/** The icon for a trace step: tools by name, everything else by kind. */
export function spanIcon(span: Pick<Span, "kind" | "name">): IconName {
  if (span.kind === "tool") return TOOL_ICONS[span.name] ?? "tool";
  return ({ agent: "agent", context: "context", llm: "llm", guardrail: "guardrail", fallback: "fallback" } as const)[span.kind] ?? "tool";
}

export const KIND_LABEL: Record<string, string> = {
  agent: "Agent", context: "Context", llm: "Model call", tool: "Tool", guardrail: "Guardrail", fallback: "Fallback",
};
