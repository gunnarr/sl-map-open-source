// Site configuration — edit this file for your own deployment.
//
// map.html loads it synchronously at the top of <head> and keeps no
// deployment-specific values of its own: basemap tiles, analytics and the iOS
// app banner are all decided here. The file is plain data; map.html does the
// work.
//
// If you put a CDN in front of the site, remember that it may cache this file.
window.SITE = {
  // Basemap tiles. Each theme is a Leaflet tileLayer: `url` is the template,
  // every other key is passed to L.tileLayer as an option. If you only define
  // `light`, it is used in dark mode too.
  //
  // Default: Stadia Maps "Alidade Smooth" (light) and "Alidade Smooth Dark".
  // Works on localhost without an account (rate limited). For a public site,
  // create a free account at https://client.stadiamaps.com/ and add your domain
  // under "Authentication Configuration" — no key in the URL is needed then.
  // Check Stadia's terms for commercial use.
  tiles: {
    light: {
      url: 'https://tiles.stadiamaps.com/tiles/alidade_smooth/{z}/{x}/{y}{r}.png',
      attribution: '&copy; <a href="https://stadiamaps.com/" target="_blank" rel="noopener">Stadia Maps</a> &copy; <a href="https://openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
      maxZoom: 20,
    },
    dark: {
      url: 'https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png',
      attribution: '&copy; <a href="https://stadiamaps.com/" target="_blank" rel="noopener">Stadia Maps</a> &copy; <a href="https://openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> &copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
      maxZoom: 20,
    },
    // Tiles fetched before Leaflet has downloaded, as [z, x, y]: the four
    // centre tiles of the default view (zoom 13 over central Stockholm).
    // Change them if you move the default map centre, or use [] for none.
    preload: [[13, 4507, 2409], [13, 4506, 2408], [13, 4507, 2408], [13, 4506, 2409]],
  },

  // --- Other tile presets (replace `tiles.light` / `tiles.dark` above) -----
  //
  // CARTO Positron / Dark Matter — needs a free key from
  // https://carto.com/basemaps/ (non-commercial use):
  //
  //   light: {
  //     url: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png?key=YOUR_CARTO_KEY',
  //     attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  //     subdomains: 'abcd',
  //     maxZoom: 19,
  //   },
  //   dark: {
  //     url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png?key=YOUR_CARTO_KEY',
  //     attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  //     subdomains: 'abcd',
  //     maxZoom: 19,
  //   },
  //
  // OpenStreetMap standard tiles — light only, no key. Fine for trying things
  // out; read https://operations.osmfoundation.org/policies/tiles/ before
  // using it for a site with real traffic (heavy use is not allowed):
  //
  //   light: {
  //     url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  //     attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  //     maxZoom: 19,
  //   },
  //   (no `dark` entry — light tiles are used in both themes)
  //
  // OpenFreeMap (https://openfreemap.org/) is free and needs no key, but serves
  // vector tiles only. Using it means rendering with MapLibre GL (for example via
  // the maplibre-gl-leaflet plugin) instead of a plain L.tileLayer, which this
  // map does not do out of the box.

  // Privacy-friendly analytics with Plausible. null = no analytics; the page's
  // plausible('...') event calls are then harmless no-ops. Example:
  //   analytics: { plausible: { src: 'https://plausible.io/js/script.js', api: 'https://plausible.io/api/event' } },
  analytics: null,

  // Safari's Smart App Banner for a native iOS app. null = no banner. Example:
  //   appBanner: { appId: '1234567890' },
  appBanner: null,

  // Links in the menu and footer to pages a deployment may have besides the
  // map: 'about', 'news', 'contact', 'screensaver', 'install' and 'credit'
  // (the footer link to the original author). Those pages are not part of this
  // repo, so all of them are hidden. Add a name here once you have built the
  // page, and point the link in map.html at it.
  pages: [],
};
