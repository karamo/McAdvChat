# McApp with install guide server components and webapp directory
McApp is a single page, client rendered, web application. It should run on every modern browser out there, but you never know. Settings get stored in your browser. If you delete your browser cache, everything is reset.

Rendering on the client, the Raspberry Pi is only sending and receiving UDP, Bluetooth LoRa and TCP web traffic.
- No LightSQL - we have an SD Card that does not handle well constant writes
- no PHP as this means, we need page reloads which is slow and not so elegant in 2025, just static web page is retrieved once
- On initial page load, a dump from the UDP proxy gets sent to your browser. So every time you refresh your browser, you get a fresh reload.
- Try to install the app on your mobile phone by storing it as icon on your home screen

.. please refer to the install guide, as it has screenshots available

### 🧱 `release.sh` – Build & Publis (hidden, not public)

You can install this app, I am constantly updating it, to refelect latest issues and development of MeshCom

How I package my application with the release script:
- is building the WebApp (`npm run build`)
- tar-balls the `/dist` folder
- automatically creates the `release.json` with Metadata (version, date)
- generates a `CHANGELOG.md` that shows what files have been changed
- automaticall increments `Minor`-version (`vX.Y.0`)
- create a new GitHub Release and then pushes `dist.tar.gz` in the public Repo that the whole world has access to

### ⚙️ `install.sh` – Remote Bootstrap Installer

There are scripts, that are stored on GitHub, so they are ever green to be executed on the target machine (e.g. Raspberry Pi Zero 2W):

   curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/mc-install.sh | sudo bash
   curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash



# McApp Pflichtenheft 

Das MashCom McApp-Projekt zerfällt in zwei Komponenten: 
- Frontend, das hübsch, responsive und Mulit-Device fähig ist
    - Soll sich wie eine App benehmen
    - Soll sich an aktuellen Programmierstandards für Webanwendungen orientieren
    - Soll als Progressive Web App (PWA) im Browser installierbar sein
    - Soll als PWA am Telefon Bildschirm abgelegt werden können
    - Soll sowohl am Laptop, wie am iPad als auch am Telefon laufen
    - Muss Dark Mode fähig sein
    - Muss sich an den Bildschirm dynamisch anpassen aka responsive design
    - Muss regelmäßig mit security fixes versorgt werden, da in den Bibliotheken vulnerabilities sein können
       
  - Konnektivität
     - Muss sich mit dem MeshCom Node verbinden können
     - Muss Zeichen ausfiltern, die nicht dem APRS + UTF-8 Protokoll entsprechen.
     - Soll fehlertolerant mit Zeichen sein, die man ohne Probleme filtern kann. 
     - Nachrichten, die sich nicht einfach filtern lassen und illegale Binärdaten enthalten werden hart verworfen, weil es sich um Fehlaussendungen von kaputten E22 Nodes handelt. Es macht keinen Sinn diese anzuzeigen, da sie sowieso Datenmüll enthalten.
     - Muss bei jedem Refresh die Nachrichten vom MeshCom Node neu einlesen
     - Soll sich optional auf MeshCom Activity Seite verbinden können
       
  - Chat View
     - Muss Gruppen und Nutzer filtern können
     - Spam und illegale Nachrichten wird Grupp 9999 zugeordent, weil diese störne die normale Nachrichtenansicht
     - Muss das Zeitsignal empfagen und per Watchdog eine Nachricht ausgeben, wenn aus Wien keine Zeitsignal Paket mehr empfangen werden.
     - Muss ein Suchfeld haben, damit jedes beliebige Call oder jeder beliebige Gruppenchat gefiltert werden kann
     - Das Ziel soll sich dynamisch an die ausgewählte Gruppe oder das ausgewählte Call anpassen
     - Senden Button wird durch dücken von Enter ausgelöst, (newlines sind in APRS nicht erlaubt)
     - Messages landen zuerst in einer Sende Queue, damit der MeshCom Node nicht überfordert wird. Sendeverzögerung aktuell 12 Sekunden, was für verlässliche HF Aussendungen teilweise noch zu schnell ist.
     - Optional: dynamisches Verzögern der Nachrichten, was aber erst mit Bluetoth Zugriff realisiert werden kann, weil wir vom MeshCom Node keine Rückemldung bekommen
     - Anzeige der UDP Messages für einen technischen Look

  - Map View
     - Braucht APRS Grafiken
     - Muss eine durchsuchbare Karte der Nodes haben
     - Karte muss Sat-View und Darkmode haben
     - Beim Klick auf einen Node wird mehr Info angezeigt
     - Nicht geplant: abrufen von dynamischen Daten zu Temperatur, Luftfeuchte und Luftdruck, sowie die weiteren Sensordaten

  - FT - der File Transfer / ist in der App enthalten, jedoch nicht freigeschalten, wegen Zurückweisung von OE1KBC
     - Ein File < 1kB kann in die Drop Zone gezogen werden und wird anschließend übertragen
     - Via Gruppe 9 (HF Only)
     - Empfänger wartet passiv auf übertragene Files
     - Empfänger kann verloren gegangene Übertragungen erneut anfordern
     - Übertragungskodierungmit Base91 Zeichensatz
     - Blöcke werden Reed Solomon kodiert (ist overkill, weil wir sowieso nicht an die rohen LoRa Pakete kommen)
     - Vor Übertragung wird ein Header mit Meta-Information gesendet, damit klar ist, warum soviel Nachrichten kommen, die nicht menschenlesbar sind
     - könnte auch Bilder übertragen, aber dazu reicht uns aktuell nicht die Bandbreite aus (8 Sekunden TX für 149 Bytes)
     
  - Setup Page 
     - Muss Gruppen und PN-Nutzer filtern können
     - Muss automatisch die Verbindungsdaten zum WebSocket ermitteln 
     - Muss Nutzer und Gruppen löschen können (im Browser, nicht am Server)
     - Kein "SAVE Settings" Button, muss Input speichern beim Verlassen der Seite

