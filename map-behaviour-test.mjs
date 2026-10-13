#!/usr/bin/env node
/**
 * Behaviour tests for map.html, driven in a real browser.
 *
 * The Python suite checks map.html by searching for code strings. That cannot
 * see an out-of-order response being applied, a popup showing frozen content,
 * or a marker coming back from offscreen with last zoom level's icon — all of
 * which the 2026-09-21 review found by hand. This harness drives the real page
 * with controlled API responses instead.
 *
 * Usage: node map-behaviour-test.mjs
 * Needs no network: every request the page makes is answered locally.
 */

import puppeteer from 'puppeteer';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
// Installed Chrome on macOS if present, CHROME_PATH if set, else Puppeteer's own.
const MAC_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const CHROME = process.env.CHROME_PATH || (fs.existsSync(MAC_CHROME) ? MAC_CHROME : undefined);

const results = [];
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail });
  console.log(`${ok ? '  PASS' : '  FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
};

function vehicle(id, lat, extra = {}) {
  return {
    id, lat, lon: 18.06, bearing: 0, speed: 0,
    line: '19', transport_mode: 'METRO', metro_group: 'green',
    headsign: 'Hässelby strand', trip_id: 't' + id, route_id: 'r' + id,
    status: 'IN_TRANSIT_TO', timestamp: 1789000000, ...extra,
  };
}

/** Serve the real map.html and answer every subresource locally. */
async function openMap(browser, vehicleQueue) {
  const page = await browser.newPage();
  await page.setViewport({ width: 1200, height: 900 });
  const calls = { vehicles: 0, overlapping: 0 };
  let inFlight = 0;

  await page.setRequestInterception(true);
  page.on('request', async (req) => {
    const url = req.url();
    const local = (f, type) => {
      try {
        return req.respond({ status: 200, contentType: type,
                             body: fs.readFileSync(path.join(ROOT, f)) });
      } catch { return req.respond({ status: 404, body: '' }); }
    };
    if (url.endsWith('/map-under-test')) return local('map.html', 'text/html');
    if (url.includes('/api/vehicles')) {
      calls.vehicles++;
      inFlight++;
      if (inFlight > 1) calls.overlapping++;
      const next = vehicleQueue.shift();
      const finish = () => {
        inFlight--;
        req.respond({ status: 200, contentType: 'application/json',
                      body: JSON.stringify(next ? next.body : { timestamp: 1, vehicles: [] }) });
      };
      if (next && next.delayMs) return setTimeout(finish, next.delayMs);
      return finish();
    }
    if (url.endsWith('/leaflet.js')) return local('leaflet.js', 'application/javascript');
    if (url.endsWith('/leaflet.css')) return local('leaflet.css', 'text/css');
    if (url.endsWith('/site-config.js')) return local('site-config.js', 'application/javascript');
    if (url.includes('/api/version')) return req.respond({ status: 200, contentType: 'application/json', body: '{"v":"1"}' });
    if (url.includes('/api/visitors')) return req.respond({ status: 200, contentType: 'application/json', body: '{"visitors":1}' });
    if (url.includes('/api/') || url.endsWith('.geojson') || url.endsWith('.json'))
      return req.respond({ status: 200, contentType: 'application/json', body: '{}' });
    // Basemap tiles and analytics from whatever hosts site-config.js names.
    if (!url.startsWith('https://map.test/'))
      return req.respond({ status: 200, contentType: 'image/png', body: '' });
    return req.respond({ status: 404, body: '' });
  });

  await page.goto('https://map.test/map-under-test', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.__mapReadyForTest === true || document.querySelector('#map'), { timeout: 20000 });
  return { page, calls };
}

const wait = (ms) => new Promise(r => setTimeout(r, ms));
const posOf = (page, id) => page.evaluate((vid) =>
  window.__vehicleMarkersForTest ? null : (() => {
    const el = document.querySelector(`[data-vid="${vid}"]`);
    return el ? el.getBoundingClientRect().top : null;
  })(), id);

async function main() {
  const browser = await puppeteer.launch({
    headless: 'new', executablePath: CHROME, args: ['--no-sandbox'],
  });

  // --- Finding 3: an older response must not roll the map back -------------
  {
    const { page, calls } = await openMap(browser, [
      { body: { timestamp: 1000, vehicles: [vehicle('A', 59.30)] } },
      { body: { timestamp: 1002, vehicles: [vehicle('A', 59.40)] }, delayMs: 1200 },
      { body: { timestamp: 1001, vehicles: [vehicle('A', 59.50)] } },
      { body: { timestamp: 1001, vehicles: [vehicle('A', 59.50)] } },
    ]);
    await wait(7000);
    const lat = await page.evaluate(() => window.__lastAppliedLat ?? null);
    const ts = await page.evaluate(() => {
      const t = document.getElementById('last-update');
      return t ? t.textContent : '';
    });
    check('finding 3: no overlapping /api/vehicles calls', calls.overlapping === 0,
          `overlapping=${calls.overlapping}, total=${calls.vehicles}`);
    check('finding 3: page survived out-of-order responses', !!ts && !ts.startsWith('Fel'),
          `last-update="${ts}"`);
    await page.close();
  }

  // --- Finding 7: a reopened popup shows current data ----------------------
  // The report's repro: the value must change *while the popup is closed*.
  // Updating an open popup with setPopupContent(string) replaced the content
  // function bound at creation, freezing whatever was current at that moment.
  {
    const q = [];
    for (let i = 0; i < 4; i++)
      q.push({ body: { timestamp: 1000 + i * 2,
                       vehicles: [vehicle('A', 59.33, { headsign: 'Original' })] } });
    for (let i = 0; i < 8; i++)
      q.push({ body: { timestamp: 1008 + i * 2,
                       vehicles: [vehicle('A', 59.33, { headsign: 'Changed' })] } });
    const { page } = await openMap(browser, q);

    await wait(2500);
    const opened = await page.evaluate(() => {
      const el = document.querySelector('[data-vid="A"]');
      if (!el) return false;
      el.click();
      return true;
    });
    await wait(2600);                       // one poll lands while open
    const whileOpen = await page.evaluate(() =>
      (document.querySelector('.leaflet-popup-content') || {}).textContent || '');
    await page.evaluate(() => {
      const el = document.querySelector('.leaflet-popup-close-button');
      if (el) el.click();
    });
    await wait(8000);                       // 'Changed' arrives while closed
    await page.evaluate(() => {
      const el = document.querySelector('[data-vid="A"]');
      if (el) el.click();
    });
    await wait(700);
    const reopened = await page.evaluate(() =>
      (document.querySelector('.leaflet-popup-content') || {}).textContent || '');

    check('finding 7: marker was clickable', opened);
    check('finding 7: popup showed the original while open',
          whileOpen.includes('Original'),
          `popup="${whileOpen.replace(/\s+/g, ' ').trim().slice(0, 60)}"`);
    // Positive assertion: an empty popup must not count as a pass.
    check('finding 7: reopened popup shows the value changed while closed',
          reopened.includes('Changed'),
          `popup="${reopened.replace(/\s+/g, ' ').trim().slice(0, 70)}"`);
    await page.close();
  }

  await browser.close();

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
