import { expect, sample, test } from "./fixtures";

// The switch and the read-only view, with the responses shaped by the test: real data is never published here.
test("an admin can show one of their clips to visitors", async ({ page, request }) => {
  const clip = (await sample(request)).clipWithFrames;
  test.skip(!clip, "no clip");
  let isPublic = false;
  let sent: unknown = null;
  await page.route(`**/api/v1/clips/${clip.clip_id}`, async (route) => {
    const real = await (await route.fetch()).json();
    await route.fulfill({ json: { ...real, can_publish: true, clip: { ...real.clip, is_public: isPublic } } });
  });
  await page.route(`**/api/v1/clips/${clip.clip_id}/public`, async (route) => {
    sent = route.request().postDataJSON();
    isPublic = true;
    await route.fulfill({ json: { clip_id: clip.clip_id, is_public: true } });
  });
  await page.goto(`/#/clips/${clip.clip_id}`);
  await page.getByRole("button", { name: "Show to visitors" }).click();
  await expect(page.getByRole("button", { name: "Stop showing to visitors" })).toBeVisible();
  await expect(page.locator(".page-head .badge.example")).toHaveText("Example");
  expect(sent).toEqual({ public: true });
});

test("a visitor sees an example as read-only", async ({ page, request }) => {
  const { clipWithFrames: clip, diagnosed } = await sample(request);
  test.skip(!clip || !diagnosed, "needs a clip and a pending diagnosis");
  await page.route(`**/api/v1/clips/${clip.clip_id}`, async (route) => {
    const real = await (await route.fetch()).json();
    await route.fulfill({ json: { ...real, can_edit: false, can_publish: false, clip: { ...real.clip, is_public: true } } });
  });
  await page.goto(`/#/clips/${clip.clip_id}`);
  await expect(page.getByText("This is an example clip.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Show to visitors" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Correct this" })).toHaveCount(0);

  await page.route(`**/api/v1/anomalies/${diagnosed.anomaly_id}`, async (route) => {
    const real = await (await route.fetch()).json();
    await route.fulfill({ json: { ...real, can_edit: false } });
  });
  await page.route(`**/api/v1/anomalies/${diagnosed.anomaly_id}/diagnosis`, async (route) => {
    const real = await (await route.fetch()).json();
    await route.fulfill({ json: { ...real, decision: "pending" } });
  });
  await page.goto(`/#/anomalies/${diagnosed.anomaly_id}`);
  await expect(page.getByText("its owner decides whether to approve")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
});