- Das Server Backend 
    - Läuft auf einem Raspi Pi Zero 2W, weil der besonders stromsparend ist und mehr als ausreichend ist für unsere Zwecke
    - Erhält über UDP alle Nachrichten vom MeshCom node (--extupip 192... und --extudp on nicht vergessen!)
    - Spricht ebenso das Bluetoth Protokoll, für eine stabilere Übertragung mit mehr Daten und mehr Möglichkeiten
    - Implementiert ein Keep-Alive über Bluetooth und verbindet sich automatisch neu, falls die Verbindung verloren geht
    - Setzt die Zeitzone automatisch auf dem MeshCom Node, berücksichtigt Sommer/Winterzeit
    - Muss UTF-8 und APRS Protokoll Checks durchführen, weil es immer wieder illegale Zeichen gibt, die dann zu Abstürzen führen
    - Greift über BLE auf das Device zu, wenn gar nichts mehr geht
    - Kann Skripts und Webseite über bootstrap skript automatisch aktualisieren
    - mheard RSSI und SNR Statistiken erzeugen, wenn BLE verbunden ist
      
- Use Cases:
    - Chat
        - mit Bestätigung "grüner Haken" (für persönliche Chats, aber auch für Gruppenchats)
        - Look and Feel gemäß aktueller ChatApps, damit die Bedienung einfach ist
        - "grauer Haken" für Nachrichten, die erfolgreich in Wien auf dem Server angekommen sind, wenn das Web zugeschaltet ist.
    - Map
        - Alle empfangenen POS Meldungen auf ein Karte mit verschiednenen Darstellungsoptionen anzeigen
    - File Transfer
        - Kann Files übertragen, Bilder wären schön
    - Konfigurationsseite: die Config Seite muss entsprechend aktueller Design Guide Lines gestaltet werden

    - Optional: mehrere Nodes über UDP und http anbinden
        - man kann auf mehreren Nodes den Raspi als Ziel angeben. Somit ist sichergestellt, dass wenn ein Node etwas überhört, wir die Nachricht vom anderen Node bekommen. 2 sind gut, 3 sind natürlich besser. Am Besten "Antennen Diversity" machen, also die Nodes über den Raum verteilen.

