const os = require('os');
const path = require('path');
const { defineConfig } = require('@playwright/test');

const devDir = process.env.DESYNC_DEV_DIR || path.join(os.homedir(), 'DESYNC_Data_dev');
const repoRoot = path.resolve(__dirname, '../..');
const port = 8099;

module.exports = defineConfig({
  testDir: '.',
  timeout: 30000,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: 'chromium',
    headless: true,
  },
  webServer: {
    // Starts from a fresh sanitized database, with workers off and outside network blocked.
    command: `cp "${devDir}/library.clean.db" "${devDir}/old_app/library.db" && ` +
      `"${repoRoot}/.venv/bin/python" "${repoRoot}/scripts/baseline/old_app_server.py" ` +
      `--code new --port ${port} --app-root "${devDir}/old_app"`,
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: false,
    timeout: 30000,
  },
});
