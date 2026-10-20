# Rotaidem på STRATO Webhosting Basic

Den här guiden flyttar hela Rotaidem (webbplats, konton, admin, beställningar, licenser, bilder) till ditt befintliga STRATO-webbhotell. Ingen extra hosting, ingen Gunicorn, inga ändringar i DNS, domän eller e-post.

---

## 1. Kort sammanfattning

| Fråga | Svar |
|---|---|
| Kan STRATO Basic köra en serverprocess (Gunicorn, `python server.py`) dygnet runt? | **Nej.** Webbhotellet är delat. SSH är till för att hantera filer och köra kommandon, inte för program som ska fortsätta köra efter utloggning. Det finns inget sätt att låta webbservern skicka trafik till en egen port. |
| Stöder STRATO Python på något officiellt sätt? | **Ja, som CGI.** STRATO anger att Python (och Perl, Ruby) kan köras som CGI-skript från paketet Basic, med `/usr/bin/python3`. WSGI-modul (mod_wsgi) eller Passenger finns inte. |
| Fungerar Flask utan Gunicorn? | **Ja.** Flask kan köras som CGI med Pythons inbyggda `wsgiref`. Apache startar programmet för varje anrop till `/api/...` och programmet avslutas när svaret är skickat. |
| Fungerar inloggning, sessioner, databas, formulär och API då? | **Ja.** Inloggningen bygger redan på en slumpad nyckel (token) som sparas i databasen, inte i serverns minne. Därför spelar det ingen roll att varje anrop är en ny process. Allt är testat i en kopia av STRATO-miljön (se avsnitt 9). |
| Behövs en omskrivning till PHP? | **Nej.** Flask via CGI uppfyller kraven, så backend behålls i Python och alla funktioner finns kvar. |
| Dygnet runt? | **Ja, så länge STRATO:s webbserver är uppe.** Det finns ingen egen process som kan krascha eller somna (till skillnad från Render free, som somnar). Varje anrop tar cirka 0,2 till 0,5 sekunder extra eftersom Python startas. |

---

## 2. Vad jag har undersökt

**STRATO Webhosting Basic** innehåller (enligt STRATO och oberoende tester): PHP, Perl, Python och Ruby som CGI, MySQL-databaser, SSH och SFTP, `.htaccess` och SSL. Du har själv bekräftat SSH, Python 3.11.2 och Flask 2.2.2. Rotaidem använder ändå inte serverns Flask: Flask 3.1.1 och alla andra paket följer med i `strato/vendor`, i samma versioner som på Render. Sidan fungerar då oavsett vilka paket STRATO har installerat.

**Begränsningar som styr lösningen:**

1. **Inga permanenta processer.** Ett program som startas via SSH (till exempel Gunicorn eller `flask run`) stoppas när du loggar ut eller när STRATO städar processer. Det kan inte heller ta emot trafik från rotaidem.com, eftersom webbservern inte kan skicka trafik vidare till en egen port. Därför fungerar varken Gunicorn, `waitress` eller `python main.py` här.
2. **Ingen WSGI-modul eller Passenger.** Python körs bara som CGI, alltså ett skript i mappen `cgi-bin`.
3. **CGI startar en ny process per anrop.** Allt som programmet håller i minnet försvinner mellan anropen. Rotaidem sparar allt viktigt i databasen, så det är inget problem.
4. **Apache skickar inte `Authorization`-headern till CGI-skript** om man inte konfigurerar det. Det löses i `.htaccess`, och sidan skickar dessutom en reservkopia av inloggningsnyckeln i en egen header (`X-Rotaidem-Token`).
5. **Filsystemet** på delade webbhotell kan vara ett nätverksfilsystem, där SQLite:s fillåsning inte är pålitlig. Därför rekommenderas **STRATO:s MySQL-databas**, som ingår i paketet.
6. **Databasversion.** Nya STRATO-databaser är antingen **MySQL 8** eller **MariaDB 10.11**. MySQL 8 tillåter inte standardvärden på TEXT-kolumner, så sådana kolumner är nu VARCHAR. Båda versionerna är testade.

