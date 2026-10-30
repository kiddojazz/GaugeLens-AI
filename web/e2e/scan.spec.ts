import { expect, sample, test } from "./fixtures";

// Scan with a synthetic camera: getUserMedia returns an animated canvas stream (the browser's built-in fake
// device ends its stream after about a second on this page in headless runs). Our recorder, review and upload
// code run unchanged; the upload is intercepted.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const canvas = document.createElement("canvas");
      canvas.width = 640;
      canvas.height = 480;
      const g = canvas.getContext("2d")!;
      let n = 0;
      setInterval(() => {
        g.fillStyle = `hsl(${(n += 7) % 360}, 60%, 45%)`;
        g.fillRect(0, 0, 640, 480);
        g.fillStyle = "#cfe8c8";
        g.font = "64px monospace";
        g.fillText(`${(24 + (n % 10) / 10).toFixed(1)} V`, 200, 260);
      }, 40);
      return canvas.captureStream(25);
    };
  });
});

test("record in the page, review, and send: the clip goes up for automatic identification", async ({ page }) => {
  let body = "";
  await page.route("**/api/v1/clips", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    body = route.request().postDataBuffer()?.toString("latin1") ?? "";
    return route.fulfill({ status: 201, json: { clip_id: "clip_scan_test", job_id: "job_test" } });
  });
  await page.route("**/api/v1/clips/clip_scan_test**", (route) => route.fulfill({ status: 404, json: { detail: "test" } }));

  await page.goto("/#/scan");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Scan");
  await page.getByRole("button", { name: "Start camera" }).click();
  const camera = page.getByLabel("Camera");
  await expect.poll(() => camera.evaluate((v: HTMLVideoElement) => v.videoWidth)).toBeGreaterThan(0);
  await expect(page.locator(".scan-guide")).toBeVisible();

  await page.getByRole("button", { name: "Start recording" }).click();
  await expect(page.locator(".scan-timer")).toContainText("keep going");
  await page.waitForTimeout(1500);
  await page.getByRole("button", { name: "Stop recording" }).click();

  const review = page.getByLabel("Your recording");
  await expect(review).toBeVisible();
  await expect.poll(() => review.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThan(0);
  await page.getByLabel(/simulated test/).check();
  await page.getByRole("button", { name: "Use this clip" }).click();

  await expect(page).toHaveURL(/#\/clips\/clip_scan_test$/);
  expect(body).toMatch(/filename="scan-[\d-]+\.(webm|mp4)"/);
  expect(body).toMatch(/name="equipment_id"\s+auto/);
  expect(body).toMatch(/name="simulated"\s+true/);
});

test("photos from the live camera go up together as one clip", async ({ page }) => {
  let body = "";
  await page.route("**/api/v1/clips", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    body = route.request().postDataBuffer()?.toString("latin1") ?? "";
    return route.fulfill({ status: 201, json: { clip_id: "clip_photo_test", job_id: "job_test" } });
  });
  await page.route("**/api/v1/clips/clip_photo_test**", (route) => route.fulfill({ status: 404, json: { detail: "test" } }));

  await page.goto("/#/scan");
  await page.getByRole("button", { name: "Start camera" }).click();
  const camera = page.getByLabel("Camera");
  await expect.poll(() => camera.evaluate((v: HTMLVideoElement) => v.videoWidth)).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Take a photo" }).click();
  await expect(page.getByRole("button", { name: "Use this photo" })).toBeVisible();
  await page.getByRole("button", { name: "Take a photo" }).click();          // "Another"
  await expect(page.getByLabel("Photos taken").locator("img")).toHaveCount(2);
  await page.getByRole("button", { name: "Use these 2 photos" }).click();

  await expect(page).toHaveURL(/#\/clips\/clip_photo_test$/);
  expect(body.match(/filename="photo-[\d-]+\.jpg"/g)).toHaveLength(2);
  expect(body).toContain("Content-Type: image/jpeg");
});

test("retake throws the recording away and reopens the camera", async ({ page }) => {
  await page.goto("/#/scan");
  await page.getByRole("button", { name: "Start camera" }).click();
  await page.getByRole("button", { name: "Start recording" }).click();
  await page.waitForTimeout(700);
  await page.getByRole("button", { name: "Stop recording" }).click();
  await page.getByRole("button", { name: "Retake" }).click();
  await expect(page.getByRole("button", { name: "Start recording" })).toBeVisible();
  await expect(page.getByLabel("Your recording")).toHaveCount(0);
});

test("the phone's camera app is offered as well", async ({ page }) => {
  await page.goto("/#/scan");
  const input = page.getByLabel("Record with the camera app");
  await expect(input).toHaveAttribute("capture", "environment");
  await expect(input).toHaveAttribute("accept", "video/*");
});

test("Scan is in the sidebar and on the Incidents page", async ({ page }) => {
  await page.goto("/#/incidents");
  await page.locator(".page-head").getByRole("link", { name: "Scan now" }).click();
  await expect(page).toHaveURL(/#\/scan$/);
  await page.locator("nav.nav").getByRole("link", { name: "Incidents" }).click();
  await page.locator("nav.nav").getByRole("link", { name: "Scan" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Scan");
});

test("the chat can read answers aloud, and remembers the choice", async ({ page, request }) => {
  const clip = (await sample(request)).clipWithFrames;
  test.skip(!clip, "no clip");
  await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) =>
    route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.fallback());
  await page.goto(`/#/clips/${clip.clip_id}`);
  const toggle = page.locator("section.chat").getByRole("button", { name: /Read aloud|Reading aloud/ });
  test.skip(!(await toggle.count()), "no speech synthesis in this browser");
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(page.locator("section.chat").getByRole("button", { name: "Reading aloud" })).toHaveAttribute("aria-pressed", "true");
});

test.describe("reading answers aloud (a stand-in speech engine: the test browser has none)", () => {
  const ANSWER = { message_id: "m_a", clip_id: "c", role: "assistant", content: "The battery reads 24.1 V [reading:rd_1]. It looks fine.", frame_id: null,
    anomaly_id: null, citations: [], verified: true, problems: [], run_id: null, created_at: new Date().toISOString() };

  test.beforeEach(async ({ page, request }) => {
    const clip = (await sample(request)).clipWithFrames;
    test.skip(!clip, "no clip");
    await page.addInitScript(() => {
      const said: string[] = [];
      (window as any).said = said;
      (window as any).SpeechSynthesisUtterance = class { onend: any; onerror: any; lang = ""; volume = 1; text: string; constructor(text: string) { this.text = text; } };
      Object.defineProperty(window, "speechSynthesis", { value: {
        speaking: false, pending: false, cancel() {}, resume() {},
        speak(u: any) { said.push(u.text); if ((window as any).speechFails) u.onerror?.({ error: "synthesis-failed" }); },
      } });
    });
    await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) =>
      route.request().method() === "GET" ? route.fulfill({ json: [ANSWER] })
        : route.fulfill({ json: { ...ANSWER, message_id: "m_b", content: "Second answer." } }));
    await page.goto(`/#/clips/${clip.clip_id}`);
    await expect(page.getByRole("button", { name: "Listen to this answer" })).toBeVisible();
  });
  const said = (page: any) => page.evaluate(() => (window as any).said);

  test("Listen reads one answer, without its evidence IDs, sentence by sentence", async ({ page }) => {
    await page.getByRole("button", { name: "Listen to this answer" }).click();
    expect(await said(page)).toEqual(["The battery reads 24.1 V.", "It looks fine."]);
    await page.locator("section.chat").getByRole("button", { name: "Stop" }).click();
    await expect(page.getByRole("button", { name: "Listen to this answer" })).toBeVisible();
  });

  test("turning Read aloud on speaks at once, and a question unlocks speech before its answer arrives", async ({ page }) => {
    await page.locator("section.chat").getByRole("button", { name: "Read aloud" }).click();
    expect((await said(page))[0]).toBe("The battery reads 24.1 V.");
    await page.getByLabel("Your question").fill("and now?");
    await page.getByRole("button", { name: "Send" }).click();
    await expect.poll(() => said(page)).toContain("Second answer.");
    const all: string[] = await said(page);
    expect(all.indexOf(" ")).toBeGreaterThan(-1);                     // spoken during the tap
    expect(all.indexOf(" ")).toBeLessThan(all.indexOf("Second answer."));
  });

  test("a browser that can't speak says so", async ({ page }) => {
    await page.evaluate(() => { (window as any).speechFails = true; });
    await page.getByRole("button", { name: "Listen to this answer" }).click();
    await expect(page.locator("section.chat .error")).toContainText("couldn't read the answer aloud");
  });
});
