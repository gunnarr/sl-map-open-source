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
async function openMap(browser, vehicleQueue, opts = {}) {
  const page = await browser.newPage();
  await page.setViewport(opts.viewport || { width: 1200, height: 900 });
  // Leaflet's map object, for tests that need to move the map or read its centre.
  await page.evaluateOnNewDocument(() => {
    let _L;
    Object.defineProperty(window, 'L', { configurable: true, get() { return _L; },
      set(v) { _L = v; try { v.Map.addInitHook(function () { window.__mapForTest = this; }); } catch (e) {} } });
  });
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
    if (url.split('?')[0].endsWith('/map-under-test')) return local('map.html', 'text/html');
    if (url.includes('/api/vehicles')) {
      calls.vehicles++;
      // `silent` on the page handle: the server stops answering altogether.
      if (page.__silent) return;
      inFlight++;
      if (inFlight > 1) calls.overlapping++;
      // Tests that only need a steady feed pass `opts.steady`; a new timestamp
      // each time, so every answer is applied.
      const next = vehicleQueue.shift() || (opts.steady ? { body: opts.steady(calls.vehicles) } : undefined);
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

  await page.goto('https://map.test/map-under-test' + (opts.query || ''), { waitUntil: 'domcontentloaded' });
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

  // --- Dots sit on their position; popups open clear of the bars ------------
  // Real clock time as the feed timestamp, or the page (rightly) says the data is old.
  const feedNow = () => Math.floor(Date.now() / 1000);
  const steady = (extra) => () => ({ timestamp: feedNow(), vehicles: [
    vehicle('A', 59.3300), vehicle('B', 59.3590, { lon: 18.071 }),
    vehicle('BUS', 59.3250, { line: '4', transport_mode: 'BUS', metro_group: undefined, lon: 18.08 }), ...(extra || [])] });
  {
    const { page } = await openMap(browser, [], { steady: steady(), viewport: { width: 390, height: 844, isMobile: true, hasTouch: true } });
    await wait(2500);
    const off = await page.evaluate(() => [...document.querySelectorAll('.leaflet-marker-icon[data-vid]')].map(e => {
      const a = e.getBoundingClientRect(), b = e.querySelector('.veh-body').getBoundingClientRect();
      return Math.abs((b.y + b.height / 2) - (a.y + a.height / 2)) + Math.abs((b.x + b.width / 2) - (a.x + a.width / 2));
    }));
    check('dots: every dot is centred on its position at the start zoom', off.length >= 3 && off.every(d => d < 0.6),
          `offsets=${off.map(d => d.toFixed(1)).join(',')}`);
    // B is drawn just below the toolbar; its popup must not open underneath it.
    const tb = await page.evaluate(() => document.getElementById('toolbar').getBoundingClientRect().bottom);
    const by = await page.evaluate(() => { const r = document.querySelector('[data-vid="B"]').getBoundingClientRect(); return r.top; });
    await page.evaluate(() => document.querySelector('[data-vid="B"]').click());
    await wait(900);
    const r = await page.evaluate(() => {
      const p = document.querySelector('.leaflet-popup').getBoundingClientRect(), s = document.querySelector('.status-bar').getBoundingClientRect();
      return { top: p.top, bottom: p.bottom, status: s.top };
    });
    check('popup: opens below the toolbar and above the status bar', by < tb + 120 && r.top >= tb && r.bottom <= r.status,
          `marker at ${Math.round(by)}, toolbar ends ${Math.round(tb)}, popup ${Math.round(r.top)}–${Math.round(r.bottom)}`);
    await page.close();
  }

  // --- An open popup follows its vehicle until the visitor drags the map -----
  {
    let lat = 59.3300;
    const { page } = await openMap(browser, [], { query: '?lat=59.3305&lon=18.06&zoom=16',
      steady: () => ({ timestamp: feedNow(), vehicles: [vehicle('A', (lat += 0.00025))] }) });   // ~28 m north per answer
    await wait(2500);
    await page.evaluate(() => document.querySelector('[data-vid="A"]').click());
    await wait(800);
    const centre = () => page.evaluate(() => window.__mapForTest.getCenter().lat);
    const c0 = await centre();
    // Drag upwards, far enough to put the popup above the top of the window.
    await page.mouse.move(900, 800); await page.mouse.down();
    for (let i = 1; i <= 9; i++) { await page.mouse.move(900, 800 - i * 50); await wait(15); }
    await wait(120); await page.mouse.up(); await wait(1200);
    const c1 = await centre();
    await wait(8000);                         // the train moves on, four answers
    const c2 = await centre();
    const open = await page.evaluate(() => !!document.querySelector('.leaflet-popup'));
    const m = (a, b) => Math.round(Math.abs(a - b) * 111320);
    const gone = await page.evaluate(() => document.querySelector('.leaflet-popup').getBoundingClientRect().bottom < 54);
    check('popup: after a drag the open popup no longer pulls the map along', m(c0, c1) > 200 && m(c1, c2) < 5 && open && gone,
          `drag moved ${m(c0, c1)} m, then ${m(c1, c2)} m in 8 s, popup open=${open}`);
    // Closed and opened again, it is brought into view as before.
    await page.evaluate(() => window.__mapForTest.closePopup());
    await wait(400);
    await page.evaluate(() => { const A = Object.values(window.__mapForTest._layers).find(l => l._icon && l._icon.dataset.vid === 'A'); A.openPopup(); });
    await wait(1500);
    const shown = await page.evaluate(() => { const r = document.querySelector('.leaflet-popup'); if (!r) return null; const b = r.getBoundingClientRect(); return b.top >= 54 && b.bottom <= innerHeight; });
    check('popup: opened again, it is brought back into view', shown === true, `in view=${shown}`);
    await page.evaluate(() => localStorage.clear());   // the drag stored a view; the next block starts clean
    await page.close();
  }

  // --- The map opens where it was left, with the layers that were on -------
  {
    let { page } = await openMap(browser, [], { steady: steady() });
    await wait(1500);
    const first = await page.evaluate(() => ({ z: window.__mapForTest.getZoom(), stored: localStorage.getItem('view') }));
    await page.evaluate(() => window.__mapForTest.setView([59.402, 17.948], 15));
    await wait(1500);
    await page.evaluate(() => document.querySelector('#toolbar-chips .toolbar-chip[data-layer="bus"]').click());
    await wait(300);
    await page.close();
    ({ page } = await openMap(browser, [], { steady: steady() }));
    await wait(1500);
    const again = await page.evaluate(() => { const c = window.__mapForTest.getCenter(); return {
      lat: +c.lat.toFixed(3), lon: +c.lng.toFixed(3), z: window.__mapForTest.getZoom(),
      bus: document.querySelector('#toolbar-chips .toolbar-chip[data-layer="bus"]').getAttribute('aria-pressed'),
      mark: document.getElementById('toolbar-menu').classList.contains('has-filter'),
      preloads: document.querySelectorAll('link[rel=preload][as=image]').length }; });
    check('view: first visit shows the default view and stores nothing', first.z === 13 && first.stored === null, JSON.stringify(first));
    check('view: next visit opens where the map was left', again.lat === 59.402 && again.lon === 17.948 && again.z === 15, JSON.stringify(again));
    check('view: layers are remembered and the menu button says so', again.bus === 'false' && again.mark === true);
    check('view: the default view\'s tiles are not preloaded then', again.preloads === 0);
    await page.close();
    ({ page } = await openMap(browser, [], { steady: steady(), query: '?lat=59.3&lon=18.1&zoom=12' }));
    await wait(1200);
    const linked = await page.evaluate(() => { const c = window.__mapForTest.getCenter(); return [+c.lat.toFixed(2), +c.lng.toFixed(2), window.__mapForTest.getZoom()]; });
    check('view: a link with a position wins over the stored view', linked.join() === '59.3,18.1,12', linked.join());
    await page.close();
    ({ page } = await openMap(browser, [], { steady: steady(), query: '?screensaver' }));
    await wait(1200);
    const ss = await page.evaluate(() => window.__mapForTest.getZoom());
    check('view: screensaver mode ignores the stored view', ss === 13, `zoom=${ss}`);
    await page.evaluate(() => localStorage.clear());
    await page.close();
  }

  // --- Lost contact is said out loud, and a silent server cannot block ------
  {
    const { page, calls } = await openMap(browser, [], { steady: steady() });
    await wait(3000);
    const calm = await page.evaluate(() => !document.getElementById('stale-banner'));
    check('contact: no banner while answers arrive', calm);
    page.__silent = true;                     // requests now hang for ever
    const before = calls.vehicles;
    await wait(13000);
    const banner = await page.evaluate(() => (document.getElementById('stale-banner') || {}).textContent || '');
    check('contact: a banner appears when nothing has come back for ten seconds', /Ingen kontakt med servern/.test(banner), `banner="${banner}"`);
    check('contact: a request that never answers does not block the next one', calls.vehicles - before >= 2, `requests while silent=${calls.vehicles - before}`);
    page.__silent = false;
    await wait(11000);                        // the hung request times out, the next one answers
    const after = await page.evaluate(() => ({ banner: !!document.getElementById('stale-banner'), foot: document.getElementById('last-update').textContent }));
    check('contact: the banner goes away when answers come back', !after.banner && !after.foot.startsWith('Fel'), JSON.stringify(after));
    await page.close();
  }

  // --- Coming back from the background: vehicles are placed, not glided -----
  {
    let far = false;
    // 111 m further on: far enough to see, near enough for the speed check to accept.
    const { page } = await openMap(browser, [], { query: '?lat=59.3305&lon=18.06&zoom=16',
      steady: () => ({ timestamp: feedNow(), vehicles: [vehicle('A', far ? 59.3310 : 59.3300)] }) });
    await wait(2500);
    const y0 = await page.evaluate(() => document.querySelector('[data-vid="A"]').getBoundingClientRect().top);
    await page.evaluate(() => { window.__hid = true; Object.defineProperty(document, 'hidden', { get: () => window.__hid, configurable: true }); document.dispatchEvent(new Event('visibilitychange')); });
    far = true;                               // the train moves on while the page is hidden
    await wait(4000);
    await page.evaluate(() => { window.__hid = false; document.dispatchEvent(new Event('visibilitychange')); });
    await wait(700);                          // a glide would have covered 7 % of the way by now
    const y1 = await page.evaluate(() => document.querySelector('[data-vid="A"]').getBoundingClientRect().top);
    await wait(4000);
    const y2 = await page.evaluate(() => document.querySelector('[data-vid="A"]').getBoundingClientRect().top);
    check('return: the vehicle is at its new position at once', Math.abs(y0 - y2) > 60 && Math.abs(y1 - y2) < 3,
          `before ${Math.round(y0)}, 0.7 s after return ${Math.round(y1)}, settled ${Math.round(y2)}`);
    await page.close();
  }

  await browser.close();

  const failed = results.filter(r => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
