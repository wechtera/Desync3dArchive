// The download job window. The Python half is DownloadJobDetailTests in tests/test_remaining_items.py.
const { test, expect } = require('@playwright/test');

const LONG = 'Size mismatch on ogre-butcher-pot.chitubox: expected 197131215, got 17192220. '.repeat(60);

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
  await page.route('**/api/downloads/history*', (route) => route.fulfill({ json: {
    total: 2, limit: 50, offset: 0, history: [
      { id: 3, model_id: 7, model_name: 'Ogre Butchers', creator_name: 'Duncan Shadow', status: 'failed', error_message: LONG },
      { id: 4, model_id: 8, model_name: 'Fine Model', creator_name: 'Maker', status: 'completed', error_message: null },
    ],
  } }));
  await page.route('**/api/downloads/jobs/3', (route) => route.fulfill({ json: {
    job: { id: 3, model_id: 7, model_name: 'Ogre Butchers', creator_name: 'Duncan Shadow', status: 'failed', error_message: LONG, retry_count: 1 },
    attempts: [{ logged_at: '2026-11-08 03:01:19', attempt_number: 1, status: 'failed', filename: 'ogre-butcher-pot.chitubox', error_detail: 'attempt one' }],
  } }));
  await page.goto('/#downloads');
  await page.evaluate(async () => {
    const { actions } = await import('/static/js/actions.js');
    actions.switchDownloadSubView('history');
  });
  await expect(page.locator('.history-error-link')).toHaveCount(1);
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
});

test('a job with an error opens its full text', async ({ page }) => {
  await page.locator('.history-error-link').click();
  const window = page.locator('#job-detail');
  await expect(window.locator('.job-detail-title')).toHaveText('Ogre Butchers');
  await expect(window.locator('.job-detail-error').first()).toHaveText(LONG.trim());
  await expect(window).toContainText('attempt one');
  await expect(window).toContainText('ogre-butcher-pot.chitubox');
});

test('a long error scrolls inside its box', async ({ page }) => {
  await page.locator('.history-error-link').click();
  const box = page.locator('.job-detail-error').first();
  await expect(box).toBeVisible();
  const scrolls = await box.evaluate((element) => element.scrollHeight > element.clientHeight);
  expect(scrolls).toBe(true);
});

test('the X closes it', async ({ page }) => {
  await page.locator('.history-error-link').click();
  await page.locator('#job-detail .stl-preview-close').click();
  await expect(page.locator('#job-detail')).toHaveCount(0);
});

test('a click outside closes it, and one inside does not', async ({ page }) => {
  await page.locator('.history-error-link').click();
  await page.locator('.job-detail-error').first().click();
  await expect(page.locator('#job-detail')).toHaveCount(1);
  await page.mouse.click(4, 4);
  await expect(page.locator('#job-detail')).toHaveCount(0);
});

test('a row with no error has nothing to open', async ({ page }) => {
  await expect(page.locator('#history-table-body tr').nth(1).locator('button.history-error-link')).toHaveCount(0);
});
