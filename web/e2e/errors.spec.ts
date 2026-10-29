import { expect, test } from "./fixtures";

test("a failing API shows an error box instead of crashing", async ({ page }) => {
  await page.route("**/api/v1/anomalies", (route) => route.fulfill({ status: 500, json: { detail: "database unavailable" } }));
  await page.goto("/");
  const section = page.locator("section.card", { has: page.getByRole("heading", { name: "Anomalies" }) });
  await expect(section.locator(".error")).toHaveText("database unavailable");
  await expect(section.getByText(/^No anomalies\./), "an error must not also claim there's nothing").toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Recent corrections" })).toBeVisible();
});

test("an unreachable API is shown in the top bar", async ({ page }) => {
  await page.route("**/api/v1/health", (route) => route.abort("connectionrefused"));
  await page.goto("/");
  await expect(page.locator(".topbar")).toContainText("API unreachable");
});

test("empty data shows friendly empty states", async ({ page }) => {
  for (const path of ["anomalies", "corrections", "clips"]) {
    await page.route(`**/api/v1/${path}`, (route) => route.fulfill({ json: [] }));
  }
  await page.goto("/");
  await expect(page.getByText(/^No anomalies\./)).toBeVisible();
  await expect(page.getByText(/^No corrections yet\./)).toBeVisible();
  await page.locator("nav.nav").getByRole("link", { name: "Clips" }).click();
  await expect(page.getByText("No clips yet. Upload one to start.")).toBeVisible();
});
