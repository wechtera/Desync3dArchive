// LAN mode in a real browser, over real TLS, with real Secure and SameSite=Strict cookies.
const path = require('path');
const { spawn } = require('child_process');
const { test, expect } = require('@playwright/test');

const PORT = 8098;
const PASSWORD = 'correct horse battery';
const BASE = `https://localhost:${PORT}`;

test.use({ baseURL: BASE, ignoreHTTPSErrors: true });

let server;

test.beforeAll(async ({ request }) => {
  const python = path.resolve(__dirname, '../../.venv/bin/python');
  server = spawn(python, [path.join(__dirname, 'lan_server.py'), String(PORT), PASSWORD], { stdio: 'ignore', detached: true });
  await expect(async () => {
    expect((await request.get(`${BASE}/login`, { ignoreHTTPSErrors: true })).status()).toBe(200);
  }).toPass({ timeout: 20000 });
});

test.afterAll(() => {
  process.kill(-server.pid, 'SIGTERM');
});

async function logIn(page, password = PASSWORD) {
  await page.goto('/login');
  await page.fill('#login-password', password);
  await page.click('button[type=submit]');
}

test('an anonymous browser lands on the login page', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(`${BASE}/login`);
});

test('a wrong password shows an error and stays on the page', async ({ page }) => {
  await logIn(page, 'not the right password');
  await expect(page.locator('#login-error')).toHaveText('Wrong password.');
  await expect(page).toHaveURL(`${BASE}/login`);
});

test('login opens the app, and Log Out closes it', async ({ page, context }) => {
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await logIn(page);
  await expect(page).toHaveURL(`${BASE}/#models`);

  const [cookie] = await context.cookies();
  expect(cookie).toMatchObject({ name: 'desync_session', httpOnly: true, secure: true, sameSite: 'Strict' });

  await page.goto('/#settings');
  await page.click('#btn-logout');
  await expect(page).toHaveURL(`${BASE}/login`);

  await page.goto('/');
  await expect(page).toHaveURL(`${BASE}/login`);
  expect(errors).toEqual([]);
});

test('an expired session sends the open app back to login', async ({ page, context }) => {
  await logIn(page);
  await expect(page).toHaveURL(`${BASE}/#models`);
  await context.clearCookies();
  // The app's own polling may get the 401 first, and either one navigates away mid-call.
  await page.evaluate(() => fetch('/api/settings')).catch(() => {});
  await expect(page).toHaveURL(`${BASE}/login`);
});

test('a live session skips the login page', async ({ page }) => {
  await logIn(page);
  await expect(page).toHaveURL(`${BASE}/#models`);
  await page.goto('/login');
  await expect(page).toHaveURL(`${BASE}/#models`);
});