Källor: [STRATO: Vad är ett CGI](https://www.strato.de/faq/hosting/was-ist-ein-cgi/), [STRATO: När kan ett Perl-skript startas](https://www.strato.de/faq/hosting/wann-ist-ein-perl-script-startfaehig/), [STRATO: tekniska funktioner](https://www.strato.de/faq/hosting/wie-finde-ich-heraus-welche-technische-unterstuetzungen-zb-php-version-ich-fuer-meine-webinhalte-bei-strato-brauche/), [webhostingvergleich.eu: STRATO](https://www.webhostingvergleich.eu/strato/), [netzsieger.de: STRATO-test](https://www.netzsieger.de/p/strato-webhosting), [Flask: CGI-driftsättning](https://flask-dev.readthedocs.io/en/stable/deploying/cgi.html), [SQLite-forum om NFS-låsning](https://sqlite.org/forum/forumpost/89c52cdf661b77d6?raw).

**Det jag inte kunnat kontrollera direkt på din server** finns i listan i avsnitt 9. Kontrollsidan (steg 5) kontrollerar det mesta åt dig.

---

## 3. Så fungerar lösningen

```
Besökare ──> rotaidem.com (STRATO Apache)
               ├─ /, /login, /heavypro, bilder, CSS ... ──> vanliga filer (snabbt)
               └─ /api/..., /media/...  ──.htaccess──> cgi-bin/rotaidem.py
                                                        └─ startar Flask-appen (server.py)
                                                             └─ STRATO MySQL
```

Mappar på ditt webbutrymme efter installationen:

```
/                         ← mappen som rotaidem.com visar (oftast roten)
├─ index.html, login.html, admin.html, ...   webbplatsens sidor (kopieras dit av setup)
├─ .htaccess              styr adresser och skyddar mappar
├─ cgi-bin/rotaidem.py    startar backend vid varje anrop
├─ rotaidem/              programmet (server.py, strato/ ...) – spärrat från webben
└─ rotaidem-data/         inställningar (config.env) – spärrat från webben
```

---

## 4. Databas, lösenord, sessioner och hemligheter

- **Databas:** STRATO MySQL 8 eller MariaDB 10.11 (rekommenderas). Den ingår i Basic, ligger kvar oberoende av filerna och säkerhetskopieras av STRATO. Alternativt en SQLite-fil i `rotaidem-data/`.
- **Anslutningen till databasen** krypteras med TLS när servern erbjuder det. Det gör också att MySQL 8:s inloggningsmetod fungerar utan extra paket.
- **Produkter raderas aldrig automatiskt.** Installation, uppdatering och import tar inte bort några produkter. Vill du tömma katalogen gör du det uttryckligen med `reset-catalog` (se avsnitt 8). Programmet frågar först och sparar en kopia.
- **Lösenord:** sparas aldrig i klartext. De hashas med PBKDF2-SHA256 (100 000 varv, unikt salt per konto), samma som tidigare.
- **Sessioner:** vid inloggning skapas en slumpad 128-bitars nyckel som sparas på kontot i databasen och i webbläsaren. Utloggning tar bort den. Inget ligger i serverns minne, så det fungerar med CGI.
- **Administratörslösenordet** sparas inte i någon fil. Installationsprogrammet skapar kontot direkt i databasen med det hashade lösenordet.
- **Hemligheter** (`SECRET_KEY`, databaslösenord, `LICENSE_API_KEY`, `RESEND_API_KEY`) ligger i `rotaidem-data/config.env`:
  - filen kan bara läsas av ditt eget konto (behörighet 600, mappen 700),
  - SQLite-filen är också 600, och nya filer skapas privata,
  - mappen spärras för webben med en egen `.htaccess`,
  - huvud-`.htaccess` blockerar dessutom adresserna till `rotaidem/` och `rotaidem-data/`,
  - kontrollsidan visar att de är spärrade (svar 403).
- **Inget av detta sparas i GitHub.** `config.env` skapas på servern av installationsprogrammet.

**Varför 600 räcker, och när det inte gör det.** Med 600 får bara ditt eget konto läsa filen. Det fungerar när webbservern kör CGI-programmet som ditt konto. Det är det normala på STRATO:s webbhotell, men det måste bekräftas på din server. Kontrollsidan visar raden *Webbserverns användare*:

| Kontrollsidan säger | Betyder | Gör så här |
|---|---|---|
| *kör programmet som ditt eget konto* | Allt kan vara privat (600/700). | Inget. |
| *... men ni delar grupp* | Webbservern kör som en annan användare i din grupp. | `python3 rotaidem/strato/setup.py permissions group` (640/750, eller 660/770 för SQLite). Andra användare får fortfarande inte läsa. |
| *... och ni delar ingen grupp* | Webbservern kör som en helt annan användare. | Hemligheterna kan då inte skyddas från andra kunder på samma server. Programmet gör aldrig filerna läsbara för alla (644/666/777). Kontakta STRATO och fråga om CGI kan köras som ditt konto, och installera inte förrän det är löst. |

---

## 5. Det här behöver du

1. **SFTP-programmet FileZilla** (gratis): https://filezilla-project.org
2. **SSH-uppgifter och SFTP-uppgifter** från STRATO-inloggningen: *Hosting > SFTP/SSH* (server, användarnamn, lösenord).
3. **En MySQL-databas** från STRATO-inloggningen: *Datenbanken > Datenbank anlegen*. Du kan välja MySQL 8 eller MariaDB 10.11, båda fungerar. Skriv upp:
   - server (Host), till exempel `database-5012345678.webspace-host.com`
   - databasnamn, till exempel `dbs12345678`
   - användare, till exempel `dbu12345678`
   - lösenordet du väljer

   Att skapa en databas ändrar inte domän, DNS eller e-post.
4. **Vilken mapp rotaidem.com visar.** Titta, men ändra inget: *Domains > Domainverwaltung > rotaidem.com > Einstellungen*. Där står målet (Ziel), oftast `/`.

> **Viktigt om domänen:** om rotaidem.com i dag pekar på Render (eller någon annan tjänst) i stället för på STRATO-webbutrymmet, måste domänens mål ändras för att besökarna ska komma till STRATO-versionen. Det är en domäninställning som jag **inte** ändrar. Du bestämmer själv om och när du vill göra det.

---

## 6. Installation steg för steg

### Steg 1: Ladda ner filerna
1. Öppna https://github.com/tornqvisteliaz-cyber/rsg.com/tree/claude/lucid-tesla-liazv2
2. Klicka på den gröna knappen **Code** och sedan **Download ZIP**.
3. Packa upp ZIP-filen på datorn.
4. Byt namn på den uppackade mappen till exakt **`rotaidem`**.

### Steg 2: Ladda upp med SFTP (FileZilla)
1. Öppna FileZilla. Fyll i *Värd* `sftp://` + SFTP-servern från STRATO, ditt användarnamn, lösenord och port `22`. Klicka på **Snabbanslut**.
2. Till höger visas webbutrymmet. Stanna i översta mappen (`/`).
3. Dra mappen **`rotaidem`** från vänster sida till höger sida. Vänta tills allt är uppladdat.

### Steg 3: Logga in med SSH
- **Windows:** öppna *Terminal* eller *PowerShell*. **Mac:** öppna *Terminal*.
- Skriv (byt ut mot dina uppgifter från STRATO):

  ```
  ssh ANVÄNDARNAMN@SSH-SERVER
  ```
- Svara `yes` första gången och skriv lösenordet. Det syns inte när du skriver, det är normalt.

### Steg 4: Kör installationsprogrammet
Skriv:

```
python3 rotaidem/strato/setup.py
```

Programmet frågar en sak i taget. Tryck **Enter** för att välja förslaget inom hakparentes.

1. **Mappen för rotaidem.com:** det du såg i steg 5.4 (oftast bara Enter för `/`).
2. **Databas:** `1` för MySQL, sedan uppgifterna från steg 5.3. Programmet testar anslutningen direkt.
3. **Administratörskonto:** användarnamn, e-post och ett lösenord (minst 8 tecken). Kontot skapas direkt, och lösenordet sparas bara hashat.
4. **Webbplatsens adress:** Enter för `https://rotaidem.com`.
5. **RESEND_API_KEY:** klistra in din Resend-nyckel (samma som på Render), eller Enter för att hoppa över.
6. **LICENSE_API_KEY:** klistra in samma nyckel som licensgeneratorn använder i dag, eller Enter för att skapa en ny.

Till sist skriver programmet ut adressen till **kontrollsidan**.

### Steg 5: Öppna kontrollsidan
Öppna adressen som programmet skrev ut (`https://rotaidem.com/cgi-bin/rotaidem-check.py?key=...`). Alla rader ska vara **gröna (OK)**. Är något rött, ta en skärmbild och skicka den till mig.

### Steg 6: Logga in
Gå till `https://rotaidem.com/login` och logga in med administratörskontot. Du kommer till `/admin`.

---

## 7. Flytta data från Render (konton, licenser, produkter, bilder)

Gör detta om du har riktiga konton eller licenser på Render-sidan.

1. Logga in på den **gamla** sidan (`https://rotaidem.onrender.com/admin`).
2. Gå till **Activity log** och klicka på **Download backup**. En fil `rotaidem-backup-....json` laddas ner.
3. Ladda upp filen med FileZilla till översta mappen (samma ställe som `rotaidem`).
4. Skriv i SSH (byt filnamnet mot ditt):

   ```
   python3 rotaidem/strato/setup.py import rotaidem-backup-2026-10-20.json
   ```
5. Skriv `JA` när programmet frågar. Allt i den nya databasen ersätts med innehållet i backupen.
6. Logga in med samma konton och lösenord som på Render.
7. Ta bort backupfilen från servern med FileZilla när du är klar, eftersom den innehåller kunddata.

**Licensgeneratorn och HeavyPro-appen** pratar i dag med `https://rotaidem.onrender.com`. När STRATO-sidan fungerar och domänen visar den, säg till, så byter jag adressen till `https://rotaidem.com` i appen och generatorn. Fram till dess fortsätter de att fungera mot Render.

---

## 8. Senare: uppdatera, byta lösenord, felsöka

**Uppdatera till en ny version:** ladda ner ZIP:en igen (steg 1), ladda upp mappen `rotaidem` och låt FileZilla skriva över filerna. Kör sedan:

```
python3 rotaidem/strato/setup.py update
```

Inställningar och data i `rotaidem-data` och i MySQL påverkas inte.

**Tömma produktkatalogen** (tar bara bort produkter, inte konton, beställningar, licenser eller bilder; programmet frågar först och sparar en kopia i `rotaidem-data`):

```
python3 rotaidem/strato/setup.py reset-catalog
```

**Glömt lösenord:**

```
python3 rotaidem/strato/setup.py password
```

**Kontrollera allt från SSH:**

```
python3 rotaidem/strato/setup.py check
```

**Ändra en inställning** (till exempel Resend-nyckeln): öppna `rotaidem-data/config.env` i FileZilla (högerklicka och välj *Visa/Redigera*), ändra raden och spara. Ändringen gäller från nästa sidvisning.

**Felsökning**

| Det som syns | Gör så här |
|---|---|
| Kontrollsidan visar *500 Internal Server Error* eller laddar ner filen i stället för att visa den | Python-CGI körs inte i `cgi-bin`. Kör `python3 rotaidem/strato/setup.py update` (sätter rätt behörigheter) och försök igen. Hjälper det inte, kontakta STRATO-supporten och fråga: *"Kann ich Python-3-CGI-Skripte im Ordner cgi-bin meiner Domain ausführen?"* |
| *Kan inte läsa inställningarna* | Läs meddelandet under det. Det säger om webbservern kör som ditt konto, delar grupp eller inte. Se tabellen i avsnitt 4. |
| *Flask* eller *Programmet startar* är rött | Kontrollera att hela mappen `rotaidem/strato/vendor` laddades upp. Flask och alla andra paket följer med där, så inget behöver installeras. |
| *Backend via /api* är rött | `.htaccess` saknas eller är fel mapp. Kontrollera att du angav rätt mapp i steg 4 och kör `update`. |
| *E-post (Resend)* är rött | Webbservern får inte nå internet. Allt annat fungerar, men licensmejl och utskick går inte att skicka. Säg till, så lägger jag till utskick via STRATO:s egen e-postserver (SMTP) i stället. |
| Den gamla startsidan syns fortfarande | Domänen visar en annan mapp än den du angav. Kör setup igen med rätt mapp. |

---

## 9. Hur lösningen är testad, och vad som måste kontrolleras på STRATO

### Automatiska tester (i koden: `strato/tests/test_strato.py`)
Testerna installerar Rotaidem med `setup.py` i ett tillfälligt webbutrymme. Varje anrop körs sedan som en egen CGI-process, precis som Apache på STRATO gör, med Python 3.11 och en minimal miljö utan UTF-8-inställning. De körs mot tre databaser:

- SQLite,
- **MySQL 8.0.46** (standardläge med `ONLY_FULL_GROUP_BY` och strikt läge, `caching_sha2_password`, TLS),
- **MariaDB 10.11** (strikt läge, TLS).

Resultat: 39 av 39 tester godkända (13 per databas). De täcker:
- installationen: filer, `#!/usr/bin/python3`, radslut, behörigheter 755/700/600, `.htaccess`, och att inget administratörslösenord sparas,
- `permissions` och `permissions group`, som aldrig öppnar för alla,
- att programmet går med bara paketen i `strato/vendor` (`python3 -S`, inga globala paket),
- kontrollsidan, inklusive att fel nyckel ger 404,
- adresser: `/health`, `/api/...`, okända adresser, `/media/...`,
- inloggning via e-post och via användarnamn, med både `Authorization` och reservheadern, samt utloggning,
- att kunder inte kommer åt administratörsfunktioner,
- produkter: skapa och ändra, och att de finns kvar efter `update` och efter en framtida schemaändring,
- beställningar med belopp (29,99), licens till kunden och omsättning,
- licenser så som HeavyPro använder dem: aktivera, aktiveringsgräns, validera, avaktivera, spärra, och import med generatorns nyckel,
- bilduppladdning: lagras och visas byte för byte, fel filtyp och för stora filer avvisas, bilden kan väljas för en plats på sidan och kan tas bort,
- e-post mot en låtsad Resend-server: licensmejl, testutskick och felmeddelande när domänen inte är verifierad,
- backup, `import` (med tidsstämplar från PostgreSQL), och `reset-catalog` med bekräftelse och sparad kopia.

Kör dem själv (valfritt): `python3.11 -m unittest discover -s strato/tests -v`

### Manuella tester i en kopia av STRATO-miljön
- **Riktig Apache** med CGI i `cgi-bin`, `.htaccess` och `mod_rewrite`, mot MySQL 8, i en webbläsare: kontrollsidan helt grön, inloggning, admin, bilduppladdning, banner, produkter, licenser och backup.
- Webbservern skickar vidare `Authorization`-headern tack vare `.htaccess`.
- `rotaidem-data/`, `rotaidem/` och `vendor/` ger svar 403.
- Webbservern som **annan användare**: inget läcker, och kontrollsidan förklarar orsaken. Med **gemensam grupp** löser `permissions group` problemet.
- Render-versionen (Gunicorn med PostgreSQL) fungerar som förut, och befintliga produkter finns kvar.

### Måste kontrolleras på ditt riktiga STRATO-webbhotell
Detta kan inte testas utanför STRATO. Kontrollsidan (steg 5) visar punkt 1–5 automatiskt. Punkt 6–9 provar du själv.

1. **Python-CGI körs i `cgi-bin` i din domänmapp**, med `/usr/bin/python3`. Om inte visas *500* eller så laddas filen ner.
2. **Webbservern kör CGI som ditt eget konto**, raden *Webbserverns användare*. Avgör om 600-behörigheterna fungerar (avsnitt 4).
3. **`.htaccess` och `mod_rewrite` är på**, raderna *Backend via /api* och *Sidor utan .html*.
4. **MySQL-anslutningen från webbservern**, med STRATO:s databasserver (`database-....webspace-host.com`), MySQL 8 eller MariaDB.
5. **Att Resend kan nås från webbservern**, raden *E-post*. Om inte fungerar allt utom e-post.
6. **Ett riktigt mejl:** skicka ett testutskick till dig själv i Admin, *Email a product*. Det bekräftar också att rotaidem.com är verifierad hos Resend.
7. **Bilduppladdning nära gränsen (cirka 4–5 MB):** STRATO kan ha en egen gräns för hur stora anrop får vara.
8. **Svarstider:** varje API-anrop bör ta under en sekund. Admin gör många anrop när sidan laddas, så kontrollera att panelen känns rimlig.
9. **HTTPS:** att `https://rotaidem.com` har ett giltigt certifikat (ingår hos STRATO). Ändra inget, titta bara.

## 10. Ärlig bedömning av "dygnet runt"

- Det finns **ingen process som måste hållas igång**. Apache startar backend vid varje anrop. Webbplatsen och backend är därför tillgängliga när STRATO:s webbserver är det, dygnet runt, utan att somna som Render free.
- Varje API-anrop tar **0,2 till 0,5 sekunder** längre än med en ständigt körande server. Det märks knappt för den här webbplatsen.
- Det som kan stoppa sidan är saker i STRATO:s drift, som underhåll eller driftstörningar, och de ligger utanför vad koden kan påverka.
- STRATO kan begränsa hur många CGI-processer som körs samtidigt. Vid mycket hög trafik (många hundra samtidiga besökare på admin- eller API-sidor) kan det bli långsamt. Vanliga sidor och bilder påverkas inte, eftersom de är vanliga filer.
