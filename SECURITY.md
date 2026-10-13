# Säkerhet / Security

## Svenska

Hittar du ett säkerhetsproblem – till exempel ett sätt att läsa filer som inte ska vara publika, få ut nycklar eller
slå ut servern – **skriv inte om det i ett öppet ärende**. Rapportera det privat via GitHub:
fliken **Security → Report a vulnerability** i det här repot.

Beskriv vad du hittat, hur man återskapar det och vilken version (commit) det gäller. Jag svarar så snart jag kan,
oftast inom en vecka, och berättar när det är rättat. Vill du nämnas när rättningen publiceras gör jag gärna det.

Testa bara mot din egen kopia. Belasta inte sl-map.gunnar.se och försök inte komma åt andras uppgifter.

## English

If you find a security problem – for example a way to read files that shouldn't be public, extract keys or take the
server down – **please don't post it in a public issue**. Report it privately through GitHub: the
**Security → Report a vulnerability** tab in this repo.

Describe what you found, how to reproduce it and which version (commit) it affects. I'll reply as soon as I can,
usually within a week, and let you know when it's fixed. If you'd like to be credited when the fix is published, I'm
happy to do that.

Only test against your own copy. Don't load-test sl-map.gunnar.se and don't try to access other people's data.

## Scope

The code in this repo: `server.py`, `map.html`, the build scripts and the default configuration. The apps and the
rest of sl-map.gunnar.se are separate projects; report problems with them through the contact form on the site.
