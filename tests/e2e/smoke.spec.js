const { test, expect } = require('@playwright/test');

const TABS = ['models', 'releases', 'creators', 'downloads', 'stashes', 'settings', 'help'];

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.badResponses = [];
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/') && response.status() >= 500) {
      page.badResponses.push(`${response.status()} ${url.pathname}`);
    }
  });
  // No test may reach MMF or anything else outside the local app.
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
  expect(page.badResponses).toEqual([]);
});

test('home page shows the catalog', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/DESYNC/);
  await expect(page.locator('.model-card-actions').first()).toBeAttached();
});

for (const tab of TABS) {
  test(`${tab} view opens`, async ({ page }) => {
    await page.goto(`/#${tab}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('body')).not.toContainText('Internal Server Error');
    expect(await page.evaluate(() => window.location.hash)).toBe(`#${tab}`);
  });
}

test('the tasks route opens inside settings', async ({ page }) => {
  await page.goto('/#tasks');
  await expect(page).toHaveURL(/#settings$/);
});

test('navigation tabs switch views', async ({ page }) => {
  await page.goto('/');
  await page.locator('#nav-tab-settings').click();
  await expect(page).toHaveURL(/#settings$/);
  await page.locator('#nav-tab-stashes').click();
  await expect(page).toHaveURL(/#stashes$/);
});

test('catalog search calls the API and renders results', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('.model-card-actions').first()).toBeAttached();
  const search = page.waitForResponse((r) => r.url().includes('/api/models') && r.url().includes('search=dragon'));
  await page.locator('#model-search').fill('dragon');
  const response = await search;
  expect(response.status()).toBe(200);
  const body = await response.json();
  expect(body.total ?? body.models?.length ?? 0).toBeGreaterThan(0);
});

test('static assets and previews load', async ({ page }) => {
  for (const path of ['/static/css/vault.css', '/static/js/app.js', '/static/js/views/models.js']) {
    expect((await page.request.get(path)).status()).toBe(200);
  }
  const placeholder = await page.request.get('/previews/none/cover.jpg');
  expect(placeholder.headers()['content-type']).toContain('image/svg+xml');
});
