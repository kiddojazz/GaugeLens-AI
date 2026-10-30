import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { request, type FullConfig } from "@playwright/test";

export const ADMIN_EMAIL = "e2e-admin@gaugelens.test";
const HERE = dirname(fileURLToPath(import.meta.url));
export const ADMIN_STATE = join(HERE, ".auth", "admin.json");

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

  // Most tests browse the real data as an admin (who sees every clip); guest tests clear this state themselves.
  // A fresh password every run, for a test-only account in the local database.
  const password = randomBytes(18).toString("base64url");
  const root = join(HERE, "..", "..");
  execFileSync(join(root, ".venv", "Scripts", "python.exe"),
    ["-m", "backend.app.cli", "user", ADMIN_EMAIL, "--admin", "--reset-password", "--password-env", "E2E_ADMIN_PASSWORD"],
    { cwd: root, env: { ...process.env, E2E_ADMIN_PASSWORD: password }, stdio: "pipe" });
  const login = await ctx.post("/api/v1/auth/login", { data: { email: ADMIN_EMAIL, password } });
  if (!login.ok()) throw new Error(`e2e admin sign-in failed: ${login.status()} ${await login.text()}`);
  mkdirSync(dirname(ADMIN_STATE), { recursive: true });
  await ctx.storageState({ path: ADMIN_STATE });
  await ctx.dispose();
}