Was noch fehlt:
- Auslesen von Umweltsensoren, inklusive Dashboard zur Anzeige der Statistiken
    - Aktuell noch keinen echten UseCase dafür, da man die Umweltsensoren für die Implementierung benötigt
    - Bräuche dazu erst mal LoRa Pakete, wo diese Infos drin stecken


# Vision: McAdvChat - der "MeshCom Advanced Chat"
- Was ich eigentlich vor hatte um das Projekt voran zu bringen

## - “Robuste Echtzeit-Übertragung von Chatnachrichten über fehleranfällige Broadcast-Kanäle mittels Paketfragmentierung, Kompression und Vorwärtsfehlerkorrektur” -

# Disclaimer (oder warum das alles nicht so richtig geht), nach intensiven Forschungen

- Nachrichten müssen dem APRS Protokoll entsprechen

     - APRS messages are designed to be ASCII-compatible, typically 7-bit printable ASCII (decimal 33–126)
     - Control characters (like null \x00, bell \x07, or newline \x0A) and extended 8-bit values (128–255) are not safe
     - Characters outside this range may cause message corruption
     - Allowed: A–Z, a–z, 0–9, common punctuation
     - Not allowed: _binary_data_, _emoji_, _extended_Unicode_

- MeshCom nutzt UTF-8, mit der Besonderheit dass bei der Übertragung über UDP das JSON doppelt stringified ist

- MeshCom kann unsafe Characters übertragen, besonders wenn ein E22-Node mit unsauberer Spannungsversorgung betrieben wird
     - der rohe Byte-Strom kann toxisch sein und sollte dringend mehrere sanitizing Schritte durchlaufen 	

- Kompression bei nur wenigen Bytes bringt leider nur Overhead und keine echte Ersparnis
     - Man müsste ein custom Dicitionary für den HAM-Sprech in DACH aufbauen, um die Entropie zu erhöhen
     - Das Wegschneiden von einem Bit um Base91 effektiv umzusetzen würde wiederum vorausstzen, dass alle 8 Bits genutzt werden können auf der LoRa Strecke

- Die Kodierung von Binärdaten mit Base64 funktioniert und Übertragung funktioniert ebenso

- Reed Solomon (RS) ist lauffähig, würde bei Übertragbarkeit von Binärdaten und Empfang von fehlerbehafteten Paketen sehr viele Vorteile gegenüber der sehr einfachen Hamming Codierung in LoRa bringen. Es fehlt aber der Zugriff auf rohe Pakete, die keine gültige CRC haben.

- RS setzt auf Blöcke mit fixer Größe, wir können also lange Narichten in kurze Chunks, die als Burst ausgesendet werden, verpacken
     - das würde enorme Vorteile bringen, da Messages >70 Zeichen kaum erfolgreich übertragen werden können.

- RS geht davon aus, dass einzelne Bits einer Übertragung umfallen. Dies fängt aber schon der MeshCom Node mit Hamming ab, jedoch bei weitem nicht so Robust und fehlertolerant
     - MeshCom verwirft LoRa Pakete mit Bitfehlern. Daher kann uns hier RS nicht helfen das Paket wiederherzustellen

- Mit Interleaving kann der Verlust von ein oder zwei Chunks, bei Übertragung von mehreren Chunks aufgefangen werden
     - Der Overhead ist immens und daher ist ein erneutes Anfordern des Pakets wesentlich effektiver 

- RS kann Base64 kodiert werden und kommt dann auch mit dem Ausfall von ganzen Chunks zurecht. Aber viele der großen Vorteile werden durch LoRa Protokoll ausgebremst


## Zusammenfassung - Idee für eine robustere Version von MeshCom

