import { expect, test } from "@playwright/test";

// Plain Playwright test: unlike ./fixtures, it expects the browser error that a crashing page logs.
test("a page that crashes shows a message, and the rest of the app keeps working", async ({ page }) => {
  await page.route("**/api/v1/clips/clip_broken", (route) => route.fulfill({ json: { clip: null, job: null, counts: {} } }));
  await page.goto("/#/clips/clip_broken");
  await expect(page.getByRole("alert")).toContainText("This page couldn't be shown");
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  await page.locator("nav.nav").getByRole("link", { name: "Incidents" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Incidents");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("an anomaly with no stored evidence still opens", async ({ page, request }) => {
  const anomalies = await (await request.get("/api/v1/anomalies")).json();
  test.skip(!anomalies.length, "no anomaly");
  const a = anomalies[0];
  await page.route(`**/api/v1/anomalies/${a.anomaly_id}`, async (route) => {
    const real = await (await route.fetch()).json();
    await route.fulfill({ json: { ...real, evidence: null } });
  });
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`/#/anomalies/${a.anomaly_id}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByText("This page couldn't be shown")).toHaveCount(0);
  expect(errors).toEqual([]);
});
