import { expect, expectImageLoaded, sample, test } from "./fixtures";

test("stat tiles match the API", async ({ page, request }) => {
  const data = await sample(request);
  const open = data.anomalies.filter((a) => a.decision !== "approved" && a.decision !== "rejected").length;
  await page.goto("/");
  const stats = page.locator(".grid-stats .stat");
  await expect(stats.nth(0).locator(".stat-value")).toHaveText(String(open));
  await expect(stats.nth(1).locator(".stat-value")).toHaveText(String(data.corrections.length));
  await expect(stats.nth(2).locator(".stat-value")).toHaveText(String(data.clips.length));
});

test("anomalies table lists every anomaly and opens one", async ({ page, request }) => {
  const { anomalies } = await sample(request);
  test.skip(!anomalies.length, "no anomalies in the database");
  await page.goto("/");
  const rows = page.locator("section.card", { has: page.getByRole("heading", { name: "Anomalies" }) }).locator("tbody tr");
  await expect(rows).toHaveCount(anomalies.length);
  for (const [i, a] of anomalies.entries()) {
    if (a.headline) await expect(rows.nth(i)).toContainText(a.headline.replace(/^SIMULATED( data)?:\s*/i, ""));
  }
  await rows.first().click();
  await expect(page).toHaveURL(new RegExp(`#/anomalies/${anomalies[0].anomaly_id}$`));
  await expect(page.locator(".page-head h1")).toBeVisible();
});

test("corrections show both crops and open the clip at that frame", async ({ page, request }) => {
  const { corrections } = await sample(request);
  test.skip(!corrections.length, "no corrections in the database");
  const key = (c: any) => [c.clip_id, c.role, c.original_text, c.fixed_text].join("|");
  const counts = new Map<string, number>();
  for (const c of corrections) counts.set(key(c), (counts.get(key(c)) ?? 0) + 1);
  await page.goto("/");
  const section = page.locator("section.card", { has: page.getByRole("heading", { name: "Recent corrections" }) });
  const rows = section.locator("tbody tr");
  await expect(rows, "repeats of the same correction are grouped").toHaveCount(Math.min(12, counts.size));
  const repeats = counts.get(key(corrections[0]))!;
  if (repeats > 1) await expect(rows.first()).toContainText(`${repeats} frames`);

  const first = corrections[0];
  await expect(rows.first()).toContainText(first.original_text);
  await expect(rows.first()).toContainText(first.fixed_text);
  if (first.original_crop) await expectImageLoaded(rows.first().getByAltText("first read"));
  if (first.fixed_crop) await expectImageLoaded(rows.first().getByAltText("re-look"));

  await rows.first().click();
  await expect(page).toHaveURL(new RegExp(`#/clips/${first.clip_id}\\?frame=${first.frame_id}$`));
  await expectImageLoaded(page.locator(".viewer img"));
});
