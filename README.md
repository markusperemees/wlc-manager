# Avaliku Wi-Fi automaatika skript

Skript, mis iga teatud ajatagant muudab parooli ning töövälisel ajal lülitab Wi-Fi välja WLC-s.




Parool genereeritakse kasutatakse suffixi, sõnaraamatud ja juhuslikku kolmekordset numbrit, näiteks kui meie suffix on "markus" siis genereeritakse parool kujule "markus483pirn".

Parool salvestatakse andmebaasi SQLite.

Andmebaasis hoiame viimaseid 12 WLC salasõna mittekrüpteeritud kujul kuna tegu on Avaliku Wi-Fi-ga, kui uue salasõna genereerime siis kontrollime ennem et seda salasõna pole kasutatud (kontrollime äkki ainult viimast sõna salasõnast mis on sõnaraamatust valitud, mitte suffixi ja numbrit). Kui salasõna on genereeritud siis kasutame svg template ja CairoSVG teeki, et panna see SVG failile, teha sellest PDF ja PNG ja siis saadame need vastutavatele isikutele kasutades smtp microsoft vist?

WLC-sse ssh jaoks kasutame Netmiko teeki, tegu on vana Cisco AireOS WLC-ga.

Kõik tegevused sama run id alla, logimine peaks toimuma Graylogi.

Rakenduse või skripti arhitektuur peaks olema modulaarne, kõik muutujad mis võimalik paneme muutujatesse .yaml faili, saladused tulevad Hashicorp Vaultist agendi kaudu. Orkestreerija juhib ansamblit. Arhitektuur ikka noh selline et meil on kerge seda hallata ja asju vajadusel välja vahetada või lisada jne.

Meil peaks olema ka CLI käsud, et protsesse käsitsi käivitada jne, kuid ise rakendus peaks nt tunnis korra jooksma ja vaatama mida tegema peaks.

Meie puhul: 3 tööpäeva ennem esimest tööpäeva peaksime genereerima uue parooli, vaatama et see vanaga ei kattuks, kui sellega on korras siis peaks genereerima SVG malli põhjal plakati voi planketi ning saatma selle vastutavatele isikutele. Esimesel kuupäeval, vahet pole kas tegu on pühaga, nädalavahetus või tööpäev peaks muutuma parool wlc-s.

Samuti töövälisel ajal peaks ta Wi-Fi välja lülitama.

Plaanis ehitada Pythonis, andmebaasiks SQLite ja siis Docker. Jooksutatakse Portaineris.

## Arendus

Kinnitatud nõuded ja vastuvõtukriteeriumid asuvad failis
[`TEHNILINE_LAHTEULESANNE.md`](TEHNILINE_LAHTEULESANNE.md).

