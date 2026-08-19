# src/wlc_manager/config.py
- eemaldada mittemuudetavad config-väljad;
- tugevdada password prefix valideerimist;
- valideerida SMTP template'id configi laadimisel;
- eraldada runtime/preflight kontroll konfiguratsioon valideerimisest;
- täiendada konfiguratsiooni teste
- SMTP kasutajanimi ja parool ka secret-s, samamoodi nagu WLC kasutajanimi ja parool

# src/wlc_manager/passwords.py
- rename passwords.py -> password.py;
- tõsta dictionary loogika uude dictionary.py;
- tsentraliseerida PSK valideerimine password.py faili;
- valideerida parool enne DB-sse salvestamist;
- piirata prefix turvalisele ASCII märgistikule;
- vältida täieliku parooli korduskasutust;
- eristada duplicate/concurrent insert päris DB vigadest;
- viia dictionary_path generaatori konfiguratsiooni;
- lihtsustada klasside ja funktsioonide nimesid;
- täiendada security- ja boundary-teste.

# src/wlc_manager/artifacts.py
- eraldada poster generation uude poster.py;
- rename MonthlyArtifactReconciler -> ArtifactReconciler;
- rename ArtifactFiles -> PosterFiles;
- eemaldada cleanup generaatorist;
- eemaldada current_period generatori API-st;
- lisada source-, PNG- ja PDF-hashid;
- mitte usaldada olemasolevaid faile ainult nime/suuruse järgi;
- muuta artifactide publish crash-safe'iks;
- jõustada artifactide turvalised failiõigused;
- lihtsustada SVG template contract'i;
- eemaldada qr_auth_type configist;
- täiendada integrity-, corruption- ja crash recovery teste.

# src/wlc_manager/database.py
- muuta database.py -> db/package;
- jagada core, models, migrations, passwords, notifications, runtime;
- lisada ühine transaction context manager;
- lisada spetsiifilised DB exception'id;
- jõustada DB directory/file õigused ja umask 0077;
- kasutada WAL + synchronus=FULL; 
- kaaluda secure_delete=ON;
- viia poster artifactid eraldi tabelisse koos source/PNG/PDF hashidega;
- olemasolevaid migratsioone mitte muuta, ainult lisada uusi;
- võtta workflow_runs/workflow_steps päriselt kasutusele või eemaldada;
- eemaldada kasutamata state'id/schema väljad;
- lisada turvaline SQLite backup/restore mehhanism;
- täiendada concurrency-, rollback-, migration-, corruption- ja permission-teste;

# src/wlc_manager/scheduler.py
- rename SchedulerRuntime -> AppScheduler;
- rename reconcilitation.py -> rotation.py;
- viia kogu monthly workflow RotationService sisse;
- viia WLAN reconcilitation wlan_state.py / WlanReconciler sisse;
- scheduler peab ainult job'e registeerima ja käivitama;
- eemaldada service/dependency construction schedulerist;
- hiljem tsentraliseerida dependency construction app.py faili;
- muuta first-start/bootstrap fail-safe'iks;
- lisada lock lease renewal/context manager;
- eemaldada schedulerist hardcoded lock TLL-id;
- viia heartbeat interval configi ja tuletada healthcheck threshold sellest;
- viia lead_workdays=3 configi;
- lihtsustada job'ide registeerimise dubleeritud seadeid;
- lisada startup-, lock-expiry-, partial-failure-, retry- ja crash-recovery testid.

# src/wlc_manager/notifications.py
- tõsta SMTP transport smtp.py faili;
- eemaldada MonthlyNotificationReconciler, viia loogika RotationService sisse;
- rename MessageRelay -> MailRelay;
- rename NotificationNotAttemptedError -> DeliveryNotStartedError;
- kontrollida attachmentide SHA-256 enne saatmist;
- kasutada DB-s kinnitatud artifact metadata't;
- muuta attachmentide lugemine TOCTOU-kindlamaks;
- täpsustada SMTP delivery faaside klassifitseerimist;
- säilitada fail-safe UNCERTAIN käsitlus;
- STARTTLS/TLS 1.2+ jääb hardcoded security invariant'iks;
- eemaldada smtp.starttls configist;
- valideerida template'id juba config startup'is;
- uncertain retry ainult operaatori explicit tegevusena;
- täiendada SMTP failure-, integrity- ja crash-window teste.

