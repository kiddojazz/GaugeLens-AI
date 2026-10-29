import { test as base, expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

/** Every test fails if the page throws, or logs a console error other than an HTTP failure a test provoked on purpose. */
export const test = base.extend<{ browserErrors: string[] }>({
  browserErrors: [
    async ({ page }, use) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(`uncaught: ${e.message}`));
      page.on("console", (m) => {
        if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(`console: ${m.text()}`);
      });
      await use(errors);
      expect(errors, "browser errors").toEqual([]);
    },
    { auto: true },
  ],
});
export { expect };

export async function getJson<T = any>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`/api/v1${path}`);
  expect(response.ok(), `GET ${path}`).toBeTruthy();
  return response.json();
}

/** Real data the tests navigate through; each test skips itself if the database doesn't have what it needs. */
export async function sample(request: APIRequestContext) {
  const [anomalies, clips, corrections, equipment] = await Promise.all([
    getJson<any[]>(request, "/anomalies"),
    getJson<any[]>(request, "/clips"),
    getJson<any[]>(request, "/corrections"),
    getJson<any[]>(request, "/equipment"),
  ]);
  return {
    anomalies,
    clips,
    corrections,
    equipment,
    diagnosed: anomalies.find((a) => a.diagnosis_id),
    clipWithFrames: clips.find((c) => c.status === "done" && c.frames >= 3),
  };
}

export async function expectImageLoaded(img: Locator) {
  await expect(img).toBeVisible();
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
}

/** The page fits the viewport width (no sideways scrolling). */
export async function expectNoHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
}
