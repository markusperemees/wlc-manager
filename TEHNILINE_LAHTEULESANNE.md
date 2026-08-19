# Avaliku Wi-Fi halduri tehniline lähteülesanne

**Staatus:** kavand
**Eesmärk:** production-ready lahenduse arendamise alus

## 1. Eesmärk

Luua Pythonil põhinev konteineriseeritud rakendus, mis automatiseerib avaliku Wi-Fi parooli elutsükli Cisco AireOS WLC-s: parooli genereerimise, teavitusmaterjalide loomise ja saatmise, parooli rakendamise ning WLAN-i tööajapõhise sisse- ja väljalülitamise.

## 2. Funktsionaalsed nõuded

### 2.1 Parooli elutsükkel

- Järgmise kuu parool genereeritakse kolm tööpäeva enne selle kuu esimest tööpäeva. Tööpäevadena arvestatakse esmaspäeva kuni reedet; riigipühi esialgu ei arvestata.
- Parool rakendatakse WLC-s uue kuu esimesel kuupäeval sõltumata nädalavahetusest või riigipühast.
- Kui teenus ei töötanud kuu vahetumise hetkel, rakendatakse teavitatud parool esimesel järgneval edukal kontrollil. Rakendamata parooli korduskäivitus saadab WLC-sse sama soovitud väärtuse; `applied` olekus kirjet uuesti ei rakendata.
- Parool koosneb seadistatavast prefiksist, juhuslikust kolmekohalisest numbrist ja sõnastikusõnast kujul `<prefiks><number><sõna>`.
- Sõnastikust kasutatakse ainult sõnu, mis vastavad avaldisele `^[A-Za-z]+$`. Täpitähti, numbreid, tühikuid, kirjavahemärke või muid sümboleid sisaldavad kirjed jäetakse vahele ilma töövoogu katkestamata. Vahelejäetud kirjete arv logitakse, kuid kirjete sisu ei logita.
- Kui pärast filtreerimist ei jää ühtegi kasutuskõlblikku sõna või kõik sobivad sõnad on viimase 12 parooli jooksul kasutatud, lõpetatakse genereerimine selge veaga ja parooli ei muudeta.
- Uue parooli sõnastikusõna ei tohi kattuda viimases 12 paroolis kasutatud sõnaga. Võrdlus on tõstutundetu, kuid genereeritud paroolis säilitatakse sõnastikukirje algne kirjapilt. Eraldi täisparooli korduskontrolli ei nõuta, sest kordumatu sõnastikusõna välistab ka terve parooli kordumise.
- Rakendus peab talletama SQLite'i andmebaasis loetaval kujul vähemalt viimased 12 parooli, kasutatud sõnastikusõna, kehtivusperioodi, oleku ning loomise ja rakendamise ajatemplid.
- Toimingud peavad olema idempotentsed: korduskäivitus ei tohi luua sama perioodi jaoks uut parooli ega korrata edukalt lõpetatud tegevust.

### 2.2 Teavitusmaterjalid

