import { expect, expectImageLoaded, getJson, sample, test } from "./fixtures";

test("clips table lists every clip and opens one", async ({ page, request }) => {
  const { clips } = await sample(request);
  test.skip(!clips.length, "no clips in the database");
  await page.goto("/#/clips");
  const rows = page.locator("tbody tr");
  await expect(rows).toHaveCount(clips.length);
  await expect(rows.first()).toContainText(clips[0].equipment_id);
  await rows.first().click();
  await expect(page).toHaveURL(new RegExp(`#/clips/${clips[0].clip_id}`));
  await expect(page.locator(".page-head h1")).toContainText(clips[0].equipment_id);
});

test.describe("clip page", () => {
  let clip: any;
  let frames: any;
  let readings: any[];

  test.beforeEach(async ({ request }) => {
    clip = (await sample(request)).clipWithFrames;
    test.skip(!clip, "no finished clip with at least 3 frames");
    frames = await getJson(request, `/clips/${clip.clip_id}/frames`);
    readings = await getJson(request, `/readings?clip_id=${clip.clip_id}&limit=5000`);
  });

  test("shows the frame with SAM's display boxes", async ({ page }) => {
    await page.goto(`/#/clips/${clip.clip_id}`);
    await expectImageLoaded(page.locator(".viewer img"));
    const first = frames.frames[0];
    await expect(page.locator(".viewer svg g")).toHaveCount(frames.frame_size ? first.boxes.length : 0);
    for (const box of first.boxes) await expect(page.locator(".viewer svg")).toContainText(box.label);
    await expect(page.locator(".card-head", { hasText: "Footage" })).toContainText(`frame 1 / ${frames.frames.length}`);
  });

  test("prev/next, slider and keyboard move between frames", async ({ page }) => {
    const n = frames.frames.length;
    await page.goto(`/#/clips/${clip.clip_id}`);
    const header = page.locator(".card-head", { hasText: "Footage" });
    const prev = page.getByRole("button", { name: "Previous frame" });
    const next = page.getByRole("button", { name: "Next frame" });

    await expect(prev).toBeDisabled();
    await next.click();
    await expect(header).toContainText(`frame 2 / ${n}`);
    await expect(page).toHaveURL(new RegExp(`frame=${frames.frames[1].frame_id}$`));
    await prev.click();
    await expect(header).toContainText(`frame 1 / ${n}`);

    const slider = page.getByRole("slider", { name: "Frame" });
    await slider.focus();
    await page.keyboard.press("End");
    await expect(header).toContainText(`frame ${n} / ${n}`);
    await expect(next).toBeDisabled();
    await expectImageLoaded(page.locator(".viewer img"));
  });

  test("readings panel matches the selected frame", async ({ page }) => {
    const target = frames.frames.find((f: any) => readings.some((r) => r.frame_id === f.frame_id)) ?? frames.frames[0];
    const expected = readings.filter((r) => r.frame_id === target.frame_id);
    await page.goto(`/#/clips/${clip.clip_id}?frame=${target.frame_id}`);
    const panel = page.locator("section.card", { has: page.getByRole("heading", { name: "Readings on this frame" }) });
    if (!expected.length) {
      await expect(panel).toContainText("No readings on this frame.");
    } else {
      await expect(panel.locator("tbody tr")).toHaveCount(expected.length);
      for (const r of expected.filter((r) => r.raw_text)) await expect(panel).toContainText(r.raw_text);
    }
  });

  test("deep link ?frame= selects that frame", async ({ page }) => {
    const i = Math.min(2, frames.frames.length - 1);
    await page.goto(`/#/clips/${clip.clip_id}?frame=${frames.frames[i].frame_id}`);
    await expect(page.locator(".card-head", { hasText: "Footage" })).toContainText(`frame ${i + 1} / ${frames.frames.length}`);
  });

  test("trend and physics panels render", async ({ page }) => {
    await page.goto(`/#/clips/${clip.clip_id}`);
    await expect(page.getByRole("heading", { name: "Trends" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Physics on this frame" })).toBeVisible();
    const accepted = readings.filter((r) => r.value != null && !r.reused_from && !["rejected", "needs_review"].includes(r.status));
    if (accepted.length) await expect(page.locator("section.card", { has: page.getByRole("heading", { name: "Trends" }) }).locator("tbody tr").first()).toBeVisible();
  });
});

test("a clip's anomaly links to its diagnosis page", async ({ page, request }) => {
  const { anomalies } = await sample(request);
  const a = anomalies.find((x) => x.clip_id);
  test.skip(!a, "no anomaly tied to a clip");
  await page.goto(`/#/clips/${a.clip_id}`);
  await page.locator("section.card", { has: page.getByRole("heading", { name: "Anomalies" }) }).getByRole("link").first().click();
  await expect(page).toHaveURL(/#\/anomalies\//);
});

test("an unknown clip shows an error, not a blank page", async ({ page }) => {
  await page.goto("/#/clips/clip_does_not_exist");
  await expect(page.locator(".error")).toBeVisible();
});
