import { expect, expectNoHorizontalScroll, sample, test } from "./fixtures";

const PAGES = [
  { link: "Incidents", hash: "#/incidents", heading: "Incidents" },
  { link: "Clips", hash: "#/clips", heading: "Clips" },
  { link: "Upload", hash: "#/upload", heading: "Upload clip" },
  { link: "Equipment", hash: "#/equipment", heading: "Equipment" },
  { link: "Traces", hash: "#/traces", heading: "Traces" },
  { link: "Evaluations", hash: "#/evals", heading: "Evaluations" },
];

test("sidebar navigates to every page and marks it active", async ({ page }) => {
  await page.goto("/");
  const nav = page.locator("nav.nav");
  for (const p of PAGES) {
    await nav.getByRole("link", { name: p.link }).click();
    await expect(page).toHaveURL(new RegExp(`${p.hash.replace("/", "\\/")}$`));
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(p.heading);
    await expect(nav.getByRole("link", { name: p.link })).toHaveClass(/active/);
    await expect(nav.locator("a.active")).toHaveCount(1);
  }
});

test("browser back and forward move between pages", async ({ page }) => {
  await page.goto("/#/clips");
  await page.locator("nav.nav").getByRole("link", { name: "Equipment" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Equipment");
  await page.goBack();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Clips");
  await page.goForward();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Equipment");
});

test("top bar shows the model and a healthy database", async ({ page, request }) => {
  const health = await (await request.get("/api/v1/health")).json();
  await page.goto("/");
  await expect(page.locator(".topbar")).toContainText(`${health.model.model} · database ok`);
});

test("'Upload clip' buttons lead to the upload form", async ({ page }) => {
  for (const start of ["/#/incidents", "/#/clips"]) {
    await page.goto(start);
    await page.locator(".page-head").getByRole("link", { name: "Upload clip" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Upload clip");
  }
});

test.describe("phone width", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  for (const p of PAGES) {
    test(`${p.heading} fits without sideways scrolling`, async ({ page }) => {
      await page.goto(`/${p.hash}`);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(p.heading);
      await page.waitForLoadState("networkidle");
      await expectNoHorizontalScroll(page);
    });
  }

  test("clip and anomaly pages fit without sideways scrolling", async ({ page, request }) => {
    const { clipWithFrames, diagnosed } = await sample(request);
    const paths = [clipWithFrames && `/#/clips/${clipWithFrames.clip_id}`, diagnosed && `/#/anomalies/${diagnosed.anomaly_id}`].filter(Boolean);
    test.skip(!paths.length, "no clip or anomaly to open");
    for (const path of paths) {
      await page.goto(path);
      await expect(page.locator(".page-head h1")).toBeVisible();
      await page.waitForLoadState("networkidle");
      await expectNoHorizontalScroll(page);
    }
  });
});
