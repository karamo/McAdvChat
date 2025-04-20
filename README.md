# McApp initial release with draft install guide server components and webapp directory
McApp is a single page, client rendered web application. It should run on every browser out there, but you never know.
Settings get stored in your browser. If you delete your browser cache, you need to setup the connection parameters again.
Everything is rendered on the client, the raspberry pi is only sending and receiving UDP LoRa and TCP web traffic.
- No LightSQL, no PHP, just static web pages.
- On initial page load, a memory dump from the UDP proxy gets sent to the browser. So every time you refresh your browser, you get a fresh reload.

# McApp Pflichtenheft 

Mein persönliches MashCom McApp-Projekt zerfällt in zwei Komponenten: 
- Frontend, das hübsch, responsive und Mulit-Device fähig ist
    - Soll sich wie eine App benehmen
    - Soll sich an aktuellen Programmierstandards für Webanwendungen orientieren
    - Soll als Progressive Web App (PWA) im Browser installierbar sein
    - Soll als PWA am Telefon Bildschirm abgelegt werden können
    - Soll sowohl am Laptop, wie am iPad als auch am Telefon laufen
    	- Mobile Phone Support derzeit noch eingeschränkt, da hierfür noch das Layout optimiert werden muss
     - Muss Dark Mode fähig sein
     - Muss sich an den Bildschirm dynamisch anpassen
       
  - Konnektivität
     - Muss sich mit dem MeshCom Node verbinden können
     - Muss bei jedem Refresh die Nachrichten vom MeshCom Node neu einlesen
     - Soll sich optional auf MeshCom Activity Seite verbinden können
       
  - Chat View
     - Muss Gruppen und Nutzer filtern können
     - Muss ein Suchfeld haben, damit jedes beliebige Call oder jeder beliebige Gruppenchat gefiltert werden kann
     - Das Ziel soll sich dynamisch an die ausgewählte Gruppe oder das ausgewählte Call anpassen
     - Senden Button wird durch dücken von Enter ausgelöst
     - Messages landen zuerst in einer Sende Queue. Optional: dynamisches Verzögern der Nachrichten
     - Anzeige der UDP Messages für einen technischen Look

  - Map View
     - Braucht APRS Grafiken
     - Muss eine durchsuchbare Karte der Nodes haben
     - Karte muss Sat-View und Darkmode haben
     - Beim Klick auf einen Node wird mehr Info angezeigt
     - Nicht geplant: abrufen von dynamischen Daten zu Temperatur, Luftfeuchte und Luftdruck, sowie die weiteren Sensordaten
     
  - Setup Page 
     - Muss Gruppen und Nutzer filtern können
     - Muss Nutzer und Gruppen löschen können (im Browser, nicht am Server)
     - Kein "SAVE Settings" Button, muss Input annehmen beim Verlassen der Seite

- Das Server Backend 
    - Läuft auf einem Raspi Pi Zero 2W, weil der besonders stromsparend ist und mehr als ausreichend ist für unsere Zwecke
    - Erhält über UDP alle Nachrichten vom MeshCom node (--extupip 192... und --extudp on nicht vergessen!)
    - Greift über BLE auf das Device zu, wenn gar nichts mehr geht
    	- Wird nicht über http auf das MeshCom Device zugreifen, weil wir BLE implementieren werden
      
- Use Cases:
    - Chat
        - mit Bestätigung (für persönliche Chats, aber auch für Gruppenchats)
        - Look and Feel gemäß aktueller ChatApps, damit die Bedienung einfach ist
    - Map
        - Alle empfangenen POS Meldungen auf ein Karte mit verschiednenen Darstellungsoptionen anzeigen
    - Konfigurationsseite: die Config Seite muss entsprechend aktueller Design Guide Lines gestaltet werden
      



- Optional: mehrere Nodes über UDP und http anbinden

Was noch fehlt:
- Schön wäre, wenn der UDP Proxy auch noch BLE sprechen lernt und so an mehr Informationen im MeshCom Node kommt
- mheard RSSI und SNR auslesen und Statistiken erzeugen
- APRS Icon auf für den Chat verwenden
- Auslesen von Umweltsensoren, inklusive Dashboard zur Anzeige der Statistiken
- Projekt sollte sich auf einer neuen SD-Karte selbst Bootstrappen mit einem Shellscript
	- Darauf wird derzeit verzichtet, denn es werden Nutzer-Probleme mit den SSL-Zertifikaten erwartet.
 	- Es gibt leider keinen einfachen und sauberen Weg gibt mit den SSL-Zertifikaten im Heimnetzwerk.


# Ausblick / Vision: McAdvChat - der "MeshCom Advanced Chat"

