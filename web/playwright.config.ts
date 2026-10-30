import { defineConfig } from "@playwright/test";

// End-to-end tests against the real API and database. Playwright starts its own uvicorn on :8010 serving the
// built UI (web/dist), so run `npm run build` first (`npm run test:e2e` does both).
// Uses the installed Microsoft Edge, so no browser download is needed.
const PORT = 8010;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: 4,
  reporter: [["list"], ["html", { open: "never", outputFolder: "e2e-report" }]],
  outputDir: "e2e-results",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    storageState: "e2e/.auth/admin.json",         // signed in as the e2e admin (global-setup.ts); guest tests reset it
    channel: "msedge",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `.venv\\Scripts\\python.exe -m uvicorn backend.app.main:app --port ${PORT}`,
    cwd: "..",
    url: `http://127.0.0.1:${PORT}/api/v1/health`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
