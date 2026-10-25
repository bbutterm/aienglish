// Рендер промо-ролика: кадры из promo.html (Playwright/Chromium) → MP4 (ffmpeg, H.264, без звука).
//
//   node capture.js                       оба формата, бот YOUR_BOT
//   node capture.js --bot my_buddy_bot    подставить юзернейм в финальную плашку
//   node capture.js --format square       только 1080×1080 (vertical — только 9:16)
//   node capture.js --stills 2.9,10.9     только PNG-кадры для проверки (в out/stills)
//   node capture.js --samples 1           быстрее, без размытия движения
//
// Размытие движения: каждый кадр 30 fps собирается из N подкадров (затвор 180°), ffmpeg их усредняет.
const { chromium } = require('playwright');
const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const arg = (name, def) => { const i = process.argv.indexOf('--' + name); return i > 0 ? process.argv[i + 1] : def; };
const FORMATS = { vertical: { w: 1080, h: 1920, out: 'promo_9x16.mp4', poster: 'promo_poster_9x16.png' }, square: { w: 1080, h: 1080, out: 'promo_1x1.mp4', poster: 'promo_poster_1x1.png' } };
const which = arg('format', 'both');
const BOT = arg('bot', 'YOUR_BOT');
const FPS = +arg('fps', 30);
const SAMPLES = Math.max(1, +arg('samples', 5));
const SHUTTER = 0.5;            // доля кадра, когда «открыт затвор»
const CRF = arg('crf', '20');
const WORKERS = Math.max(1, +arg('workers', Math.min(4, os.cpus().length)));
const POSTER_T = +arg('poster', 2.95);
const stills = arg('stills', null);
const OUT = path.join(__dirname, 'out');

async function openPage(browser, fmt) {
  const page = await browser.newPage({ viewport: { width: fmt.w, height: fmt.h }, deviceScaleFactor: 1 });
  const url = 'file://' + path.join(__dirname, 'promo.html') + `?capture=1&format=${fmt.name}&bot=${encodeURIComponent(BOT)}`;
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.evaluate(() => window.__init);
  return page;
}

async function renderFormat(browser, name) {
  const fmt = { ...FORMATS[name], name };
  fs.mkdirSync(OUT, { recursive: true });

  if (stills) {
    const dir = path.join(OUT, 'stills'); fs.mkdirSync(dir, { recursive: true });
    const page = await openPage(browser, fmt);
    for (const s of stills.split(',').map(Number)) {
      await page.evaluate((t) => window.renderAt(t), s);
      await page.screenshot({ path: path.join(dir, `${name}_${s.toFixed(2)}.png`) });
    }
    await page.close();
    console.log(`${name}: stills → ${dir}`);
    return;
  }

  const dur = await (await openPage(browser, fmt)).evaluate(() => window.DUR);
  const frames = Math.round(dur * FPS);
  const total = frames * SAMPLES;
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ebpromo-'));
  console.log(`${name}: ${frames} кадров × ${SAMPLES} подкадров → ${tmp}`);
  const t0 = Date.now();

  // подкадры раскидываем по нескольким вкладкам
  const jobs = [];
  for (let w = 0; w < WORKERS; w++) {
    jobs.push((async () => {
      const page = await openPage(browser, fmt);
      for (let n = w; n < total; n += WORKERS) {
        const i = Math.floor(n / SAMPLES), k = n % SAMPLES;
        const off = SAMPLES === 1 ? 0 : (k / (SAMPLES - 1) - .5) * SHUTTER / FPS;
        const t = Math.min(dur - 1e-4, Math.max(0, i / FPS + off));
        await page.evaluate((tt) => window.renderAt(tt), t);
        await page.screenshot({ path: path.join(tmp, `f${String(n).padStart(6, '0')}.jpg`), type: 'jpeg', quality: 94 });
        if (w === 0 && i % 60 === 0 && k === 0) process.stdout.write(`  ${(i / FPS).toFixed(1)}s `);
      }
      await page.close();
    })());
  }
  await Promise.all(jobs);
  console.log(`\n  кадры готовы за ${((Date.now() - t0) / 1000).toFixed(0)} с`);

  // постер — чёткий кадр без размытия
  const page = await openPage(browser, fmt);
  await page.evaluate((t) => window.renderAt(t), POSTER_T);
  await page.screenshot({ path: path.join(OUT, fmt.poster) });
  await page.close();

  const vf = SAMPLES > 1
    ? `tmix=frames=${SAMPLES},select='not(mod(n+1\\,${SAMPLES}))',setpts=N/${FPS}/TB,format=yuv420p`
    : 'format=yuv420p';
  const outFile = path.join(OUT, fmt.out);
  const ff = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(FPS * SAMPLES), '-i', path.join(tmp, 'f%06d.jpg'),
    '-vf', vf, '-r', String(FPS), '-c:v', 'libx264', '-preset', 'slow', '-crf', CRF, '-tune', 'animation',
    '-profile:v', 'high', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an', outFile], { stdio: 'inherit' });
  if (ff.status !== 0) throw new Error('ffmpeg failed');
  fs.rmSync(tmp, { recursive: true, force: true });
  const mb = (fs.statSync(outFile).size / 1048576).toFixed(2);
  console.log(`  → ${outFile} (${mb} MB)`);
}

(async () => {
  const browser = await chromium.launch();
  const list = which === 'both' ? ['vertical', 'square'] : [which];
  for (const f of list) await renderFormat(browser, f);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
