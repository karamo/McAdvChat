# McAdvChat - MeshCom Advanced Chat - Robuste Echtzeit-Übertragung von Chatnachrichten über fehleranfällige Broadcast-Kanäle mittels Paketfragmentierung, Kompression und Vorwärtsfehlerkorrektur

Titel

“Robuste Echtzeit-Übertragung von Chatnachrichten über fehleranfällige Broadcast-Kanäle mittels Paketfragmentierung, Kompression und Vorwärtsfehlerkorrektur”

Zusammenfassung

Dieses Projekt beschreibt ein (browserbasiertes) Übertragungsprotokoll für eine Gruppenkommunikation über einen geteilten Broadcast-Kanal mit hoher Fehlerrate. Ziel ist es, Textnachrichten in Echtzeit zu übertragen, wobei jede Nachricht in kleine, robust übertragbare Pakete aufgeteilt wird. Die Nachrichten werden komprimiert, mit Vorwärtsfehlerkorrektur (FEC) versehen und in kleinen, JSON-sicheren Fragmenten (max. 149 Byte pro Paket) gesendet. Ein Nachrichtendigest (MD5) wird zur Verifikation der vollständigen Nachricht eingesetzt. Optionale, selektive Retransmission einzelner Fragmente erhöht die Robustheit bei Paketverlust.

Technische Details
	•	Kanalmodell: öffentlich geteiltes Medium, kein Hidden-Node-Problem, hohe Paketfehlerwahrscheinlichkeit bei steigender Payloadgröße.
	•	Nutzdaten-Paketgröße: maximal 149 Byte; Einschränkung auf UTF-8-safe, JSON-kompatible Zeichen.
	•	Chunking: Nachrichten werden in ~10-Byte Payload-Chunks segmentiert.
	•	Kompression: Realtime-kompatible verlustfreie Kompression (z. B. LZ-based).
	•	Fehlerkorrektur: FEC mit Redundanzfaktor r = 1.2 – also 20% zusätzliche Daten.
	•	Paketstruktur: [Header|Payload|FEC block] mit Headern für Position, Nachricht-ID, Digest, etc.
	•	Integritätsprüfung: MD5-Hash der komprimierten Originalnachricht zur finalen Überprüfung.
	•	Retransmissions: optionale Anforderung von Einzelpaketen bei Erkennung von Lücken im Empfang.

2) Statistischer/technischer Unterbau, ein kurzer Einblick in die wissenschaftlich Seite:

Kanalmodellierung (Paketverlustrate in Abhängigkeit von Payload)

Angenommen die Fehlerwahrscheinlichkeit Pe(l) steigt exponentiell mit der Länge l der Nutzdaten:
Pe(l) = 1 - e^(-lamda * l)

Mit typischem lambda circa 0.01 wäre z.B.:
	•	10 Bytes: ~10% Fehlerwahrscheinlichkeit
	•	50 Bytes: ~39%
	•	100 Bytes: ~63%
	•	149 Bytes: ~77%

Diese empirische Modellierung erlaubt uns, die optimale Chunkgröße zu bestimmen: Kompromiss zwischen Effizienz (Overhead ↓) und Erfolgschance (Paketverlust ↓).

FEC-Verfahren: 

Es wird auf etablierte Verfahren wie zum Beispiel Reed-Solomon (für blockbasierte Übertragung) oder Raptor Codes (für Streaming) zurückgegriffen. Ziel ist es, aus k Originalpaketen n Pakete zu erzeugen, sodass die Nachricht rekonstruiierbar ist, solange mindestens k Pakete empfangen werden:

r=n/k, z.B. r=1.2 (für 20% Overhead)

Erwartete Erfolgsrate: 

Mit p als Erfolgswahrscheinlichkeit pro Paket und k als Mindestanzahl:

P_success = sum  {i=k}^{n} (n/i) * p^i (1-p)^(n-i)

Das erlaubt gezielte Optimierung von n, k, und r.

3) Stand der Forschung (ähnliche Systeme: DVB, DAB, LoRa):

Vergleichbare Systeme
	•	DVB-S2: Verwendet LDPC + BCH für FEC, mit sehr hohen Redundanzgraden in schlechten Kanälen.
	•	DAB+ (Digital Audio Broadcast): Reed-Solomon auf Applikationsebene, Zeitdiversität.
	•	LoRaWAN: Adaptive Data Rate, kleine Pakete, starke FEC mit Hamming/FEC(4,5).