# src/wlc_manager/wlc.py
- muuta wlc.py -> wlc/package;
- jagada client, models, parser, secrets;
- rename AireOsWlcClient -> AireOsClient;
- rename WlcCredentials -> Credentials;
- rename PskUpdateResult -> PskResult;
- tsentraliseerida PSK valideerimine;
- piirata genereeritud PSK turvalisele märgistikule;
- lisada WLC mutation'i UNCERTAIN seisund;
- eristada safe retry ja unsafe retry;
- lisada piiratud connect/read retry;
- tugevdada WLAN-id + SSID target verification'it;
- tugevdada failure recovery't ja lisada WlcRecoveryError;
- käsitleda save config eraldi persistence-etapina;
- tugevdada know_hosts ja secret-failide preflight kontrolli;
- eemaldada device_type configist;
- mitte viia tehnilisi konstante põhendamatult YAML-i;
- täiendada retry- uncertain-, recovery- ja security-teste.

# src/wlc_manager/cli.py
- muuta cli.py -> cli/ package;
- jagada main, system, rotation, wlc;
- luua app.py ühise dependency construction'i jaoks;
- CLI ei tohi sisaldada business workflow ega lock-loogikat;
- CLI ja scheduler peavad kasutama samu service'e;
- rename artifact -> poster;
- rename notifications -> notify;
- lisada preflight;
- muuta status päris runtime state ülevaateks;
- tsentraliseerida exit code'id;
- tsentraliseerida exception handling;
- WLC mutation käsud nõuavad --yes;
- säilitada --allow-early eraldi turvapiirina;
- password/secrets ei tohi kunagi stdout'i ega logidesse sattuda;
- eraldada structured logging ja CLI stdout;
- lisada vajadusel --json;
- täiendada CLI failure/security teste.

# src/wlc_manager/password_application.py
- rename password_application.py -> password_apply.py;
- rename PasswordApplicationService -> PasswordApplier;
- rename result/outcome/error klassid lühemaks;
- eemaldada reconcile(), orchestration läheb RotationService sisse;
- lisada eraldi WLC application lifecycle;
- lisada APPLYING ja UNCERTAIN state'id;
- vältida automaatset retry'd ambiguous WLC mutation'i korral;
- käsitleda DB failure'it pärast WLC edu UNCERTAIN olukorrana;
- säilitada allow_early, kuid mitte lasta sellel notification requirement'i vahele jätta;
- säilitada injectable today;
- rename controller_factory -> client_factory;
- täiendada crash-, uncertain- ja recovery-teste.

# src/wlc_manager/reconciliation.py
- rename reconcilitation.py -> rotation.py;
- replace MonthlyPasswordReconciler -> RotationService;
- koondada kogu password -> poster -> notification -> WLC workflow siia;
- schedulerist eemaldada monthly business logic;
- eemaldada dictionary_path sellest kihist;
- jätta date calculations scheduling.py faili;
- teha first-start fail-safe ja nõuda initialization/bootstrap'i;
- muuta workflow resumable ja state-drive;
- ambiguous/uncertain state peatab automaatse jätkamise;
- rotation lock peab kuuluma service'i, miite schedulerisse;
- lihtustada result tüüpe;
- asendada praegune current-month-on-startup test;
- lisada partial-failure, restart, uncertain ja idempotency testid.

# src/wlc_manager/scheduling.py
- rename scheduling.py -> schedule.py;
- mitte jagada faili väiksemaks;
- rename workday -> weekday, kui riigipühi ei arvestata;
- rename lead_workdays -> lead_weekdays;
- eemaldada hardcoded =3 business default,;
- rename generation_date -> password_generation_date;
- rename generation_is_due -> password_generation_due;
- rename password_application_is_due -> password_apply_due;
- säilitada YearMonth;
- säilitada catch-up semantics;
- hoida funktsioonid DB-st, WLC-st ja workflow state'ist sõltumatuna;
- vähendada sõltuvust config.py mudelitest;
- muuta weekday mapping explicit'iks;
- täiendada DST-, calendar-boundary- ja weekday-semantics teste.

# src/wlc_manager/observability.py
- rename observabilty -> logs.py;
- rename ContextLoggerAdapter -> RunLogger;
- rename log field process_name -> process;
- standardiseerida logiv'ljad ja event nimed;
- lisada standardne error_code;
- säilitada automaatne run_id;
- secret redaction jääb defense-in.depth kaitseks, mitte põhiliseks turvamehhanismiks;
- sanitiseerida exception'id enne logimist;
- eraldada audit loggingust ja kasutada AuditRepository't;
- muuta logging configuration idempotentseks;
- eelistatult eemaldada custom GelfUdpHandler;
- saata rakendusest JSON stdout'i ja transportida logid Graylogi infrastruktuuri tasemele;
- selle korral eemaldada GraylogConfig rakenduse configist;
- mitte teha third-party logger level'e eraldi YAML seadistuseks;
- täiendada failure-, secret-leak-, schema- ja logging-configuration teste.