- Diese Projekt-Idee beschreibt ein (browserbasiertes) Übertragungsprotokoll für eine Gruppenkommunikation über einen geteilten Broadcast-Kanal mit hoher Fehlerrate. 
- Ziel ist es, Textnachrichten gepuffert zu übertragen, wobei jede Nachricht in kleine, robust übertragbare Pakete aufgeteilt wird.
- Die Nachrichten werden komprimiert, mit Vorwärtsfehlerkorrektur (FEC) versehen und in kleinen, JSON-sicheren Fragmenten (max. 149 Byte pro Paket) gesendet.
- Auf einen Nachrichtendigest (MD5) wird zur Verifikation wird verzeichtet, denn dies stell der MeshCom Node schon bereit. Und Reed-Solomon inkludiert dies schon
- Optionale, selektive Retransmission einzelner Fragmente erhöht die Robustheit bei Paketverlust.
   - würde voraussetzen, dass Pakete bestätigt werden


### Technische Details zu den Überlegungen vorab, die sich aber nicht erfüllen:
	• Kanalmodell: öffentlich geteiltes Medium, definitiv mit Hidden-Node-Problem weil alles bis zu 4 Hops wiederholt wird, hohe Paketfehlerwahrscheinlichkeit bei steigender Payloadgröße
	• Nutzdaten-Paketgröße: maximal 149 Byte; Einschränkung auf UTF-8-safe, APRS Kompatibel, JSON-kompatible Zeichen
	• Chunking: Nachrichten werden in ~10-Byte Payload-Chunks segmentiert
	• Kompression: Realtime-kompatible verlustfreie Kompression (z. B. deflate).
	• Fehlerkorrektur: FEC mit Redundanzfaktor r = 1.2 – also 20% zusätzliche Daten (Reed Solomon)
	• Paketstruktur: [Message Header ID|Payload incl. FEC]
	• Retransmissions: optionale Anforderung von Einzelpaketen bei Erkennung von Lücken im Empfang.

## 2) Statistischer/technischer Unterbau, ein kurzer Einblick in die wissenschaftlich Seite:

Kanalmodellierung (Paketverlustrate in Abhängigkeit von Payload)

Angenommen die Fehlerwahrscheinlichkeit Pe(l) steigt exponentiell mit der Länge l der Nutzdaten:
Pe(l) = 1 - e^(-lamda * l)

Mit typischem lambda circa 0.01 wäre z.B.:
	• 10 Bytes: ~10% Fehlerwahrscheinlichkeit
	• 50 Bytes: ~39%
	• 100 Bytes: ~63%
	• 149 Bytes: ~77%

Diese empirische Modellierung erlaubt uns, die optimale Chunkgröße zu bestimmen: Kompromiss zwischen Effizienz (Overhead ↓) und Erfolgschance (Paketverlust ↓).

FEC-Verfahren: 

Es wird auf etablierte Verfahren wie zum Beispiel Reed-Solomon (für blockbasierte Übertragung) zurückgegriffen. 
	• Reed-Solomon ist robuster als Hamming Code in LoRa
	• kann mehrere Fehler pro Block korrigieren
	• sowohl verteilte als auch gebündelte Fehler verarbeiten kann
	• verlustbehaftete Kanäle wie LoRa oder UDP besser absichert
	• mit Interleaving sogar noch robuster (entspricht einer 90 Grad Rotation der Sendematrix)
 
Ziel ist es, aus k Originalpaketen n Pakete zu erzeugen, sodass die Nachricht rekonstruiierbar ist, solange mindestens k Pakete empfangen werden:

	r=n/k, z.B. r=1.2 (für 20% Overhead)

Erwartete Erfolgsrate: 

Mit p als Erfolgswahrscheinlichkeit pro Paket und k als Mindestanzahl:

	P_success = sum  {i=k}^{n} (n/i) * p^i (1-p)^(n-i)

Das erlaubt gezielte Optimierung von n, k, und r.


## 3) Stand der Forschung (ähnliche Systeme: DVB, DAB, LoRa):

Vergleichbare Systeme

	• DVB-S2: Verwendet LDPC + BCH für FEC, mit sehr hohen Redundanzgraden in schlechten Kanälen.
	• DAB+ (Digital Audio Broadcast): Reed-Solomon auf Applikationsebene, Zeitdiversität.
	• LoRa: Adaptive Data Rate, kleine Pakete, starke FEC mit Hamming/FEC(4,5).