Lessons learned:
	•	FEC + Interleaving + Fragmentierung sind zentrale Säulen.
	•	Adaptive Kodierung je nach Kanalbedingungen verbessert Effizienz.
	•	Selective Acknowledgements (SACK) sind essentiell für hohe Verlässlichkeit bei real-time reassembly.

4) MeshCom:

Es ist wichtig hier zu betonen, dass wir auf das bestehde MeshCom Protokoll aufsetzen werden, das wiederum LoRaWAN als Unterbau hat. LoRaWan selbst bringt Fehlerkorrektur mit sich, sodass Kommunikation mit den aktuellen Kanal Parametern bis ca. SNR -17dB stattfinden kann. Es kommt trotzdem immer wieder zu Übertragungen, die verloregn gehen in einem Gruppen Chat, da die Übertragung nicht sichergestellt ist und wie oben dargestellt potentiell längere Nachrichten eine höhere Fehleranfälligkeit mit vollständigem Verlust habeb.

5) Mögliche Zielarchitektur und Referenzimplementierung als Tech Demo und Feasability Study / Implementierung in Vue.js 3

Client-Architekturvorschlag
	•	Composables:
	•	useChatStream() – verarbeitet Tippen → Chunking → Compression → FEC → Senden
	•	usePacketReceiver() – sammelt Chunks, prüft Hashes, fordert ggf. Pakete neu an
	•	Worker:
	•	Ein Web Worker für FEC-Berechnungen und Kompression (z. B. via pako (zlib) oder LZMA.js)

 	•	Paketaufbau (Base64 oder JSON-safe-Custom-Encoding)
{
  "id": "msg-uuid",
  "chunk": 5,
  "total": 30,
  "data": "....", // base64 oder hex
  "fec": true,
  "digest": "md5"
}

6) Verdict, Diskussion und offene Punkte

Stärken:
	•	klar definierte und saubere Idee – mit exakte Grenzen für MeshCom Spec Payload (149 Byte, JSON-safe).
	•	Echtzeit-fähig, robust und adaptiv - für hohe Kundenzufriedenheit
	•	Praktische, realitätsnahe Annahmen (Fehlerraten, Broadcastmodell). Wissenschaftlich hinterlegt, basierend auf bekannten Modellen und Vorgehensweisen, keine sudo Science.

Was noch fehlt / was noch definiert werden muss um die Idee tiefer zu legen
	•	Chunk Size Tuning Algorithmus – optimal je nach Kanalgüte - für optimalen Kanaldurchsatz
	•	Verlustmodell / Paket-Scheduling – Wiederholstrategie und Timeouts?
	•	Buffer-Strategie bei Empfang – Wie lange wartet man auf fehlende Pakete? Ab wann reißt der Geduldsfaden
	•	FEC-Typ konkretisieren – Reed-Solomon, Raptor oder XOR-basiert?
	•	Kollisionsvermeidung bei gleichzeitigen Sendern? (Im ersten Schritt haben wir “kein Hidden Node” angenommen, aber evtl. lohnt Token oder Zeitslot. Wird wohl ohne Eingriff in die MeshCom Firmware nicht möglich sein.)
	•	Security? – MD5 ist schnell, aber angreifbar, trotzdem gut genug. Für Integrität okay, aber SHA-256 wäre besser, weil weniger Hash Kollisionen (wobei wir weiße IT sind und nicht bei der grünen IT arbeiten).

Optionale Erweiterungen
	•	Adaptive Redundanz: erhöhe FEC-Anteil bei hohem Paketverlust. 
	•	Streaming Preview: Darstellung von “User is typing” + live Fragmentanzeige. Das wäre definitiv die coolste Sache.
	•	UI-Feedback: grün = empfangen, gelb = erwartet, rot = verloren. Muss definitiv mit rein.

⸻

Wie geht es weiter, was sind die nächsten Schritts:
	•	eine genaue Paketstruktur entwerfen, also den { msg: "payload" }
	•	einen Kompressions- und FEC-Stack auswählen, muss an die vorherschenden Bedingungen angepasst sein
	•	das Ganze als Vue 3 Composable + Worker-Schema umreißen, um den Aufbau zu testen. Es muss auch eine Test Komponente entwickelt werden, bei der sich die Wahrscheinlichkeit von fehlenden Paketen einstellen lässt
	•	Simulation, also Tests, die die Zuverlässigkeit und das zu erwartende Verhalten mit Messungen untermauern 

Ultimativ: die Idee per rapid prototyping bauen und ein erstes Mini-Protokoll als Proof-of-Concept implementieren.

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







 