# src/wlc_manager/__init__.py + __main__.py
- vältida versiooni dubleerimist;
- kasutada pyproject.toml/package metadata't ainsa version source'ina;
- mitte lisada siia business-loogikat ega suuri re-export'e;
- jätta ainult CLI entrypoint;
- business-loogikat ega konfiguratsiooni laadimist siia mitte lisada.

# config.example.yaml
- rename artifacts → poster;
- rename svg_template_path → template_path;
- rename monthly_check_seconds → rotation_check_seconds;
- lisada scheduler.heartbeat_seconds;
- lisada rotation.lead_weekdays;
- kuvada database.busy_timeout_seconds;
- lisada WLC connect retry seaded;
- lisada logging.level;
- eemaldada qr_auth_type;
- eemaldada wlc.device_type;
- eemaldada smtp.starttls;
- eemaldada kogu graylog sektsioon;
- hoida YAML-is ainult päriselt deployment'ist sõltuvad väärtused;
- security invariandid jätta koodi.

# pyproject.toml
- säilitada setuptools, mitte vahetada build backend’i ilma vajaduseta;
- lisada ja commitida uv.lock;
- võtta dependency/install workflow’ks uv;
- hoida dependency range’id pyproject.toml-is, täpsed versioonid lockfile’is;
- muuta dev extra → [dependency-groups].dev;
- piirata requires-python ainult CI/productionis testitud Python versioonidele;
- kasutada pyproject.toml-i ainsa version source’ina;
- täiendada Ruffi security/timezone/logging reeglitega;
- lisada ruff format --check;
- tsentraliseerida coverage config pyproject.toml-i;
- kasutada branch coverage’it;
- määrata coverage minimum pärast refaktori testibaasi stabiliseerimist;
- kaaluda pytest-timeout;
- lisada static type checking;
- lisada CI dependency vulnerability scan;
- säilitada olemasolev wlc-manager CLI entrypoint.

# Dockerfile
- kasutada uv.lock-i ja locked dependency installi;
- teha selge builder / test / runtime multi-stage build;
- runtime image'i mitte lisada dev/build tööriistu;
- kaaluda Python base image'i digestiga pin'imist;
- säilitada non-root UID/GID 10001;
- muuta /app/data ja /app/artifacts permissionid 0700;
- jõustada rakenduses umask 0077;
- hoida runtime OS paketid minimaalsed;
- configi, secrete, DB-d ja genereeritud faile image'i mitte COPY'da;
- säilitada healthcheck, aga hoida see lokaalne ja odav;
- lisada test stage'i format/lint/type/branch coverage kontrollid;
- säilitada praegune ENTRYPOINT + CMD;
- deploymentis kasutada read-only root filesystem'i, no-new-privileges ja cap_drop ALL;
- writable mountid ainult vajalikele data/artifact path'idele.

# wifi-poster.svg
- failinimi jätta wifi-poster.svg;
- hoida mall minimalistlik;
- eemaldada redundantne {{QR_CODE}}, kasutada ainult id="qr-code";
- lisada {{MONTH}} kehtivusinfo jaoks;
- kasutada deterministlikult installitud Liberation Sans fonti;
- keelata välised SVG ressursid/template reference'id;
- hoida layout ja staatiline tekst SVG-s, mitte configis;
- testida maksimaalse SSID ja PSK pikkusega;
- template muutus peab meie varasema plaani järgi muutma artifact source_hash-i ja sundima regenereerimise.

# .gitignore / .dockerignore
- säilitada data/, artifacts/, DB, secrets ja päris configi ignoreerimine;
- lisada .mypy_cache, coverage/build jäägid;
- uv.lock peab olema tracked;
- config.example.yaml peab olema tracked;
- runtime dictionary jääb data/ tõttu Gitist välja.
- .dockerignore — HARDEN
- lisada .env, .env.*, config.yaml, secrets/;
- lisada mypy/coverage/build cache'id;
- välistada kogu runtime data ja DB;
- secret ei tohi jõuda isegi Docker build context'i;
- uv.lock, README.md, source, templates ja test-stage'i vajalikud failid peavad context'i jääma.

# test-suited
- olemasolevaid teste kasutada refaktori regression safety net'ina;
- lisada tests/conftest.py ühiste fixture'ite jaoks;
- jagada testid unit/ ja integration/;
- rename testifailid vastavalt uutele source moodulitele;
- eemaldada korduv config/DB/setup kood;
- hoida adapterispetsiifilised fake'id vastava testi juures;
- muuta kõik date/time sõltuvused deterministlikuks;
- muuta CLI testid õhukeseks ja keskenduda CLI contract'ile;
- asendada first-start automaatset rotatsiooni kinnitav test fail-safe testiga;
- lisada põhjalikud crash-, recovery-, concurrency-, integrity- ja security-testid;
- CI-s ei tohi production dependency't vajavad integration testid vaikselt skip'ida;
- kasutada branch coverage'it;
- coverage threshold määrata pärast refaktorit;
- päris WLC/SMTP vastu teste mitte panna tavapärasesse CI-sse — nende jaoks teha hiljem eraldi kontrollitud staging/smoke test.