# - “Robuste Echtzeit-Übertragung von Chatnachrichten über fehleranfällige Broadcast-Kanäle mittels Paketfragmentierung, Kompression und Vorwärtsfehlerkorrektur” -

# Disclaimer (oder warum das alles nicht so richtig geht), nach intensiven Forschungen im Mockup
 - Nachrichten müssen dem APRS Protokoll entsprechen
 	• APRS messages are designed to be ASCII-compatible, typically 7-bit printable ASCII (decimal 33–126)
	• Control characters (like null \x00, bell \x07, or newline \x0A) and extended 8-bit values (128–255) are not safe
	• Characters outside this range may cause message corruption
	→ Allowed: A–Z, a–z, 0–9, common punctuation
	→ Not allowed: _binary_data_, _emoji_, _extended_Unicode_

 - MeshCom nutzt UTF-8, mit der Besonderheit dass bei der Übertragung über UDP das JSON doppelt stringified ist
 - MeshCom kann unsafe Characters übertragen, besonders wenn ein E22-Node mit unsauberer Spannungsversorgung betrieben wird
 	- der rohe Byte-Strom kann also toxisch sein und sollte dringend mehrere Sanitizing Schritte durchlaufen 	

 - Kompression bei nur wenigen Bytes bringt leider nur Overhead und keine echte Ersparnis
 	- Man müsste ein custom Dicitionary für den HAM-Sprech in DACH aufbauen, um die Entropie zu erhöhen
 - Das Wegschneiden von einem Bit um Base91 effektiv umzusetzen würde wiederum vorausstzen, dass alle 8 Bits genutzt werden können auf der LoRaWAN Strecke
 - Die Kodierung von Binärdaten mit Base64 funktioniert und Übertragung funktioniert
 - Reed Solomon (RS) ist lauffähig, würde bei Übertragbarkeit von Binärdaten und Empfang von fehlerbehafteten Paketen sehr viele Vorteile gegenüber der sehr einfachen Hamming Codierung in LoRaWAN bringen
 - RS setzt auf Blöcke mit fixer Größe, wir können also lange Narichten in kurze Chungs, die als Burst ausgesendet werden, verpacken
 - RS geht davon aus, dass einzelne Bits einer Übertragung umfallen. Dies fängt aber schon der MeshCom Node mit Hamming ab, jedoch bei weitem nicht so Robust und fehlertolerant
 - MeshCom verwirft LoRa Pakete mit Bitfehlern. Daher kann uns hier RS nicht helfen das Paket wiederherzustellen
 - Mit Interleaving kann der Verlust von ein oder zwei Chunks, bei Übertragung von mehreren Chunks aufgefangen werden
 	- Der Overhead ist immens und daher ist ein erneutes Anfordern des Pakets wesentlich effektiver 
 - RS kann Base64 kodiert werden und kommt dann auch mit dem Ausfall von ganzen Chunks zurecht. Aber viele der großen Vorteile werden durch LoRaWAN Protokoll ausgebremst


Zusammenfassung - Idee für eine robustere Version von MeshCom

- Diese Projekt-Idee beschreibt ein (browserbasiertes) Übertragungsprotokoll für eine Gruppenkommunikation über einen geteilten Broadcast-Kanal mit hoher Fehlerrate. 
- Ziel ist es, Textnachrichten in Echtzeit zu übertragen, wobei jede Nachricht in kleine, robust übertragbare Pakete aufgeteilt wird.
- Die Nachrichten werden komprimiert, mit Vorwärtsfehlerkorrektur (FEC) versehen und in kleinen, JSON-sicheren Fragmenten (max. 149 Byte pro Paket) gesendet.
- Auf einen Nachrichtendigest (MD5) wird zur Verifikation wird verzeichtet, denn dies stell der MeshCom Node schon bereit.
- Optionale, selektive Retransmission einzelner Fragmente erhöht die Robustheit bei Paketverlust.

Technische Details

	• Kanalmodell: öffentlich geteiltes Medium, vorerst kein Hidden-Node-Problem, hohe Paketfehlerwahrscheinlichkeit bei steigender Payloadgröße
	• Nutzdaten-Paketgröße: maximal 149 Byte; Einschränkung auf UTF-8-safe, APRS Kompatibel, JSON-kompatible Zeichen
	• Chunking: Nachrichten werden in ~10-Byte Payload-Chunks segmentiert
	• Kompression: Realtime-kompatible verlustfreie Kompression (z. B. deflate).
	• Fehlerkorrektur: FEC mit Redundanzfaktor r = 1.2 – also 20% zusätzliche Daten (Reed Solomon)
	• Paketstruktur: [Message Header ID|Payload incl. FEC]
	• Retransmissions: optionale Anforderung von Einzelpaketen bei Erkennung von Lücken im Empfang.