Lessons learned
	• FEC + Interleaving + Fragmentierung sind zentrale Säulen
	• Adaptive Kodierung je nach Kanalbedingungen verbessert Effizienz (nicht getestet)
	• Selective Acknowledgements (SACK) sind essentiell für hohe Verlässlichkeit bei real-time reassembly.

## 4) MeshCom

Es ist wichtig zu betonen, dass wir auf das bestehde MeshCom Protokoll aufsetzen, das wiederum LoRa mit APRS Protokoll als Unterbau hat. Kommunikation mit den aktuellen Kanal Parametern ist bis ca. SNR -17dB möglich. Es kommt trotzdem immer wieder zu Übertragungen die verloregn, da die Übertragung nicht sichergestellt ist und wie oben dargestellt potentiell längere Nachrichten eine höhere Fehleranfälligkeit mit vollständigem Verlust haben. Man sieht auch, dass manche LoRa Frames erneut übertragen werden, hierzu ist dem Autor nichts näher dazu bekannt.

## 5) Verdict, Diskussion und offene Punkte

Stärken
	• saubere Idee – mit exakte Grenzen für MeshCom Spec Payload (149 Byte, APRS / JSON-safe).
	• Echtzeit-fähig, robust und adaptiv - für hohe Kundenzufriedenheit
	• Praktische, realitätsnahe Annahmen (Fehlerraten, Broadcastmodell)
 	• Wissenschaftlich hinterlegt, basierend auf bekannten Modellen und Vorgehensweisen, keine sudo Science.

Schwächen
        • Die Rechnung leider ohne den Zugriff auf die rohen MeshCom LoRa Frames gemacht

Was noch fehlt / was noch definiert werden muss um die Idee tiefer zu legen
	• Chunk Size Tuning Algorithmus – optimal je nach Kanalgüte - für optimalen Kanaldurchsatz
	• Verlustmodell / Paket-Scheduling – Wiederholstrategie und Timeouts?
 		- Wir kann in MeshCom die Kanalgüte gemessen werden?
	• Buffer-Strategie bei Empfang – Wie lange wartet man auf fehlende Pakete? Ab wann reißt der Geduldsfaden
	• Kollisionsvermeidung bei gleichzeitigen Sendern? 
 		- Hidden Node ist bei Max Hop Count 4 ein echtes Problem und führt zu massiven Kollisionen, 
   		- Wie könnte man ein Token oder Zeitslot Modell implementieren?
     		- Macht ein Zeitslot Modell überhaupt Sinn, denn wir haben sehr viele LoRa Geräte, die komplett unaware sind
       		- Müsste als Eingriff in die MeshCom Firmware vermutlich umgesetzt werden ("wird nicht passieren, so Kurt OE1KBC")
	• Security - passiert doch schon am LoRa MeshCom Node (Hamming). Wenn RS zum Einsatz kommt, dann wird dort alles abgesichert

Optionale Erweiterungen
	• Adaptive Redundanz: erhöhe FEC-Anteil bei hohem Paketverlust.
	• Streaming Preview: Darstellung von “User is typing” + live Fragmentanzeige. Das wäre definitiv die coolste Sache.
	• UI-Feedback: grün = empfangen, gelb = erwartet, rot = verloren. Muss definitiv mit rein.

Referenzen:
- privater Austausch mit den Entwicklern im Telegram Chat (kann nicht öffentlich gemacht werden)
- https://icssw.org/grundlegende-spezifikationen/
- https://en.wikipedia.org/wiki/Raptor_code
- https://de.wikipedia.org/wiki/Reed-Solomon-Code
- https://de.wikipedia.org/wiki/JSON
- https://en.wikipedia.org/wiki/Chunking_(computing)
- https://de.wikipedia.org/wiki/Streaming-Protokoll
- https://de.wikipedia.org/wiki/Vorw%C3%A4rtsfehlerkorrektur
- https://de.wikipedia.org/wiki/Kanal_(Informationstheorie)
- https://de.wikipedia.org/wiki/Daten%C3%BCbertragung
- https://files.tapr.org/software_library/aprs/aprsspec/spec/aprs100/APRSProt.pdf
    > for allowed APRS character definition


