import { expect, getJson, sample, test } from "./fixtures";

// "Ask the agent" on the clip and anomaly pages. The conversation and the model are intercepted in the browser:
// these tests never call the model or change the stored conversation.

function reply(clipId: string, content: string, extra: Record<string, unknown> = {}) {
  return {
    message_id: `msg_test_${Math.random().toString(36).slice(2)}`, clip_id: clipId, role: "assistant", content,
    frame_id: null, anomaly_id: null, citations: [], verified: true, problems: [], run_id: null,
    created_at: new Date().toISOString(), ...extra,
  };
}

test.describe("on the clip page", () => {
  let clip: any;
  let frames: any;
  let readings: any[];

  test.beforeEach(async ({ page, request }) => {
    clip = (await sample(request)).clipWithFrames;
    test.skip(!clip, "no finished clip with frames");
    frames = await getJson(request, `/clips/${clip.clip_id}/frames`);
    readings = await getJson(request, `/readings?clip_id=${clip.clip_id}&limit=5000`);
    await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) =>
      route.request().method() === "GET" ? route.fulfill({ json: [] }) : route.fallback());
  });

  test("starts with suggested questions and knows the frame in view", async ({ page }) => {
    await page.goto(`/#/clips/${clip.clip_id}`);
    const chat = page.locator("section.chat");
    await expect(chat.getByRole("heading", { name: "Ask the agent" })).toBeVisible();
    await expect(chat.locator(".suggestion").first()).toBeVisible();
    await expect(chat.locator(".context-chip")).toContainText("frame 1");
    await page.getByRole("button", { name: "Next frame" }).click();
    await expect(chat.locator(".context-chip")).toContainText("frame 2");
  });

  test("a question goes out with the frame in view, and a citation moves the viewer", async ({ page }) => {
    const target = readings.find((r) => r.frame_id !== frames.frames[0].frame_id && frames.frames.some((f: any) => f.frame_id === r.frame_id));
    test.skip(!target, "no reading on a later frame");
    const targetIndex = frames.frames.findIndex((f: any) => f.frame_id === target.frame_id);
    let body: any = null;
    await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      body = route.request().postDataJSON();
      return route.fulfill({ json: reply(clip.clip_id, `It read \`${target.raw_text}\` there [reading:${target.reading_id}].\n- **Evidence-checked**`, { run_id: "run_test" }) });
    });
    await page.goto(`/#/clips/${clip.clip_id}`);
    const chat = page.locator("section.chat");
    await chat.getByLabel("Your question").fill("What does it read?");
    await chat.getByLabel("Your question").press("Enter");

    await expect(chat.locator(".bubble.user")).toHaveText(/What does it read\?/);
    await expect(chat.locator(".bubble.agent .answer")).toContainText(target.raw_text);
    await expect(chat.locator(".bubble.agent code")).toHaveText(target.raw_text);           // `value` -> code
    await expect(chat.locator(".bubble.agent li strong")).toHaveText("Evidence-checked");   // bullets, bold
    await expect(chat.locator(".bubble.agent .checked")).toBeVisible();
    await expect(chat.getByRole("link", { name: "Trace" })).toHaveAttribute("href", "#/traces/run_test");
    expect(body).toEqual({ question: "What does it read?", frame_id: frames.frames[0].frame_id, anomaly_id: null });

    await chat.locator(".bubble.agent .cite").click();
    await expect(page.locator(".card-head", { hasText: "Footage" })).toContainText(`frame ${targetIndex + 1} / ${frames.frames.length}`);
    await expect(page).toHaveURL(new RegExp(`frame=${target.frame_id}`));
  });

  test("a suggestion asks straight away; the frame can be left out", async ({ page }) => {
    const bodies: any[] = [];
    await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      bodies.push(route.request().postDataJSON());
      return route.fulfill({ json: reply(clip.clip_id, "Summary.") });
    });
    await page.goto(`/#/clips/${clip.clip_id}`);
    const chat = page.locator("section.chat");
    await chat.getByRole("button", { name: "Don't include this frame" }).click();
    await chat.locator(".suggestion", { hasText: "Summarise this clip" }).click();
    await expect(chat.locator(".bubble.agent")).toHaveText(/Summary\./);
    expect(bodies[0]).toMatchObject({ question: "Summarise this clip", frame_id: null });
  });

  test("while the agent thinks, a typing indicator shows and input is locked", async ({ page }) => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      await gate;
      return route.fulfill({ json: reply(clip.clip_id, "Done.") });
    });
    await page.goto(`/#/clips/${clip.clip_id}`);
    const chat = page.locator("section.chat");
    await chat.getByLabel("Your question").fill("Anything unusual?");
    await chat.getByRole("button", { name: "Send" }).click();
    await expect(chat.locator(".thinking")).toBeVisible();
    await expect(chat.getByLabel("Your question")).toBeDisabled();
    release();
    await expect(chat.locator(".thinking")).toHaveCount(0);
    await expect(chat.getByLabel("Your question")).toBeEnabled();
  });

  test("a failed answer shows the error and keeps the question to retry", async ({ page }) => {
    await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) =>
      route.request().method() === "POST" ? route.fulfill({ status: 503, json: { detail: "model unavailable" } }) : route.fallback());
    await page.goto(`/#/clips/${clip.clip_id}`);
    const chat = page.locator("section.chat");
    await chat.getByLabel("Your question").fill("Is the battery okay?");
    await chat.getByRole("button", { name: "Send" }).click();
    await expect(chat.locator(".error")).toHaveText("model unavailable");
    await expect(chat.getByLabel("Your question")).toHaveValue("Is the battery okay?");
    await expect(chat.locator(".bubble.user")).toHaveCount(0);
  });

  test("an unverified answer says so, and 'New conversation' clears the chat", async ({ page }) => {
    let deleted = false;
    await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) => {
      const method = route.request().method();
      if (method === "DELETE") { deleted = true; return route.fulfill({ status: 204, body: "" }); }
      if (method === "POST") return route.fulfill({ json: reply(clip.clip_id, "Maybe 99 A.", { verified: false, problems: ["number 99 not in any tool result"] }) });
      return route.fallback();
    });
    await page.goto(`/#/clips/${clip.clip_id}`);
    const chat = page.locator("section.chat");
    await chat.getByLabel("Your question").fill("Peak current?");
    await chat.getByRole("button", { name: "Send" }).click();
    await expect(chat.locator(".unchecked")).toHaveText(/Couldn't verify/);
    await expect(chat.locator(".unchecked")).toHaveAttribute("title", /number 99/);
    await chat.getByRole("button", { name: "New conversation" }).click();
    await expect(chat.locator(".bubble")).toHaveCount(0);
    expect(deleted).toBe(true);
    await expect(chat.locator(".suggestion").first()).toBeVisible();
  });
});

