# Bidra / Contributing

*[English below](#english)*

Kul att du vill hjälpa till! Jag driver SL Live Map ensam på fritiden, så det kan ta ett tag innan jag svarar.

## Vart projektet är på väg

Det här är **kartan**: fordonen i realtid, så snabb och lätt som möjligt, i webbläsaren och utan konto. Jag bestämmer
vad som kommer med, och jag säger ibland nej till bra idéer för att de inte passar här. Det är inget betyg på idén –
koden är fri, så du kan alltid bygga vidare i en egen fork.

Jag tar gärna emot:

- buggrättningar, särskilt med ett test som visar felet,
- bättre prestanda på mobilen och lägre datatrafik,
- tillgänglighet,
- dokumentation och förbättringar för den som vill köra en egen kopia.

Jag säger troligen nej till:

- byggsteg, ramverk eller stora nya beroenden (sidan ska gå att läsa och ändra som den är),
- inloggning, konton, reklam eller spårning,
- sådant som ökar antalet anrop mot Trafiklab,
- ändringar i API:et (`/api/vehicles`, `/api/*-lines`, `/stations.json`) som bryter mot apparna som använder det,
- stora omskrivningar som ingen har bett om.

Öppna gärna ett ärende och beskriv idén **innan** du lägger mycket tid på en större ändring.

## Så gör du

1. Forka repot och skapa en gren.
2. Kör testerna: `venv/bin/pip install -r requirements-dev.txt && venv/bin/python -m pytest tests/ -q`.
3. Rör du `map.html`, testa i en riktig webbläsare (gärna både mobil och dator, ljust och mörkt läge) och kör
   `node map-behaviour-test.mjs` (kräver `npm install`).
4. Håll ändringen liten och beskriv i pull requesten vad den gör och hur du testade.

Kartans texter är på svenska. Lägg aldrig nycklar, `.env`-filer eller personuppgifter i en commit.

Använder du en AI-assistent går det bra, men du står för koden: läs den, testa den och förklara den i pull requesten.
Se [AGENTS.md](AGENTS.md).

## Licens för bidrag

Projektet är licensierat under [AGPL-3.0](LICENSE). När du skickar in ett bidrag godkänner du att det licensieras
under AGPL-3.0, och dessutom att projektets ägare får använda det under [MIT-licensen](https://opensource.org/license/mit).
Det behövs för att en rättning ska kunna följa med till de delar av SL Live Map som inte är öppna, till exempel
apparna. Du behåller upphovsrätten till ditt bidrag.

## Uppförande

Var vänlig och saklig. Se [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

---

## English

Thanks for wanting to help! I run SL Live Map alone in my spare time, so replies can take a while.

### Where the project is going

This is **the map**: vehicles in real time, as fast and light as possible, in the browser and without an account. I
decide what goes in, and I sometimes say no to good ideas because they don't fit here. That's no verdict on the idea –
the code is free, so you can always build on it in your own fork.

Welcome:

- bug fixes, especially with a test that shows the bug,
- better performance on phones and less data traffic,
- accessibility,
- documentation and improvements for people running their own copy.

Probably declined:

- build steps, frameworks or large new dependencies (the page should stay readable and editable as it is),
- logins, accounts, ads or tracking,
- anything that increases the number of calls to Trafiklab,
- changes to the API (`/api/vehicles`, `/api/*-lines`, `/stations.json`) that break the apps using it,
- large rewrites nobody asked for.

Please open an issue and describe the idea **before** you spend a lot of time on a bigger change.

### How to

1. Fork the repo and create a branch.
2. Run the tests: `venv/bin/pip install -r requirements-dev.txt && venv/bin/python -m pytest tests/ -q`.
3. If you touch `map.html`, test in a real browser (phone and desktop, light and dark mode if you can) and run
   `node map-behaviour-test.mjs` (needs `npm install`).
4. Keep the change small and say in the pull request what it does and how you tested it.

The map's texts are in Swedish. Never commit keys, `.env` files or personal data.

Using an AI assistant is fine, but you own the code: read it, test it and explain it in the pull request. See
[AGENTS.md](AGENTS.md).

### License of contributions

The project is licensed under [AGPL-3.0](LICENSE). By submitting a contribution you agree that it is licensed under
AGPL-3.0, and also that the project owner may use it under the [MIT License](https://opensource.org/license/mit). That
lets a fix travel to the parts of SL Live Map that are not open, such as the apps. You keep the copyright to your
contribution.

### Conduct

Be kind and stay on topic. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