2) Statistischer/technischer Unterbau, ein kurzer Einblick in die wissenschaftlich Seite:

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
	✅ Reed-Solomon ist robuster als Hamming Code in LoRaWAN
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


3) Stand der Forschung (ähnliche Systeme: DVB, DAB, LoRaWAN):

Vergleichbare Systeme

	• DVB-S2: Verwendet LDPC + BCH für FEC, mit sehr hohen Redundanzgraden in schlechten Kanälen.
	• DAB+ (Digital Audio Broadcast): Reed-Solomon auf Applikationsebene, Zeitdiversität.
	• LoRaWAN: Adaptive Data Rate, kleine Pakete, starke FEC mit Hamming/FEC(4,5).

Lessons learned
	• FEC + Interleaving + Fragmentierung sind zentrale Säulen
	• Adaptive Kodierung je nach Kanalbedingungen verbessert Effizienz (nicht getestet)
	• Selective Acknowledgements (SACK) sind essentiell für hohe Verlässlichkeit bei real-time reassembly.

4) MeshCom

Es ist wichtig hier zu betonen, dass wir auf das bestehde MeshCom Protokoll aufsetzen, das wiederum LoRaWAN mit APRS Protokoll als Unterbau hat. LoRaWan selbst bringt Fehlerkorrektur mit sich, sodass Kommunikation mit den aktuellen Kanal Parametern bis ca. SNR -17dB stattfinden kann. Es kommt trotzdem immer wieder zu Übertragungen die verloregn, da die Übertragung nicht sichergestellt ist und wie oben dargestellt potentiell längere Nachrichten eine höhere Fehleranfälligkeit mit vollständigem Verlust haben. Man sieht auch, dass manche LoRa Frames erneut übertragen werden, hierzu ist dem Autor nichts näher dazu bekannt.

5) Verdict, Diskussion und offene Punkte

Stärken

	• saubere Idee – mit exakte Grenzen für MeshCom Spec Payload (149 Byte, APRS / JSON-safe).
	• Echtzeit-fähig, robust und adaptiv - für hohe Kundenzufriedenheit
	• Praktische, realitätsnahe Annahmen (Fehlerraten, Broadcastmodell). 
 	• Wissenschaftlich hinterlegt, basierend auf bekannten Modellen und Vorgehensweisen, keine sudo Science.

Was noch fehlt / was noch definiert werden muss um die Idee tiefer zu legen

	• Chunk Size Tuning Algorithmus – optimal je nach Kanalgüte - für optimalen Kanaldurchsatz
	• Verlustmodell / Paket-Scheduling – Wiederholstrategie und Timeouts?
 		- Wir kann in MeshCom die Kanalgüte gemessen werden?
	• Buffer-Strategie bei Empfang – Wie lange wartet man auf fehlende Pakete? Ab wann reißt der Geduldsfaden
	• FEC-Typ: XOR ist zu schwach, Raptor noch checken
	• Kollisionsvermeidung bei gleichzeitigen Sendern? 
 		- Im ersten Schritt haben wir “kein Hidden Node” angenommen, 
   		- Wie könnte man ein Token oder Zeitslot Modell implementieren?
     		- Macht ein Zeitslot Modell überhaupt Sinn, denn wir haben sehr viele LoRa Geräte, die komplett unaware sind
       		- Müsste als Eingriff in die MeshCom Firmware vermutlich umgesetzt werden ("wird nicht passieren")
	• Security - passiert doch schon am LoRa MeshCom Node (Hamming). Wenn RS zum Einsatz kommt, dann wird dort alles abgesichert

Optionale Erweiterungen

	• Adaptive Redundanz: erhöhe FEC-Anteil bei hohem Paketverlust.
	• Streaming Preview: Darstellung von “User is typing” + live Fragmentanzeige. Das wäre definitiv die coolste Sache.
	• UI-Feedback: grün = empfangen, gelb = erwartet, rot = verloren. Muss definitiv mit rein.

Referenzen:
- https://icssw.org/grundlegende-spezifikationen/
- https://en.wikipedia.org/wiki/Raptor_code
- https://de.wikipedia.org/wiki/Reed-Solomon-Code
- https://de.wikipedia.org/wiki/JSON
- https://en.wikipedia.org/wiki/Chunking_(computing)
- https://de.wikipedia.org/wiki/Streaming-Protokoll
- https://de.wikipedia.org/wiki/Vorw%C3%A4rtsfehlerkorrektur
- https://de.wikipedia.org/wiki/Kanal_(Informationstheorie)
- https://de.wikipedia.org/wiki/Daten%C3%BCbertragung







 
