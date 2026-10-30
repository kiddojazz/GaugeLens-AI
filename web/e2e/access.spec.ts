import { expect, expectNoHorizontalScroll, test } from "./fixtures";

// Guests and accounts. These tests start without the admin sign-in the other tests use.
test.describe("as a guest", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("a guest gets a private session, is told what happens to their data, and sees no admin pages", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".guest-banner")).toContainText("deleted when you refresh or close this page");
    await expect(page.locator("nav.nav").getByRole("link", { name: "Traces" })).toHaveCount(0);
    await expect(page.locator("nav.nav").getByRole("link", { name: "People" })).toHaveCount(0);
    await page.goto("/#/clips");
    await expect(page.getByText("No clips yet")).toBeVisible();           // nobody else's clips
    await page.goto("/#/people");
    await expect(page.getByText("This page is for administrators")).toBeVisible();
  });

  test("refreshing the page ends the guest session on the server and starts a new one", async ({ page, request }) => {
    const first = page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/guest") && r.ok());
    await page.goto("/");
    const old = (await (await first).json()).token as string;
    const asOld = { headers: { "X-GaugeLens-Session": old } };
    expect((await request.get("/api/v1/auth/me", asOld)).status()).toBe(200);

    const second = page.waitForResponse((r) => r.url().endsWith("/api/v1/auth/guest") && r.ok());
    await page.reload();
    expect((await (await second).json()).token).not.toEqual(old);
    await expect.poll(async () => (await request.get("/api/v1/auth/me", asOld)).status()).toBe(401);   // beacon arrived
    await expect(page.locator(".guest-banner")).toBeVisible();
  });

  test("creating an account keeps you signed in across a refresh, and signing out returns you to guest", async ({ page }) => {
    const email = `e2e-${Date.now()}-${Math.floor(Math.random() * 1e6)}@gaugelens.test`;
    await page.goto("/");
    await page.locator(".guest-banner").getByRole("link", { name: "Create a free account" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Create an account");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("a long enough password");
    await page.locator("form").getByRole("button", { name: "Create account" }).click();

    await expect(page.locator(".topbar")).toContainText(email);
    await expect(page.locator(".guest-banner")).toHaveCount(0);
    await page.reload();
    await expect(page.locator(".topbar")).toContainText(email);

    await page.locator(".topbar").getByRole("button", { name: "Sign out" }).click();
    await expect(page.locator(".guest-banner")).toBeVisible();
    await expect(page.locator(".session-notice")).toContainText("signed out");
  });

  test("on a phone, the guest notice and the account page fit the screen", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    for (const path of ["/", "/#/scan", "/#/account"]) {
      await page.goto(path);
      await expect(page.locator(path === "/#/account" ? "form" : ".guest-banner")).toBeVisible();
      await expectNoHorizontalScroll(page);
    }
    await expect(page.locator(".topbar").getByRole("link", { name: "Sign in" })).toHaveCount(0);   // on /account itself
  });

  test("a wrong password is refused with a clear message", async ({ page }) => {
    await page.goto("/#/account");
    await page.getByLabel("Email").fill("nobody@gaugelens.test");
    await page.getByLabel("Password").fill("wrong password");
    await page.locator("form").getByRole("button", { name: "Sign in" }).click();
    await expect(page.locator(".error")).toHaveText("wrong email or password");
  });
});

test("an admin sees the People page and who owns each clip", async ({ page }) => {
  await page.goto("/#/people");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("People");
  await expect(page.locator("section.card", { has: page.getByRole("heading", { name: "Accounts" }) })).toContainText("e2e-admin@gaugelens.test");
  await expect(page.locator(".topbar .badge.admin")).toHaveText("Admin");
  await page.locator("nav.nav").getByRole("link", { name: "Clips" }).click();
  await expect(page.locator("thead")).toContainText("Owner");
});
