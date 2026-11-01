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

test.describe('polling, P-01', () => {
  test('a hidden tab stops polling and catches up when shown', async ({ page }) => {
    let polls = 0;
    await page.route('**/api/tasks/status*', (route) => {
      polls += 1;
      return route.fulfill({ json: { active_task: null, recent_tasks: [] } });
    });
    const setHidden = (hidden) => page.evaluate((value) => {
      Object.defineProperty(document, 'hidden', { configurable: true, value });
      document.dispatchEvent(new Event('visibilitychange'));
    }, hidden);
    await page.goto('/');
    await expect.poll(() => polls).toBeGreaterThan(0);
    await setHidden(true);
    await page.waitForTimeout(200);
    const before = polls;
    await page.evaluate(() => window.checkHealth());
    expect(polls).toBe(before);
    await setHidden(false);
    await expect.poll(() => polls).toBeGreaterThan(before);
  });
});


test.describe('reconcile discrepancies, E49', () => {
  const status = {
    status: 'completed', has_data: true, scan_mode: 'deep', summary: { total_audited: 2, healthy: 0, discrepancies: 2 },
    discrepancies: [
      { model_id: 1, model_name: 'From MMF', creator_name: 'A', source: 'mmf', issues: [{ defect: 'METADATA_MISSING', detail: 'x', expected_path: '/v/a' }] },
      { model_id: 2, model_name: 'Linked', creator_name: 'B', source: 'external', issues: [{ defect: 'FILE_NOT_FOUND', detail: 'y', expected_path: '/l/b' }] },
    ],
  };

  test('only an MMF collection offers a reset', async ({ page }) => {
    await page.route('**/api/reconcile/status*', (route) => route.fulfill({ json: status }));
    await page.goto('/#downloads');
    await page.locator('#group-reconcile').click();
    await expect(page.locator('.reconcile-card')).toHaveCount(2);
    await expect(page.locator('.reconcile-card').filter({ hasText: 'From MMF' }).getByRole('button', { name: 'Reset for Retry' })).toHaveCount(1);
    const linked = page.locator('.reconcile-card').filter({ hasText: 'Linked' });
    await expect(linked.getByRole('button', { name: 'Reset for Retry' })).toHaveCount(0);
    await expect(linked).toContainText("Can't be reset");
  });

  test('reset all leaves linked collections out', async ({ page }) => {
    let sent = null;
    await page.route('**/api/reconcile/status*', (route) => route.fulfill({ json: status }));
    await page.route('**/api/reconcile/reset', (route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, reset_count: 1 } });
    });
    await page.goto('/#downloads');
    await page.locator('#group-reconcile').click();
    await expect(page.locator('.reconcile-card')).toHaveCount(2);
    await page.evaluate(() => window.resetAllReconcileDiscrepancies());
    await expect.poll(() => sent).toMatchObject({ model_ids: [1] });
    await expect(page.locator('.reconcile-card')).toHaveCount(1);
  });
});
