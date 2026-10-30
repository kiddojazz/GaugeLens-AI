import { expect, getJson, sample, test } from "./fixtures";

// Approve/reject and "Run diagnosis" are intercepted in the browser, so the real database and model credits are untouched.

test.describe("diagnosed anomaly", () => {
  let anomaly: any;
  let diagnosis: any;

  test.beforeEach(async ({ request }) => {
    anomaly = (await sample(request)).diagnosed;
    test.skip(!anomaly, "no diagnosed anomaly in the database");
    diagnosis = await getJson(request, `/anomalies/${anomaly.anomaly_id}/diagnosis`);
  });

  /** Decided diagnoses can't be decided again; in the browser only, show this one as still pending. */
  async function showAsPending(page: any) {
    if (diagnosis.decision === "pending") return;
    await page.route(`**/api/v1/anomalies/${anomaly.anomaly_id}/diagnosis`, (route: any) =>
      route.fulfill({ json: { ...diagnosis, decision: "pending", decided_by: null, decided_at: null, decision_note: null } }));
  }

  test("shows the diagnosis with its evidence", async ({ page }) => {
    const o = diagnosis.output;
    await page.goto(`/#/anomalies/${anomaly.anomaly_id}`);
    await expect(page.getByText(o.headline)).toBeVisible();
    await expect(page.locator(".card-head .badge").first()).toHaveText(diagnosis.verified ? "Evidence-checked" : "Needs expert review");
    for (const obs of o.observations) await expect(page.getByText(obs.statement, { exact: false }).first()).toBeVisible();
    for (const h of o.hypotheses) await expect(page.getByText(h.cause).first()).toBeVisible();
    await expect(page.locator(".notice.action")).toHaveText(o.proposed_action);
    await expect(page.locator(".notice.safety")).toContainText(o.safety_note);
  });

  test("a citation chip opens the clip at the cited frame", async ({ page }) => {
    await page.goto(`/#/anomalies/${anomaly.anomaly_id}`);
    await page.locator("button.cite", { hasText: /^reading / }).first().click();
    await expect(page).toHaveURL(new RegExp(`#/clips/${anomaly.clip_id}\\?frame=`));
    await expect(page.locator(".viewer img")).toBeVisible();
  });

  test("the agent's investigation trace is shown (step list, or the tool list for older diagnoses)", async ({ page }) => {
    const trace = diagnosis.output.trace ?? [];
    test.skip(!trace.length, "diagnosis has no trace");
    await page.goto(`/#/anomalies/${anomaly.anomaly_id}`);
    const card = page.locator("section.card", { has: page.getByRole("heading", { name: "How the agent investigated" }) });
    await expect(card).toBeVisible();
    if (diagnosis.output.run_id) {                          // traced run: icon-led step list linking to the trace
      await expect(card.locator(".mini-trace li").first()).toBeVisible();
      await expect(card).toContainText(trace[0].tool);
      return;
    }
    const details = card.locator("details");
    await expect(details.locator("li").first()).toBeHidden();
    await details.locator("summary").click();
    await expect(details.locator("li")).toHaveCount(trace.length);
    await expect(details).toContainText(trace[0].tool);
  });

  test("approve sends the decision and note, then shows it", async ({ page, request }) => {
    await showAsPending(page);
    let posted: any = null;
    await page.route(`**/api/v1/diagnoses/${diagnosis.diagnosis_id}/decision`, (route) => {
      posted = route.request().postDataJSON();
      return route.fulfill({ json: { ...diagnosis, decision: "approved" } });
    });
    await page.goto(`/#/anomalies/${anomaly.anomaly_id}`);
    await page.getByLabel("Note (optional)").fill("Technician will check Friday");
    await page.route(`**/api/v1/anomalies/${anomaly.anomaly_id}/diagnosis`, (route) =>
      route.fulfill({ json: { ...diagnosis, decision: "approved", decided_by: "web", decided_at: new Date().toISOString(), decision_note: posted?.note } }),
    );
    await page.getByRole("button", { name: "Approve" }).click();

    await expect.poll(() => posted).toEqual({ decision: "approved", note: "Technician will check Friday" });   // the server records who
    const panel = page.locator("section.card", { has: page.getByRole("heading", { name: "Decision" }) });
    await expect(panel).toContainText("Approved");
    await expect(panel).toContainText("Technician will check Friday");
    await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);

    const real = await getJson(request, `/anomalies/${anomaly.anomaly_id}/diagnosis`);
    expect(real.decision, "the real record is untouched").toBe(diagnosis.decision);
  });

  test("reject sends a rejection", async ({ page }) => {
    await showAsPending(page);
    let posted: any = null;
    await page.route(`**/api/v1/diagnoses/${diagnosis.diagnosis_id}/decision`, (route) => {
      posted = route.request().postDataJSON();
      return route.fulfill({ json: { ...diagnosis, decision: "rejected" } });
    });
    await page.goto(`/#/anomalies/${anomaly.anomaly_id}`);
    await page.getByRole("button", { name: "Reject" }).click();
    await expect.poll(() => posted?.decision).toBe("rejected");
    expect(posted.note).toBeNull();
  });

  test("a decision conflict is shown to the user", async ({ page }) => {
    await showAsPending(page);
    await page.route(`**/api/v1/diagnoses/${diagnosis.diagnosis_id}/decision`, (route) =>
      route.fulfill({ status: 409, json: { detail: "This diagnosis was already decided" } }),
    );
    await page.goto(`/#/anomalies/${anomaly.anomaly_id}`);
    await page.getByRole("button", { name: "Approve" }).click();
    await expect(page.locator(".error")).toHaveText("This diagnosis was already decided");
    await expect(page.getByRole("button", { name: "Approve" })).toBeEnabled();
  });
});

test("'Run diagnosis' queues the agent and shows it investigating", async ({ page, request }) => {
  const { anomalies } = await sample(request);
  test.skip(!anomalies.length, "no anomalies in the database");
  const a = anomalies[0];
  const detail = await getJson(request, `/anomalies/${a.anomaly_id}`);
  let queued = false;

  await page.route(`**/api/v1/anomalies/${a.anomaly_id}/diagnosis`, (route) => route.fulfill({ status: 404, json: { detail: "no diagnosis yet" } }));
  await page.route(`**/api/v1/anomalies/${a.anomaly_id}`, (route) =>
    route.fulfill({ json: { ...detail, diagnosis_id: null, verified: null, decision: null, diagnose_job: queued ? "queued" : null } }),
  );
  await page.route(`**/api/v1/anomalies/${a.anomaly_id}/diagnose`, (route) => {
    queued = true;
    return route.fulfill({ json: { job_id: "job_test" } });
  });

  await page.goto(`/#/anomalies/${a.anomaly_id}`);
  await expect(page.getByText("No diagnosis yet.")).toBeVisible();
  await page.getByRole("button", { name: "Run diagnosis" }).click();
  await expect(page.getByText(/diagnosis agent is investigating/)).toBeVisible();
  expect(queued).toBe(true);
});

test("an unknown anomaly shows an error", async ({ page }) => {
  await page.goto("/#/anomalies/an_does_not_exist");
  await expect(page.locator(".error")).toBeVisible();
});