- Rakendus täidab seadistatava SVG-malli uue parooli ja kehtivusinfoga ning loob CairoSVG abil PNG- ja PDF-faili.
- Malli `{{QR_CODE}}` kohale luuakse vektorkujul QR-kood, mille sisu järgib Wi-Fi QR-stringi vormingut `WIFI:T:<tüüp>;S:<SSID>;P:<parool>;;`. SSID, parool ja erimärgid kodeeritakse vormingu nõuete järgi ning QR sisaldab nelja mooduli laiust vaiketsooni.
- Materjalid saadetakse kohe pärast parooli edukat kontrollimist ja failide loomist seadistatud adressaatidele Microsoft 365 SMTP relay kaudu.
- Microsoft 365 connector-põhine relay kasutab tenant'i MX-aadressi, TCP porti 25 ja kohustuslikku STARTTLS-i vähemalt TLS 1.2 tasemel. Connector tuvastab saatva serveri staatilise avaliku IP-aadressi järgi; rakenduses ei kasutata SMTP AUTH kasutajanime ega parooli.
- Enne saatmist kontrollitakse mõlema manuse perioodipõhist failinime, tavalise faili staatust, signatuuri ja seadistatavat suuruse ülempiiri. Kirjal on perioodi kohta deterministlik `Message-ID`.
- Saatmise olek reserveeritakse andmebaasis enne SMTP `DATA` toimingut. Edukat saatmist ei korrata. Katkestuse või ebaselge SMTP tulemuse korral märgitakse saatmine ebakindlaks ning automaatne kordussaatmine peatatakse; kordamine nõuab operaatori teadlikku CLI valikut.
- Failisüsteemis säilitatakse ainult kehtiva kuu ja järgmise kuu PNG- ning PDF-failid. Vanemad failid kustutatakse pöördumatult alles pärast uue perioodi failide edukat loomist.
- SVG vahefaili ei säilitata, välja arvatud muutumatu mall.
- PNG- ja PDF-faili loomine peab õnnestuma enne, kui andmebaasikirje märgitakse olekusse `materials_created`; kirjes säilitatakse mõlema faili absoluutne asukoht.

### 2.3 WLC haldus

- Hallatakse ühte Cisco AIR-CT3504-K9 kontrollerit, mille AireOS versioon on 8.10.196.0. Kontrolleriga suheldakse SSH kaudu Netmiko teegi abil.
- SSH hostivõtit kontrollitakse seadistatud `known_hosts` faili vastu; tundmatut või muutunud võtit automaatselt ei aktsepteerita.
- Rakendus muudab ainult seadistuses määratud WLAN-i parooli ja administratiivset olekut.
- WLAN on sisse lülitatud esmaspäevast neljapäevani kell 08:00–16:45 ning reedel kell 08:00–15:45. Nädalavahetusel on WLAN välja lülitatud. Tööajad ja päevad peavad olema YAML-is seadistatavad.
- Enne ja pärast muudatust kontrollitakse WLC tegelikku olekut.
- PSK muutmisel lülitatakse WLAN vajadusel ajutiselt välja, taastatakse selle eelnev olek ja salvestatakse WLC konfiguratsioon. Kuna AireOS käsitleb PSK-d set-only väärtusena, kontrollitakse käsu tulemust ja WLAN-i oleku taastamist, kuid parooli ennast WLC-st tagasi ei loeta.
- WLC parooli tohib rakendada ainult andmebaasikirjest olekuga `notified`. Kirje viiakse olekusse `applied` ja rakendamise ajatempel salvestatakse alles pärast PSK käsu, WLAN-i oleku taastamise, järelkontrolli ning `save config` toimingu õnnestumist.
- Ühendus- või käsutõrke korral rakendatakse piiratud korduskatseid; ebaõnnestunud muudatust ei märgita lõpetatuks.

### 2.4 Käivitamine ja CLI

- Rakendus kontrollib WLAN-i soovitud olekut vaikimisi kord minutis ning kuu parooliprotsesside vajadust kord tunnis. Mõlemad intervallid peavad olema YAML-is seadistatavad.
- CLI peab võimaldama vähemalt oleku kontrolli, parooli genereerimist, materjalide loomist ja saatmist, parooli rakendamist ning WLAN-i oleku muutmist.
- Mõju avaldavad käsud peavad vaikimisi järgima samu valideerimis- ja auditireegleid nagu automaatne töövoog.
- Paralleelkäivitused peavad olema lukustatud, et sama tegevust ei saaks samaaegselt täita.

## 3. Arhitektuur

Rakendus jagatakse eraldatud mooduliteks: orkestreerimine ja ajastus, parooligeneraator, kalender, andmebaas, materjalide generaator, teavitused, WLC-adapter, konfiguratsioon ning logimine. Välised teenused peavad olema adapteritega asendatavad ja automaattestides simuleeritavad.

