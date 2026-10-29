import { expect, expectImageLoaded, getJson, test } from "./fixtures";

// Traces and evaluations. "Run evaluation" is intercepted: a real run would spend model credits.

test("traces list shows every agent run and opens one", async ({ page, request }) => {
  const runs = await getJson<any[]>(request, "/traces");
  test.skip(!runs.length, "no agent runs recorded yet");
  await page.goto("/#/traces");
  const rows = page.locator("tbody tr");
  await expect(rows).toHaveCount(runs.length);
  await rows.first().click();
  await expect(page).toHaveURL(new RegExp(`#/traces/${runs[0].run_id}`));
});

test.describe("trace page", () => {
  let trace: any;

  test.beforeEach(async ({ request }) => {
    const runs = await getJson<any[]>(request, "/traces");
    test.skip(!runs.length, "no agent runs recorded yet");
    trace = await getJson(request, `/traces/${runs[0].run_id}`);
  });

  test("shows every step with its icon and the run's totals", async ({ page }) => {
    await page.goto(`/#/traces/${trace.run.run_id}`);
    const steps = page.locator(".span-row");
    await expect(steps).toHaveCount(trace.spans.length);
    await expect(steps.locator(".span-icon")).toHaveCount(trace.spans.length);
    for (const [i, s] of trace.spans.entries()) await expect(steps.nth(i).locator(".span-title")).toHaveText(s.name);
    const stats = page.locator(".trace-stats");
    await expect(stats).toContainText(String(trace.run.llm_calls));
    await expect(stats).toContainText(trace.run.model);
  });

  test("selecting a step drills into its output, input and attributes", async ({ page }) => {
    const tool = trace.spans.find((s: any) => s.kind === "tool" && s.status === "ok");
    test.skip(!tool, "no successful tool call in this run");
    await page.goto(`/#/traces/${trace.run.run_id}`);
    await page.locator(".span-row", { has: page.locator(".span-title", { hasText: tool.name }) }).first().click();
    await expect(page).toHaveURL(/span=sp_/);
    const detail = page.locator(".span-detail");
    await expect(detail.locator("h2")).toHaveText(tool.name);
    await detail.getByRole("tab", { name: "Attributes" }).click();
    await expect(detail).toContainText("gen_ai.tool.name");
    await detail.getByRole("tab", { name: "Input" }).click();
    await expect(detail.getByRole("tab", { name: "Input" })).toHaveAttribute("aria-selected", "true");
  });

  test("a model step shows its messages and token usage", async ({ page }) => {
    const llm = trace.spans.find((s: any) => s.kind === "llm");
    await page.goto(`/#/traces/${trace.run.run_id}?span=${llm.span_id}`);
    const detail = page.locator(".span-detail");
    await expect(detail.locator("h2")).toHaveText(llm.name);
    await detail.getByRole("tab", { name: "Input" }).click();
    await expect(detail.locator(".msg").first()).toBeVisible();
    await detail.getByRole("tab", { name: "Attributes" }).click();
    await expect(detail).toContainText("gen_ai.usage.input_tokens");
  });
});

test("the frame the agent looked at is shown in its step", async ({ page, request }) => {
  const runs = await getJson<any[]>(request, "/traces");
  for (const run of runs.slice(0, 10)) {
    const trace = await getJson(request, `/traces/${run.run_id}`);
    const frame = trace.spans.find((s: any) => s.name === "view_frame" && s.status === "ok");
    if (!frame) continue;
    await page.goto(`/#/traces/${run.run_id}?span=${frame.span_id}`);
    await expectImageLoaded(page.locator(".span-image"));
    return;
  }
  test.skip(true, "no successful view_frame in recent runs");
});

test("evaluations show the latest run's scenarios, criteria and trace links", async ({ page, request }) => {
  const evals = await getJson<any[]>(request, "/evals");
  test.skip(!evals.length, "no evaluations yet");
  const detail = await getJson(request, `/evals/${evals[0].eval_run_id}`);
  await page.goto("/#/evals");
  const results = page.locator("section.card", { has: page.getByRole("heading", { name: "Results" }) });
  await expect(results.locator("tbody tr")).toHaveCount(detail.results.length);
  const first = detail.results[0];
  const row = results.locator("tbody tr").first();
  await expect(row).toContainText(first.title);
  await expect(row.locator(".criteria li")).toHaveCount(Object.keys(first.criteria).length);
  await expect(page.locator(".grid-stats .stat").nth(1)).toContainText(`${evals[0].passed} / ${evals[0].scenarios}`);
  if (first.run_id) {
    await row.getByRole("link", { name: /Trace/ }).click();
    await expect(page).toHaveURL(new RegExp(`#/traces/${first.run_id}`));
  }
});

test("the scenario catalog lists every scenario", async ({ page, request }) => {
  const scenarios = await getJson<any[]>(request, "/evals/scenarios");
  await page.goto("/#/evals");
  const card = page.locator("section.card", { has: page.getByRole("heading", { name: "Scenarios" }) });
  await expect(card.locator("li")).toHaveCount(scenarios.length);
});

test("'Run evaluation' asks first, then queues a run", async ({ page }) => {
  let posted = 0;
  await page.route("**/api/v1/evals", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    posted += 1;
    return route.fulfill({ status: 202, json: { eval_run_id: "ev_test", job_id: "job_test" } });
  });
  await page.goto("/#/evals");
  page.once("dialog", (d) => d.dismiss());                         // cancel: nothing is sent
  await page.getByRole("button", { name: /Run evaluation/ }).click();
  expect(posted).toBe(0);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: /Run evaluation/ }).click();
  await expect.poll(() => posted).toBe(1);
  await expect(page).toHaveURL(/run=ev_test/);
});

test("a traced diagnosis links from its anomaly page to the full trace", async ({ page, request }) => {
  const runs = (await getJson<any[]>(request, "/traces")).filter((r) => !r.eval_run_id && r.anomaly_id);
  test.skip(!runs.length, "no traced diagnosis of a real anomaly yet");
  await page.goto(`/#/anomalies/${runs[0].anomaly_id}`);
  const card = page.locator("section.card", { has: page.getByRole("heading", { name: "How the agent investigated" }) });
  await expect(card.locator(".span-icon").first()).toBeVisible();
  await card.getByRole("link", { name: /Full trace/ }).click();
  await expect(page).toHaveURL(new RegExp(`#/traces/${runs[0].run_id}`));
});
