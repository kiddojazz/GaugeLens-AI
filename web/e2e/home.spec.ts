import { expect, expectNoHorizontalScroll, test } from "./fixtures";

test.describe("home page", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("explains GaugeLens and leads straight to a scan or an upload", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Verified readings from any digital display");
    await expect(page.locator(".home-steps li")).toHaveCount(4);
    await expect(page.getByText("No account needed.")).toBeVisible();
    await page.locator(".home-actions").getByRole("link", { name: "Start a scan" }).click();
    await expect(page).toHaveURL(/#\/scan$/);
    await page.goBack();
    await page.locator(".home-actions").getByRole("link", { name: "Upload a video or photos" }).click();
    await expect(page).toHaveURL(/#\/upload$/);
  });

  test("the logo goes home from any page, and Home is marked in the menu", async ({ page }) => {
    await page.goto("/#/clips");
    await page.getByRole("link", { name: "GaugeLens home" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Verified readings from any digital display");
    await expect(page.locator("nav.nav").getByRole("link", { name: "Home" })).toHaveClass(/active/);
    await expect(page.locator("nav.nav a.active")).toHaveCount(1);
  });

  test("fits a phone", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await expect(page.locator(".home-example")).toBeVisible();
    await expectNoHorizontalScroll(page);
  });
});

test("signed in, the home page points to your results", async ({ page }) => {
  await page.goto("/");
  await page.locator(".home-hero").getByRole("link", { name: "Incidents" }).click();
  await expect(page).toHaveURL(/#\/incidents$/);
});