# järjekord
pyproject.toml + uv.lock
Pane esmalt build ja dependency’d reprodutseeritavaks.
config.py + config.example.yaml
Pane lõplik config-struktuur paika, eemalda fake-config väljad ja lisa uued rotation, logging, retry jm väljad.
schedule.py
Rename scheduling.py, korrasta weekday, lead_weekdays ja puhas date/time loogika.
dictionary.py + password.py
Split passwords.py, tsentraliseeri PSK valideerimine ja tugevda parooligeneratsiooni.
db/ package
Split database.py: core, models, migrations, repository’d, audit, lockid. Lisa uued application/artifact state’id ja hashid.
poster.py + artifacts.py
Split genereerimine ja reconciliation, lisa source/PNG/PDF hashid, atomic publish ja permissions.
smtp.py + notifications.py
Eralda transport, tugevda attachment integrity’t ja UNCERTAIN käsitlust.
wlc/ package
Split wlc.py, tugevda SSH/known_hosts/secrets, retry, recovery ja WLC UNCERTAIN.
password_apply.py
Rename ja lisa püsiv WLC application lifecycle: PENDING/APPLYING/APPLIED/UNCERTAIN.
rotation.py
Asenda reconciliation.py uue RotationService-iga. Siia tuleb kogu state-driven password → poster → notify → apply workflow.
wlan_state.py
Tõsta schedulerist WLAN state reconciliation eraldi service’iks.
app.py
Tee üks composition root, mis ehitab repository’d ja service’id. CLI ja scheduler hakkavad kasutama sama App objekti.
scheduler.py
Muuda õhukeseks: ainult jobide registreerimine ja service’ide käivitamine.
cli/ package
Split cli.py, lisa preflight, exit code’id, --yes, --json ja keskne exception mapping.
logs.py
Rename observability.py, eemalda custom GELF UDP, jäta JSON stdout + standardiseeritud event’id. Audit jääb DB kihti.
wifi-poster.svg
Uus template contract, {{MONTH}}, deterministic font, {{QR_CODE}} eemaldamine.
Dockerfile
Alles nüüd, kui package/config/dependency struktuur on stabiilne: locked build, builder/test/runtime stages, permissions ja hardening.
.gitignore + .dockerignore
Viimane cleanup vastavalt uuele struktuurile ja failidele.
Test-suite ümberkorraldus ja hardening
conftest.py, unit/, integration/, crash/recovery/security/concurrency testid, branch coverage.
Lõplik cleanup
Eemalda surnud kood, kasutamata klassid/importid/config-väljad, vanad failinimed ja ajutised compatibility wrapper’id. Seejärel kogu repo ruff format, ruff check, type-check, pytest ja Docker build.

# projekti struktuur
wlc-manager/
├── .github/
│   └── workflows/
│       └── ci.yml
│
├── docs/
│   ├── TEHNILINE_LAHTEULESANNE.md
│   ├── HARDENING_AND_REFACTORING.md
│   ├── ARCHITECTURE.md
│   ├── SECURITY.md
│   └── OPERATIONS.md
│
├── src/
│   └── wlc_manager/
│       ├── __init__.py
│       ├── __main__.py
│       │
│       ├── app.py
│       ├── config.py
│       ├── schedule.py
│       ├── dictionary.py
│       ├── password.py
│       ├── password_apply.py
│       ├── poster.py
│       ├── artifacts.py
│       ├── notifications.py
│       ├── smtp.py
│       ├── rotation.py
│       ├── wlan_state.py
│       ├── scheduler.py
│       ├── logs.py
│       │
│       ├── db/
│       │   ├── __init__.py
│       │   ├── core.py
│       │   ├── models.py
│       │   ├── migrations.py
│       │   ├── passwords.py
│       │   ├── notifications.py
│       │   ├── artifacts.py
│       │   ├── applications.py
│       │   ├── runtime.py
│       │   └── audit.py
│       │
│       ├── wlc/
│       │   ├── __init__.py
│       │   ├── client.py
│       │   ├── models.py
│       │   ├── parser.py
│       │   └── secrets.py
│       │
│       ├── cli/
│       │   ├── __init__.py
│       │   ├── main.py
│       │   ├── system.py
│       │   ├── rotation.py
│       │   └── wlc.py
│       │
│       └── templates/
│           └── wifi-poster.svg
│
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
│
├── .dockerignore
├── .gitignore
├── Dockerfile
├── README.md
├── config.example.yaml
├── pyproject.toml
└── uv.lock