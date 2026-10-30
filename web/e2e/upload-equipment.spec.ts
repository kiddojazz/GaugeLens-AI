import { expect, sample, test } from "./fixtures";

const VIDEO = { name: "test.mp4", mimeType: "video/mp4", buffer: Buffer.from("not really a video") };

test("equipment defaults to automatic identification and lists only primary units", async ({ page, request }) => {
  const { equipment } = await sample(request);
  const primary = equipment.filter((e) => !e.linked_equipment_id);
  await page.goto("/#/upload");
  await expect(page.getByLabel("Equipment")).toHaveValue("auto");
  const options = page.getByLabel("Equipment").locator("option:not([value='auto'])");
  await expect(options).toHaveCount(primary.length);
  for (const e of equipment.filter((e) => e.linked_equipment_id)) await expect(options.filter({ hasText: e.equipment_id })).toHaveCount(0);
});

test("only a video is needed: the equipment is identified when it is not chosen", async ({ page }) => {
  let body = "";
  await page.route("**/api/v1/clips", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    body = route.request().postDataBuffer()?.toString("latin1") ?? "";
    return route.fulfill({ status: 201, json: { clip_id: "clip_test", job_id: "job_test" } });
  });
  await page.route("**/api/v1/clips/clip_test**", (route) => route.fulfill({ status: 404, json: { detail: "test" } }));
  await page.goto("/#/upload");
  const submit = page.getByRole("button", { name: "Upload and process" });
  await expect(submit).toBeDisabled();
  await page.getByLabel(/Video/).setInputFiles(VIDEO);
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page).toHaveURL(/#\/clips\/clip_test$/);
  expect(body).toMatch(/name="equipment_id"\s+auto/);
});

test("several photos can be uploaded as one clip", async ({ page }) => {
  let body = "";
  await page.route("**/api/v1/clips", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    body = route.request().postDataBuffer()?.toString("latin1") ?? "";
    return route.fulfill({ status: 201, json: { clip_id: "clip_test", job_id: "job_test" } });
  });
  await page.route("**/api/v1/clips/clip_test**", (route) => route.fulfill({ status: 404, json: { detail: "test" } }));
  await page.goto("/#/upload");
  const photo = (name: string) => ({ name, mimeType: "image/jpeg", buffer: Buffer.from("jpeg bytes") });
  await page.getByLabel(/Video or photos/).setInputFiles([photo("page1.jpg"), photo("page2.jpg")]);
  await expect(page.getByText("2 photos selected")).toBeVisible();
  await page.getByRole("button", { name: "Upload and process" }).click();
  await expect(page).toHaveURL(/#\/clips\/clip_test$/);
  expect(body).toContain('filename="page1.jpg"');
  expect(body).toContain('filename="page2.jpg"');
});

test("a successful upload opens the new clip", async ({ page, request }) => {
  const data = await sample(request);
  const primary = data.equipment.find((e) => !e.linked_equipment_id);
  test.skip(!primary || !data.clips.length, "needs equipment and a clip to land on");
  let body = "";
  await page.route("**/api/v1/clips", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    body = route.request().postDataBuffer()?.toString("latin1") ?? "";
    return route.fulfill({ json: { clip_id: data.clips[0].clip_id, job_id: "job_test" } });
  });
  await page.goto("/#/upload");
  await page.getByLabel("Equipment").selectOption(primary.equipment_id);
  await page.getByLabel(/Video/).setInputFiles(VIDEO);
  await page.getByRole("button", { name: "Upload and process" }).click();

  await expect(page).toHaveURL(new RegExp(`#/clips/${data.clips[0].clip_id}$`));
  expect(body).toContain('name="equipment_id"');
  expect(body).toContain(primary.equipment_id);
  expect(body).toContain('filename="test.mp4"');
});

test("an upload error is shown and the form stays usable", async ({ page, request }) => {
  const primary = (await sample(request)).equipment.find((e) => !e.linked_equipment_id);
  test.skip(!primary, "no equipment registered");
  await page.route("**/api/v1/clips", (route) =>
    route.request().method() === "POST" ? route.fulfill({ status: 413, json: { detail: "File too large" } }) : route.fallback(),
  );
  await page.goto("/#/upload");
  await page.getByLabel("Equipment").selectOption(primary.equipment_id);
  await page.getByLabel(/Video/).setInputFiles(VIDEO);
  await page.getByRole("button", { name: "Upload and process" }).click();
  await expect(page.locator(".error")).toHaveText("File too large");
  await expect(page.getByRole("button", { name: "Upload and process" })).toBeEnabled();
  await expect(page).toHaveURL(/#\/upload$/);
});

test("equipment page shows every unit, with linked meters inside their unit's card", async ({ page, request }) => {
  const { equipment } = await sample(request);
  test.skip(!equipment.length, "no equipment registered");
  const ids = new Set(equipment.map((e) => e.equipment_id));
  const instruments = (e: any) => e.displays.reduce((n: number, d: any) => n + d.instruments.length, 0);
  await page.goto("/#/equipment");
  for (const e of equipment.filter((e) => !e.linked_equipment_id || !ids.has(e.linked_equipment_id))) {
    const card = page.locator("section.card", { has: page.getByRole("heading", { name: e.equipment_id, exact: true }) });
    await expect(card).toBeVisible();
    const linked = equipment.filter((m) => m.linked_equipment_id === e.equipment_id);
    for (const m of linked) await expect(card).toContainText(`With ${m.equipment_id}`);
    const total = instruments(e) + linked.reduce((n, m) => n + instruments(m), 0);
    await expect(card.locator("tbody tr")).toHaveCount(total);
  }
});

test("upload page explains how to film", async ({ page }) => {
  await page.goto("/#/upload");
  const tips = page.locator("section.card", { has: page.getByRole("heading", { name: "How to film" }) });
  await expect(tips.locator("li")).toHaveCount(5);
});