- Rakendus: Python 3
- Ajastus: pikaajaliselt töötav APScheduler 3.11 teenus; graafik luuakse käivitumisel uuesti ning töövoogude püsiv olek jääb SQLite'i
- Andmebaas: SQLite koos skeemiversioonide ja transaktsioonidega
- Käitus: Docker, haldus Portaineris
- Konfiguratsioon: valideeritav YAML
- Saladused: HashiCorp Vault Agenti kaudu faili või keskkonda; saladusi ei hoita YAML-is ega lähtekoodis
- Ajavöönd, tööajad, WLAN, adressaadid, mall ja paroolipoliitika peavad olema seadistatavad. Vaikimisi ajavöönd on `Europe/Tallinn`.
- Riigipühade kalendrit esimeses versioonis ei rakendata

## 4. Töökindlus, turve ja seire

- Kõik automaatsed ja CLI kaudu käivitatud protsessid seotakse unikaalse `run_id` väärtusega ning nende iga etapp logitakse struktureeritult Graylogi. Logitakse vähemalt protsessi ja etapi algus, lõpp, tulemus, kestus ning vea korral veakood ja veakirjeldus.
- Logidesse ei kirjutata paroole, WLC mandaate ega muid saladusi.
- Auditlogi peab näitama tegevuse tüüpi, perioodi, tulemust, kestust ja veateadet.
- Rakendus peab jätkama turvalisest olekust pärast taaskäivitust ning vältima poolikute töövoogude topelttäitamist.
- Scheduler käivitab oleku sobitamise kohe teenuse käivitumisel. Paralleeltöid piiratakse nii scheduler'i tasemel kui ka aeguvate SQLite'i rendilukkudega.
- Konteiner töötab mitte-root kasutajana, kasutab minimaalset kirjutusõigust ning pakub tervisekontrolli.
- SMTP- ja SSH-serveri identiteeti kontrollitakse; ebaturvalisi vaikevalikuid ei lubata.
- Tõrked, sealhulgas parooli rakendamise või teavituse ebaõnnestumine, logitakse Graylogi. Eraldi alarmide saatmine ei kuulu esimesse versiooni.
- Andmebaas ja genereeritud failid asuvad püsival andmekandjal ning andmebaasi varundus ja taastamine peavad olema dokumenteeritud.
- Kuna paroolid talletatakse loetaval kujul, käsitletakse andmebaasi salajase varana: sellele antakse ainult rakenduse kasutajale vajalikud minimaalsed failisüsteemiõigused ning seda ei lisata konteineri tõmmisesse ega logidesse.

## 5. Testimine ja vastuvõtukriteeriumid

Lahendus loetakse kasutusvalmis, kui:

1. automaattestid katavad paroolipoliitika, tööpäevade arvutuse, kuu vahetuse, säilitusreegli ja idempotentsuse;
2. integratsioonitestid katavad SQLite'i, SVG-st PNG/PDF-i loomise ning simuleeritud SMTP- ja WLC-ühenduse;
3. korduskäivitus ei tekita duplikaatparooli, -kirja ega WLC muudatust;
4. WLC-s rakendatud parool ja WLAN-i olek kontrollitakse pärast muudatust;
5. alles jäävad ainult kehtiva ja järgmise kuu PNG- ning PDF-failid;
6. saladusi ei esine lähtekoodis, konfiguratsioonis, logides ega konteineri tõmmises;
7. konteineri tervisekontroll, struktureeritud logimine ning varundus- ja taastamisjuhend on olemas;
8. paigaldus-, seadistus-, käitus- ja avariitaastejuhised on dokumenteeritud.

## 6. Esialgu väljaspool ulatust

- Graafiline kasutajaliides.
- Mitme sõltumatu WLC või WLAN-i keskne haldus.
- Kasutajapõhine autentimine ja rollihaldus.
- Riigipühade kalender ja automaatsed tõrkealarmid.
