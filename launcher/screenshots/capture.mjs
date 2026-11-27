// Regenerates the README's launcher screenshots (docs/screenshots/): serves the frontend with Vite, with mock.js in
// place of the Rust side, and captures each shot below in headless Chromium.
//
//   npm run screenshots
//
// Needs the browser once: npx playwright install chromium
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = fileURLToPath(new URL('..', import.meta.url));
const outDir = fileURLToPath(new URL('../../docs/screenshots/', import.meta.url));

// the window size in tauri.conf.json
const VIEWPORT = { width: 1080, height: 720 };

const dataUrl = async (path, type) => `data:${type};base64,${await readFile(root + path, 'base64')}`;
const icon = await dataUrl('public/icon.png', 'image/png');
const font = await dataUrl('node_modules/@fontsource-variable/open-sans/files/open-sans-latin-wght-normal.woff2', 'font/woff2');
const title = JSON.parse(await readFile(root + 'src-tauri/tauri.conf.json', 'utf8')).app.windows[0].title;

/** Room around the window for its shadow. */
const MARGIN = 24;

/** A page showing `shot` (a PNG) in a Windows 11 style window: title bar, rounded corners, thin border and shadow. */
const windowHtml = (shot) => `<!doctype html>
<style>
  @font-face { font-family: title; src: url(${font}); }
  body { margin: 0; padding: ${MARGIN}px; }
  .window { width: ${VIEWPORT.width}px; border: 1px solid #45476b; border-radius: 9px; overflow: hidden; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35); }
  .bar { display: flex; align-items: center; height: 32px; background: #1c1d38; color: #e6ebf3; font: 12px title; }
  .bar img { width: 16px; height: 16px; margin: 0 8px 0 12px; }
  .bar span { flex: 1; }
  .bar svg { width: 10px; height: 10px; padding: 11px 18px; fill: none; stroke: currentColor; }
  .shot { display: block; width: ${VIEWPORT.width}px; height: ${VIEWPORT.height}px; }
</style>
<div class="window">
  <div class="bar">
    <img src="${icon}" alt=""><span>${title}</span>
    <svg viewBox="0 0 10 10"><path d="M0 5h10"/></svg>
    <svg viewBox="0 0 10 10"><rect x=".5" y=".5" width="9" height="9" rx="1"/></svg>
    <svg viewBox="0 0 10 10"><path d="M0 0l10 10M10 0L0 10"/></svg>
  </div>
  <img class="shot" src="data:image/png;base64,${shot.toString('base64')}" alt="">
</div>`;

/**
 * `scenario` is a state mock.js knows. `click` opens what to show, in order. `ready` is a selector that appears once the
 * page has everything it asks the backend for. `scrollTo` brings an element to the top of the page area.
 */
const SHOTS = [
  { file: 'launcher-play.png', scenario: 'ready', ready: '#play:enabled' },
  { file: 'launcher-setup.png', scenario: 'building', click: ['[data-page="setup"]'], ready: '#steps-card progress', scrollTo: '#steps-card' },
  {
    file: 'launcher-settings.png',
    scenario: 'ready',
    click: ['[data-page="settings"]', '[data-tab="graphics"]'],
    ready: '#f-upscaler',
  },
];

/** Puts mock.js ahead of src/main.js in index.html. */
const mockBackend = {
  name: 'mock-backend',
  transformIndexHtml: () => [{ tag: 'script', attrs: { type: 'module', src: '/screenshots/mock.js' }, injectTo: 'head' }],
};

async function capture(browser, base, shot) {
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2, colorScheme: 'dark', locale: 'en-US' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  try {
    await page.goto(`${base}?scenario=${shot.scenario}`, { waitUntil: 'networkidle' });
    for (const selector of shot.click ?? []) await page.click(selector);
    await page.waitForSelector(shot.ready);
    await page.waitForLoadState('networkidle');
    // a font that fails to load would leave the picture in a fallback face
    const badFonts = await page.evaluate(async () => {
      await document.fonts.ready;
      return [...document.fonts].filter((f) => f.status === 'error').map((f) => f.family);
    });
    if (badFonts.length) errors.push(`fonts that did not load: ${[...new Set(badFonts)].join(', ')}`);
    if (shot.scrollTo) {
      await page.locator(shot.scrollTo).evaluate((el) => {
        el.style.scrollMarginTop = 'var(--space-6)'; // the page's own top padding
        el.scrollIntoView({ block: 'start' });
      });
    }
    await page.mouse.move(0, 0); // no hover styles
    await page.evaluate(() => document.activeElement?.blur()); // no focus ring
    const unmocked = await page.evaluate(() => window.__unmocked);
    if (unmocked.length) errors.push(`commands without a reply in mock.js: ${[...new Set(unmocked)].join(', ')}`);
    if (errors.length) throw new Error(errors.join('\n'));
    const png = await page.screenshot({ animations: 'disabled' });
    const framed = await context.newPage();
    await framed.setContent(windowHtml(png));
    await framed.evaluate(async () => {
      await Promise.all([...document.images].map((img) => img.decode()));
      await document.fonts.ready;
    });
    // sized to the window and its margin (title bar 32, border 2), transparent around them
    await framed.setViewportSize({ width: VIEWPORT.width + 2 + 2 * MARGIN, height: VIEWPORT.height + 34 + 2 * MARGIN });
    await writeFile(outDir + shot.file, await framed.screenshot({ omitBackground: true }));
    console.log(`  ${shot.file}`);
  } catch (e) {
    throw new Error(`${shot.file}: ${[...errors, e.message].join('\n')}`);
  } finally {
    await context.close();
  }
}

const server = await createServer({
  root,
  logLevel: 'warn',
  plugins: [mockBackend],
  // its own port, so a running `tauri dev` is left alone
  server: { port: 4179, strictPort: false },
  // found up front: a dependency discovered while a page loads makes Vite reload it
  optimizeDeps: { entries: ['index.html', 'screenshots/mock.js'] },
});
let browser;
try {
  await mkdir(outDir, { recursive: true });
  await server.listen();
  browser = await chromium.launch();
  for (const shot of SHOTS) await capture(browser, server.resolvedUrls.local[0], shot);
} finally {
  await browser?.close();
  await server.close();
}
