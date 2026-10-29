import { request, type FullConfig } from "@playwright/test";

// Fail fast with a clear message instead of thirty confusing page failures.
export default async function globalSetup(config: FullConfig) {
  const baseURL = config.projects[0].use.baseURL!;
  const ctx = await request.newContext({ baseURL });
  const health = await (await ctx.get("/api/v1/health")).json();
  if (health.db !== "ok") {
    throw new Error(
      `API on ${baseURL} has no database (health: ${JSON.stringify(health)}). Is the gaugelens-db container up? ` +
        "If this terminal has a stale DATABASE_URL (e.g. injected by VS Code), run `Remove-Item Env:DATABASE_URL` or open a fresh terminal.",
    );
  }
  const index = await ctx.get("/");
  if (!index.ok() || !(await index.text()).includes('id="root"')) {
    throw new Error("The built UI isn't being served. Run `npm run build` first.");
  }
  await ctx.dispose();
}
