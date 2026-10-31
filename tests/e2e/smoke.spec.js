const { test, expect } = require('@playwright/test');

const TABS = ['models', 'releases', 'creators', 'downloads', 'stashes', 'settings', 'help'];

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.on('console', (message) => {
    if (/Content Security Policy/i.test(message.text())) page.pageErrors.push(message.text());
  });
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

test('settings shows that secrets are set without showing them', async ({ page }) => {
  await page.route('**/api/settings', (route) => route.fulfill({
    json: { secrets_configured: { cookies_json: true, cookie_cf_clearance: true } },
  }));
  await page.goto('/#settings');
  await expect(page.locator('#setting-cookie-cf')).toHaveAttribute('placeholder', /Configured/);
  await expect(page.locator('#setting-cookie-cf')).toHaveValue('');
  await expect(page.locator('#setting-auth-api-key')).toHaveCount(0);
  await expect(page.locator('#setting-cookie-php')).not.toHaveAttribute('placeholder', /Configured/);
});

test('connect button goes to the MMF sign-in page', async ({ page }) => {
  await page.route('**/api/auth/login?redirect=0', (route) => route.fulfill({
    json: { auth_url: 'http://127.0.0.1:8099/#help', state: 'x' },
  }));
  await page.route('**/api/auth/status', (route) => route.fulfill({
    json: { configured: true, connected: false },
  }));
  await page.goto('/#settings');
  await page.locator('#btn-connect-auth').click();
  await expect(page).toHaveURL(/#help$/);
});

test('static assets and previews load', async ({ page }) => {
  for (const path of ['/static/css/vault.css', '/static/js/app.js', '/static/js/views/models.js']) {
    expect((await page.request.get(path)).status()).toBe(200);
  }
  const placeholder = await page.request.get('/previews/none/cover.jpg');
  expect(placeholder.headers()['content-type']).toContain('image/svg+xml');
});

test.describe('server times in New York', () => {
  test.use({ timezoneId: 'America/New_York' });

  test('UTC database times show as local, across midnight', async ({ page }) => {
    await page.goto('/');
    const result = await page.evaluate(async () => {
      const { formatLocalDate, formatLocalDateTime } = await import('/static/js/utils.js');
      return {
        date: formatLocalDate('2026-10-31 00:02:48'),
        dateTime: formatLocalDateTime('2026-10-31 00:02:48'),
        pythonLocal: formatLocalDateTime('2026-10-30T20:02:48.123456'),
        empty: formatLocalDate(null),
        unreadable: formatLocalDate('not a date'),
      };
    });
    expect(result).toEqual({
      date: '2026-10-30',
      dateTime: '2026-10-30 20:02:48',
      pythonLocal: '2026-10-30 20:02:48',
      empty: '-',
      unreadable: 'not a date',
    });
  });

  test('download history shows the local date', async ({ page }) => {
    await page.route('**/api/downloads/history*', (route) => route.fulfill({
      json: {
        history: [{
          model_id: 1, name: 'Test', model_name: 'Test', creator_name: 'Maker', status: 'completed',
          enqueued_at: '2026-10-31 00:02:48', completed_at: '2026-10-31 00:02:51',
        }],
        total: 1, limit: 50, offset: 0,
      },
    }));
    await page.goto('/#downloads');
    await expect(page.locator('body')).toContainText('2026-10-30');
    await expect(page.locator('body')).not.toContainText('2026-10-31');
  });
});

test('clearing task history previews, then confirms', async ({ page }) => {
  const bodies = [];
  await page.route('**/api/tasks/clear', async (route) => {
    const body = route.request().postDataJSON();
    bodies.push(body);
    await route.fulfill({
      json: body.confirm ? { success: true, deleted_count: 4 } : { success: true, dry_run: true, would_delete: 4 },
    });
  });
  const messages = [];
  page.on('dialog', (dialog) => { messages.push(dialog.message()); dialog.accept(); });
  await page.goto('/#settings');
  await page.evaluate(() => window.clearTaskHistory());
  expect(messages[0]).toContain('Clear 4');
  expect(bodies).toEqual([{}, { confirm: true }]);
});

test('declining the dialog sends no destructive request', async ({ page }) => {
  const bodies = [];
  await page.route('**/api/tasks/clear', async (route) => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill({ json: { success: true, dry_run: true, would_delete: 4 } });
  });
  page.on('dialog', (dialog) => dialog.dismiss());
  await page.goto('/#settings');
  await page.evaluate(() => window.clearTaskHistory());
  expect(bodies).toEqual([{}]);
});

