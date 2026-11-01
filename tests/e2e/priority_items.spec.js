// Browser side of the review's priority list. The Python half is tests/test_priority_items.py.
const { test, expect } = require('@playwright/test');

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.dialogs = [];
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  page.on('dialog', async (dialog) => {
    page.dialogs.push(dialog.message());
    await dialog.accept();
  });
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
});

test.describe('queue removal, R-17', () => {
  function answerDelete(page, json) {
    return page.route('**/api/downloads/queue', (route) => (
      route.request().method() === 'DELETE' ? route.fulfill({ json }) : route.fallback()
    ));
  }

  test('a job that had already started says it stayed', async ({ page }) => {
    await answerDelete(page, { success: true, removed: 0, kept: 1 });
    await page.goto('/');
    await page.evaluate(() => window.dequeueSingleModel(5));
    await expect.poll(() => page.dialogs.length).toBe(1);
    expect(page.dialogs[0]).toContain('stayed');
  });

  test('a job that was removed says nothing', async ({ page }) => {
    await answerDelete(page, { success: true, removed: 1, kept: 0 });
    await page.goto('/');
    await page.evaluate(() => window.dequeueSingleModel(5));
    expect(page.dialogs).toEqual([]);
  });
});

test('the duplicate panel says how many files it left out, R-13', async ({ page }) => {
  const results = {
    success: true, has_results: true, include_shared: false, pairs: [], files: [], models: [],
    summary: { pairs: 0, excluded_files: 13579 },
  };
  await page.route('**/api/reconcile/duplicates/latest', (route) => route.fulfill({ json: results }));
  await page.goto('/#downloads');
  await page.locator('#group-reconcile').click();
  const panel = page.locator('[data-card="duplicate_scan"] #duplicate-results-panel');
  await panel.locator('summary').click();
  await expect(panel).toContainText('13,579 files named as bases or supports were left out.');
});

