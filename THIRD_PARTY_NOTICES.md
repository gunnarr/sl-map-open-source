# Third-party notices

This repo includes or depends on the following third-party software. Their licenses apply to them, not the AGPL-3.0
that covers the rest of the code.

## Included in the repo

### Leaflet 1.9.4

`leaflet.js`, `leaflet.css` and `images/layers.png`, `images/layers-2x.png`, `images/marker-icon.png` –
<https://leafletjs.com>

```
BSD 2-Clause License

Copyright (c) 2010-2023, Volodymyr Agafonkin
Copyright (c) 2010-2011, CloudMade
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Installed by `pip` / `npm` (not included)

| Package | Used for | License |
|---|---|---|
| Flask | web server | BSD-3-Clause |
| Flask-Limiter | rate limiting | MIT |
| gunicorn | production server | MIT |
| requests | calls to Trafiklab | Apache-2.0 |
| gtfs-realtime-bindings | reading GTFS-RT (protobuf) | Apache-2.0 |
| python-dotenv | reading `.env` | BSD-3-Clause |
| pytest (development) | tests | MIT |
| Puppeteer (development) | browser test `map-behaviour-test.mjs` | Apache-2.0 |

## Data and map tiles

- Transit data: Trafiklab / Samtrafiken, CC0 – <https://www.trafiklab.se/api/gtfs-datasets/gtfs-regional/>
- Map data © OpenStreetMap contributors, ODbL – <https://www.openstreetmap.org/copyright>
- Map tiles come from the provider set in `site-config.js` (Stadia Maps by default) and follow that provider's terms.