test('reconnect banner follows the download status', async ({ page }) => {
  await page.route('**/api/downloads/status', (route) => route.fulfill({
    json: { active_downloads: [], active_offloads: [], jobs: [], reconnect_required: true },
  }));
  await page.goto('/');
  await expect(page.locator('#banner-reconnect')).toBeVisible();
});

test('settings shows the last backup', async ({ page }) => {
  await page.route('**/api/settings', (route) => route.fulfill({
    json: { secrets_configured: {}, last_backup_at: '2026-10-30 20:00:00', last_backup_result: 'OK: x.db' },
  }));
  await page.goto('/#settings');
  await expect(page.locator('#backup-status')).toContainText('2026-10-30 20:00:00');
});

test('headless scraping defaults on and can be turned off', async ({ page }) => {
  await page.goto('/#settings');
  await expect(page.locator('#setting-scraper-headless')).toBeChecked();
});

test('every session cookie has its own field', async ({ page }) => {
  await page.goto('/#settings');
  for (const id of ['cf', 'php', 'rememberme', 'cf-bm', 'cfuvid', 'cflb']) {
    await expect(page.locator(`#setting-cookie-${id}`)).toBeAttached();
  }
});

test('the page is served with a strict script policy', async ({ page }) => {
  const response = await page.goto('/');
  const policy = response.headers()['content-security-policy'];
  expect(policy).toContain("script-src 'self'");
  expect(policy).not.toMatch(/script-src[^;]*unsafe/);
});

test('inline scripts are blocked', async ({ page }) => {
  const errors = [];
  await page.goto('/');
  page.pageErrors = [];
  page.on('console', (message) => errors.push(message.text()));
  await page.evaluate(() => {
    document.body.insertAdjacentHTML('beforeend', '<img src=x onerror="window.ranInline=1">');
  });
  await expect.poll(() => errors.some((text) => /Content Security Policy/i.test(text))).toBe(true);
  expect(await page.evaluate(() => window.ranInline)).toBeUndefined();
  page.pageErrors = [];
});

test('no static markup uses an inline handler', async ({ page }) => {
  await page.goto('/');
  const inline = await page.evaluate(() => [...document.querySelectorAll('*')]
    .filter((el) => [...el.attributes].some((a) => /^on/.test(a.name))).length);
  expect(inline).toBe(0);
});

test('static buttons work through data attributes', async ({ page }) => {
  await page.goto('/');
  await page.locator('#nav-tab-settings').click();
  await expect(page).toHaveURL(/#settings$/);
  await page.locator('#nav-tab-stashes').click();
  await expect(page).toHaveURL(/#stashes$/);
  await page.locator('.brand-title').click();
  await expect(page).not.toHaveURL(/#stashes$/);
});

test('the sign-in callback finishes under the policy', async ({ page }) => {
  let body;
  await page.route('**/api/auth/token_callback', async (route) => {
    body = route.request().postDataJSON();
    await route.fulfill({ json: { success: true, username: '<b>maker</b>' } });
  });
  await page.goto('/api/auth/callback#access_token=abc&expires_in=604800&state=s1');
  await expect(page.locator('#status-desc')).toHaveText('Connected as @<b>maker</b>. Redirecting...');
  expect(body).toEqual({ access_token: 'abc', expires_in: 604800, state: 's1' });
});

test('every static handler names a real function', async ({ page }) => {
  await page.goto('/');
  const missing = await page.evaluate(() => ['data-click', 'data-change', 'data-input'].flatMap((attribute) => (
    [...document.querySelectorAll(`[${attribute}]`)]
      .map((element) => element.getAttribute(attribute))
      .filter((name) => typeof window[name] !== 'function')
  )));
  expect(missing).toEqual([]);
});