Arenduskeskkonna loomine Windowsis:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config.example.yaml config.yaml
```

Vundamendi CLI-käsud:

```powershell
.\.venv\Scripts\wlc-manager.exe --config config.yaml config validate
.\.venv\Scripts\wlc-manager.exe --config config.yaml db migrate
.\.venv\Scripts\wlc-manager.exe --config config.yaml status
.\.venv\Scripts\wlc-manager.exe --config config.yaml password generate --month 2026-09
.\.venv\Scripts\wlc-manager.exe --config config.yaml artifacts generate --month 2026-09
.\.venv\Scripts\wlc-manager.exe --config config.yaml notifications send --month 2026-09
.\.venv\Scripts\wlc-manager.exe --config config.yaml wlc status
.\.venv\Scripts\wlc-manager.exe --config config.yaml wlc set-state disabled
.\.venv\Scripts\wlc-manager.exe --config config.yaml wlc apply-password --month 2026-09
```

Pikaajaliselt töötava scheduler'i käivitamine ja tervisekontroll:

```powershell
.\.venv\Scripts\wlc-manager.exe --config config.yaml run
.\.venv\Scripts\wlc-manager.exe --config config.yaml healthcheck
```

Scheduler loob graafiku käivitumisel uuesti, teeb kuuprotsessi kohe käivitumisel ja seejärel
seadistatud intervalliga. Töötluslukud ning teenuse südamelöök asuvad SQLite'is. WLAN-i
tegelik olek loetakse Cisco AireOS WLC-st ja sobitatakse seadistatud kohaliku töögraafikuga.
SSH serveri võti peab olema `wlc.known_hosts_file` failis ning WLC kasutajanimi ja parool
loetakse Vault Agenti loodud failidest.

Materjaligeneraator asendab SVG-mallis väljad `{{SSID}}`, `{{PASSWORD}}`, `{{SECURITY}}`
ja `{{QR_CODE}}`. QR-kood on vektorkujul ning sisaldab standardset Wi-Fi ühendusstringi
`WIFI:T:WPA;S:<SSID>;P:<parool>;;`. Valmis failid nimetatakse kujul `wifi-YYYY-MM.png`
ja `wifi-YYYY-MM.pdf`; alles hoitakse ainult käesoleva ja järgmise kuu faile. Materjalid
luuakse automaatselt sama kuuprotsessi käigus ning nende olek ja asukohad salvestatakse
SQLite'i.

Teavitus saadetakse Microsoft 365 connector-põhise SMTP relay kaudu tenant'i MX-aadressile,
TCP pordil 25 ja kohustusliku STARTTLS-iga. Rakendus SMTP kasutajanime ega parooli ei kasuta;
Microsoft 365 connector peab saatva serveri tuvastama staatilise avaliku IP-aadressi järgi.
Saatja peab kuuluma tenant'is aktsepteeritud domeeni. Kirja pealkiri ja sisu on YAML-is
seadistatavad väljadega `{{MONTH}}` ja `{{SSID}}`; parooli kirja teksti ega logidesse ei lisata.

Iga kuu kiri saab deterministliku `Message-ID` väärtuse ja saatmisolek salvestatakse enne SMTP
andmeedastust. Edukalt saadetud kirja korduskäivitus ei saada uuesti. Kui protsess katkeb hetkel,
mil SMTP vastuvõtmise tulemus pole kindel, peatatakse automaatne kordussaatmine. Pärast adressaadi
postkasti kontrollimist saab operaator vajadusel teadlikult korrata:

```powershell
.\.venv\Scripts\wlc-manager.exe --config config.yaml notifications send --month 2026-09 --retry-uncertain
```

Käesoleva kuu teavitatud parool rakendatakse WLC-s automaatselt kuu esimesel
kalendripäeval või esimesel järgneval edukal kontrollil. WLAN lülitatakse muutmise ajaks
vajadusel välja, algne olek taastatakse, tulemus kontrollitakse ja konfiguratsioon
salvestatakse. Andmebaasikirje märgitakse `applied` olekusse alles pärast kõigi WLC
sammude õnnestumist. Järgmise kuu parooli ennetähtaegne käsitsi rakendamine nõuab
eraldi kinnitavat valikut:

```powershell
.\.venv\Scripts\wlc-manager.exe --config config.yaml wlc apply-password --month 2026-09 --allow-early
```

Ennetähtaegne rakendamine ei möödu teavituse nõudest: kirje peab alati olema enne
olekus `notified`.

Konteineri koostamine ja Linuxi integratsioonitestid:

```powershell
docker build --target test --tag wlc-manager:test .
docker build --tag wlc-manager:latest .
```

Tootmistõmmis sisaldab Cairo renderdusteeki ja töötab UID/GID `10001` all. Konfiguratsioon
eeldatakse asukohta `/config/config.yaml`; andmebaasi ja materjalide kataloogid tuleb anda
konteinerile püsivate kirjutatavate köidetena.

Kvaliteedikontroll:

```powershell
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest --cov=wlc_manager --cov-report=term-missing
```

Windowsi lokaalses testis jäetakse päris Cairo-renderdus vahele, kui süsteemis puudub Cairo
DLL. Docker `test`-etapp käivitab sama testi Linuxis koos vajaliku süsteemiteegiga.
