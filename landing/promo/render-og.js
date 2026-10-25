// Рендер OG-картинки и иконки для iOS из og.html.
// Запуск из папки landing/promo:  node render-og.js
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const src = 'file://' + path.join(__dirname, 'og.html');
  const assets = path.join(__dirname, '..', 'assets');
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 630 } });

  await page.goto(src, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.fonts.ready);
  await page.locator('#og').screenshot({ path: path.join(assets, 'og.png') });

  await page.goto(src + '#icon', { waitUntil: 'networkidle' });
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('#icon').screenshot({ path: path.join(assets, 'apple-touch-icon.png') });

  await browser.close();
  console.log('ok: assets/og.png, assets/apple-touch-icon.png');
})();