test("on the anomaly page, questions carry the anomaly and citations open the clip", async ({ page, request }) => {
  const { diagnosed } = await sample(request);
  test.skip(!diagnosed, "no diagnosed anomaly");
  const checks = await getJson<any[]>(request, `/clips/${diagnosed.clip_id}/checks`);
  const check = checks.find((c) => c.frame_id);
  test.skip(!check, "no check with a frame");
  let body: any = null;
  await page.route(`**/api/v1/clips/${diagnosed.clip_id}/chat`, (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: [] });
    body = route.request().postDataJSON();
    return route.fulfill({ json: reply(diagnosed.clip_id, `See [check:${check.check_id}].`) });
  });
  await page.goto(`/#/anomalies/${diagnosed.anomaly_id}`);
  const chat = page.locator("section.chat");
  await expect(chat.locator(".context-chip")).toHaveCount(0);                 // no frame in view here
  await chat.locator(".suggestion").first().click();
  await expect(chat.locator(".bubble.agent .cite")).toBeVisible();
  expect(body.anomaly_id).toBe(diagnosed.anomaly_id);
  await chat.locator(".bubble.agent .cite").click();
  await expect(page).toHaveURL(new RegExp(`#/clips/${diagnosed.clip_id}\\?frame=${check.frame_id}`));
});

test("the stored conversation loads when the page opens", async ({ page, request }) => {
  const clip = (await sample(request)).clipWithFrames;
  test.skip(!clip, "no clip");
  await page.route(`**/api/v1/clips/${clip.clip_id}/chat`, (route) => route.fulfill({ json: [
    { ...reply(clip.clip_id, "Earlier question"), role: "user" },
    reply(clip.clip_id, "Earlier answer"),
  ] }));
  await page.goto(`/#/clips/${clip.clip_id}`);
  const chat = page.locator("section.chat");
  await expect(chat.locator(".bubble")).toHaveCount(2);
  await expect(chat.locator(".suggestion")).toHaveCount(0);
});
