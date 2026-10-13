# SL Live Map – öppen källkod

*[English](README.en.md)*

En realtidskarta över alla bussar, tunnelbanetåg, pendeltåg, spårvagnar och båtar i Stockholms kollektivtrafik. Jag
har byggt den och driver den som ett enmansprojekt på **[sl-map.gunnar.se](https://sl-map.gunnar.se)**. Det här repot
är backenden och webbkartan som driver sajten.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/readme-dark.png">
  <img src="images/readme-light.png" alt="Kartan över Stockholm med tunnelbana, pendeltåg, spårvagnar, bussar och båtar i realtid" width="1280">
</picture>

Flask hämtar Trafiklabs GTFS-RT-flöde (protobuf) och gör om det till kompakt JSON. En sida i vanilla JavaScript och
Leaflet hämtar fordonen varannan sekund och låter dem glida längs spåren. Inget byggsteg, inget ramverk.

## Kom igång

Du behöver Python 3 (testat med 3.13 och 3.14) och två gratisnycklar från Trafiklab (se nedan).

```bash
git clone https://github.com/gunnarr/sl-map-open-source.git
cd sl-map-open-source
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env            # fyll i dina Trafiklab-nycklar

# Ladda ner tidtabellsdatan en gång och bygg filerna kartan behöver (hamnar i regions/sl/)
venv/bin/python3 build_trip_lookup.py --save-zip sl.zip    # krävs: linjenummer och destinationer
venv/bin/python3 build_stations.py --gtfs-zip sl.zip       # stationer
venv/bin/python3 build_geojson.py --gtfs-zip sl.zip        # spårlinjer

venv/bin/python3 server.py      # öppna http://localhost:8080
```

Utan `trip_lookup.json` vet servern inte vilken linje fordonen kör, och kartan blir tom.

Testerna behöver inga nycklar och inget nätverk:

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/ -q
npm install && node map-behaviour-test.mjs    # webbläsartest av kartan (kräver Chrome, se AGENTS.md)
```

## Med en AI-agent

Det går utmärkt att utforska och ändra projektet med en AI-agent som Claude Code, Codex, Cursor, Copilot eller Gemini.
Instruktionerna till agenten finns i [AGENTS.md](AGENTS.md) (`CLAUDE.md` och `GEMINI.md` pekar dit), så de flesta
verktyg läser dem av sig själva. Du kan börja med till exempel:

> Läs AGENTS.md och README.md. Sätt upp projektet lokalt, kör testerna och förklara hur kartan hämtar och visar
> fordonen. Ändra ingenting än.

Tre saker som är bra att veta:

- **Nycklarna skaffar du själv** och skriver in i `.env` – klistra inte in dem i chatten med agenten. Agenten kan
  köra testerna och starta servern utan nycklar; det är bara de riktiga fordonen som kräver dem.
- **Agenten kan förbruka din kvot.** Låt `VEHICLE_CACHE_SECONDS=60` stå kvar medan du utvecklar och be agenten att
  inte hämta tidtabellsdatan mer än en gång.
- **Du står för koden** när du skickar en pull request: läs den, kör den och skriv att en agent hjälpt till.

## Skaffa nycklar hos Trafiklab (gratis)

1. Skapa ett konto på [developer.trafiklab.se](https://developer.trafiklab.se/register) (det går att logga in med
   GitHub) och bekräfta din e-post.
2. Skapa ett projekt med **New project**.
3. Lägg till två API:er i projektet:
   - **GTFS Regional Realtime** – nyckeln går i `TRAFIKLAB_KEY` (fordonens positioner och förseningar).
   - **GTFS Regional Static** – nyckeln går i `GTFS_STATIC_KEY` (används bara av byggskripten).
4. Nycklarna fungerar direkt på nivån **Bronze**, som räcker för att prova och utveckla.

**Tänk på kvoterna.** Bronze ger 30 000 realtidsanrop i månaden. Med standardinställningen (varannan sekund) räcker det
bara drygt 16 timmar med kartan öppen, så sätt `VEHICLE_CACHE_SECONDS=60` medan du utvecklar. Statisk data ger 50
nedladdningar i månaden – ladda ner en gång och återanvänd filen med `--gtfs-zip`. Ska du köra en publik sajt söker du
**Silver** via *Upgrade quota* hos Trafiklab. Det är gratis och brukar godkännas inom ett par arbetsdagar.

Lägg aldrig `.env` eller nycklar i Git.

## Så skonar du Trafiklab: cachen är hela poängen

Besökarnas webbläsare pratar **aldrig** med Trafiklab. De pratar bara med din server, och servern hämtar
fordonspositionerna från Trafiklab **en gång varannan sekund** och sparar svaret. Alla besökare får det senaste
sparade svaret – oavsett om det är en eller tiotusen som tittar. Antalet anrop mot Trafiklab beror alltså på
klockan, inte på hur många som besöker sajten.

Det här är viktigt av tre skäl:

- **Kvoten räcker.** En server som hämtar varannan sekund gör cirka 43 000 anrop per dygn, hur populär sajten än blir.
  Utan cache skulle varje besökare göra egna anrop, och kvoten vore slut på minuter.
- **Nyckeln stannar hemlig.** Den finns bara på servern. Bygg aldrig om kartan så att webbläsaren hämtar direkt
  från Trafiklab – då hamnar nyckeln i sidkoden och vem som helst kan använda den.
- **Snabbare och stabilare.** Om Trafiklab svarar långsamt eller tillfälligt inte alls, får besökarna det senast
  sparade svaret i stället för ett fel (*stale-while-revalidate*).

Två saker att tänka på när du driftsätter:

- **Varje serverprocess har sin egen cache.** Kör du gunicorn med fyra workers hämtar var och en för sig, alltså
  fyra gånger så många anrop. Lägg därför en kort cache i nginx framför `/api/vehicles` så att bara ett anrop i
  taget går vidare till appen. Så här gör jag på sl-map.gunnar.se:

  ```nginx
  proxy_cache_path /var/cache/nginx/sl levels=1 keys_zone=sl_vehicles:1m max_size=10m inactive=10s;

  location /api/vehicles {
      proxy_cache sl_vehicles;
      proxy_cache_valid 200 2s;
      proxy_cache_key "$request_uri";
      proxy_cache_lock on;                 # bara en förfrågan i taget fyller cachen
      proxy_cache_use_stale error timeout updating http_500 http_502 http_503;
      proxy_pass http://127.0.0.1:8080;
  }
  ```

- **Webbläsaren hämtar bara ändringar.** Efter första svaret frågar kartan `/api/vehicles?since=…` och får bara de
  fordon som flyttat sig, kommit till eller försvunnit. Det sparar ungefär 40 % trafik mellan server och besökare.

Under utveckling kan du sätta `VEHICLE_CACHE_SECONDS=60` så att servern bara hämtar en gång i minuten.

## Kör din egen kopia

Allt som skiljer en sajt från en annan ligger på några få ställen:

- **`site-config.js`** – kartbilder, statistik (Plausible), appbanner för iPhone och vilka länkar som visas i menyn.
  Kommentarerna i filen förklarar varje del.
- **`.env`** – nycklar, `SITE_DOMAIN` (din domän, används för CORS) och `VEHICLE_CACHE_SECONDS`.
- **`map.html`** – överst i `<head>` står delningstaggarna (Open Graph), `canonical` och strukturerad data (JSON-LD)
  med sl-map.gunnar.se som adress. De måste stå direkt i HTML:en eftersom delningsrobotar inte kör JavaScript. Byt dem
  mot din egen adress, eller ta bort dem.
- **`manifest.json`** och ikonerna – namn och ikon när kartan läggs till på hemskärmen.

## Kartbilder

Som standard använder kartan [Stadia Maps](https://stadiamaps.com), som fungerar på localhost utan nyckel. I
produktion registrerar du din domän hos Stadia, eller väljer ett annat förval i `site-config.js`: CARTO (med egen
nyckel) eller OpenStreetMap.

## Andra städer

Backenden kan köra andra trafikområden som finns i GTFS Regional. Kopiera `regions/sl/` till `regions/<operatör>/`,
anpassa `config.json` (namn, linjefärger, tunnelbanegrupper) och sätt `REGION` i `.env`. Byggskripten tar
`--operator <operatör>`. Själva kartsidan är byggd för Stockholm (startläge, texter), så en annan stad behöver också en
egen `map.html` i `regions/<operatör>/` – servern tar den före den i roten.

## Det som inte finns här

Apparna för iPhone, iPad och Apple TV (och tjänsten bakom Live Activities) är separata projekt och ingår inte. Det gör
inte heller sajtens övriga sidor, mina deployskript, min statistik eller mitt nyhetsbrev. Frågor om apparna ställer du
via kontaktformuläret på [sl-map.gunnar.se](https://sl-map.gunnar.se).

## Bidra

Jag är en person, så det kan ta ett tag innan jag svarar. Läs [CONTRIBUTING.md](CONTRIBUTING.md) och öppna gärna ett
ärende innan större ändringar. Säkerhetsproblem rapporterar du enligt [SECURITY.md](SECURITY.md).

## Licens och tack

- Koden: [AGPL-3.0](LICENSE). Kör du en ändrad version som en publik webbtjänst ska besökarna kunna få källkoden.
- Trafikdata: Trafiklab / Samtrafiken, [CC0](https://www.trafiklab.se/api/gtfs-datasets/gtfs-regional/).
- Kartdata © [OpenStreetMaps bidragsgivare](https://www.openstreetmap.org/copyright) (ODbL). Kartbilder från den
  leverantör du väljer.
- [Leaflet](https://leafletjs.com), BSD-2-Clause. Se [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Projektet är fristående och har ingen koppling till SL, Region Stockholm, Samtrafiken eller Trafiklab. Namnet och
ikonerna omfattas inte av kodlicensen.
