# !mheard limit:5
📻 MH: DK5EN-1 @07:58 (12), DB0ED-99 @07:57 (8), DL2JA-1 @07:56 (15)

# !mheard call:DB0ED
📻 DB0ED-99: via direct, -118dBm/-7SNR, 24.9km/526m, 34x/Heltec_V3

# !mheard group:26298  
📻 Group 26298: DG6TOM-11 via DB0ISM-1, DL3NCU-1 via DB0ED-99


# Grundlegende Syntax
mheard [Optionen] [Logfile]

# Typische Parameter:
-n <anzahl>     # Nur die letzten n Einträge anzeigen
-c <rufzeichen> # Nur spezifisches Rufzeichen anzeigen  
-d <digipeater> # Nach Digipeater-Nutzung filtern
-p <port>       # Spezifischen AX.25-Port analysieren
-t <zeit>       # Zeitbereich einschränken
-s              # Sortierung nach verschiedenen Kriterien
-v              # Verbose-Modus mit Details

mheard liest typischerweise den message store aus und erstellt Statistiken wie:

Anzahl gehörter Frames pro Station
Letzte Aktivität
Verwendete Digipeater-Pfade
Signal-Quality-Indikatoren (wenn verfügbar)
