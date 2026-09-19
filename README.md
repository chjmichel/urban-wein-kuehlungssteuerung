# urban-wein-kuehlungssteuerung

Weinkühlungssteuerung für einen Raspberry Pi (getestet auf Pi 4, Raspberry Pi OS
Bookworm). Das Programm liest zwei DS18B20-Temperatursensoren aus und schaltet über
zwei Relais die Kühlpumpen zweier Kreisläufe – geregelt per Hysterese. Zusätzlich
werden die Messwerte protokolliert (CSV), stündlich per E-Mail versendet und an ein
Tago.io-Dashboard gesendet, von dem aus sich die Sollwerte fernsteuern lassen.

## Funktionsüberblick

- **Temperaturmessung**: zwei DS18B20 1-Wire-Sensoren über das sysfs-Kernelinterface.
- **Relaissteuerung**: zwei Relais über GPIO, angesteuert mit **lgpio** (dieselbe
  libgpiod-Schnittstelle wie das Kommandozeilen-Tool `gpioset`).
- **Hysterese-Regelung**: pro Kreislauf getrennte Ein-/Ausschaltschwelle (Totband),
  damit die Pumpen nicht ständig takten.
- **CSV-Logging**: eine Datei pro Tag (`daten/messungen_YYYY-MM-DD.csv`); Dateien
  älter als `CSV_ROTATIONS_TAGE` (28 Tage) werden automatisch gelöscht.
- **Status-E-Mail**: stündlich, mit Zusammenfassung der letzten Stunde und aktueller
  CSV als Anhang.
- **Tago.io**: Push der Messwerte + aktiver Schwellwerte; Abruf neuer Sollwerte aus
  dem Dashboard (Fernsteuerung ohne Neustart).

## Hardware

- Raspberry Pi (getestet: Pi 4), Raspberry Pi OS Bookworm.
- 2× DS18B20 1-Wire-Sensor (1-Wire per `raspi-config` bzw. `dtoverlay=w1-gpio` aktivieren).
- 2× Relais an GPIO 23 und 24. Typische Optokoppler-Platinen schalten **active-low**
  (GPIO LOW = Relais an) → `RELAIS_ACTIVE_HIGH = False`.

## Installation / Deployment

```bash
# In das Zielverzeichnis klonen
git clone https://github.com/chjmichel/urban-wein-kuehlungssteuerung.git /home/pi/kuehlungssteuerung
cd /home/pi/kuehlungssteuerung

# Abhängigkeiten
sudo apt install python3-lgpio python3-requests

# Zugangsdaten/Schalter anlegen (siehe Abschnitt Umgebungsvariablen)
sudo nano /etc/kuehlungssteuerung.env

# systemd-Dienst einrichten
sudo cp kuehlungssteuerung.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kuehlungssteuerung

# Status/Log prüfen
systemctl status kuehlungssteuerung
journalctl -u kuehlungssteuerung -f
```

> **Wichtig:** git-Befehle im Projektordner **ohne `sudo`** ausführen, sonst gehören
> Dateien danach `root` und der als `urban` laufende Dienst kann nicht mehr in Log/CSV
> schreiben (`PermissionError`). Reparatur: `sudo chown -R urban:urban /home/pi/kuehlungssteuerung`.

## Verzeichnis- und Dateistruktur

Arbeitsverzeichnis `/home/pi/kuehlungssteuerung`:

| Eintrag | Herkunft | Bedeutung |
|---|---|---|
| `kuehlungssteuerung.py` | Git | Das Hauptprogramm. |
| `kuehlungssteuerung.service` | Git | Vorlage der systemd-Unit (aktive Kopie unter `/etc/systemd/system/`). |
| `requirements.txt` | Git | Python-Abhängigkeiten: `lgpio`, `requests`. |
| `README.md` | Git | Diese Dokumentation. |
| `.git/` | Git | Git-Metadaten. Nie manuell anfassen; git-Befehle ohne `sudo`. |
| `daten/` | zur Laufzeit | Eine CSV pro Tag (`messungen_YYYY-MM-DD.csv`); alte Dateien werden gelöscht. |
| `kuehlungssteuerung.log` | zur Laufzeit | Logdatei (zusätzlich zu `journalctl`). |

Außerhalb des Projektordners:

| Pfad | Bedeutung |
|---|---|
| `/etc/kuehlungssteuerung.env` | Zugangsdaten & Schalter (kein Git). |
| `/etc/systemd/system/kuehlungssteuerung.service` | Aktive Dienst-Definition. |

## Umgebungsvariablen (`/etc/kuehlungssteuerung.env`)

Diese Datei liegt bewusst **außerhalb des Repos**, damit keine Geheimnisse ins Git
gelangen. Die systemd-Unit lädt sie über `EnvironmentFile=`. Beispiel:

```ini
# SMTP / E-Mail
SMTP_SERVER=smtp.strato.de
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_LOGIN=absender@example.com
SMTP_PASSWORT=geheim
EMAIL_ABSENDER=absender@example.com
EMAIL_EMPFAENGER=a@example.com,b@example.com   # mehrere durch Komma trennen, KEINE Klammern/Anführungszeichen

# Tago.io
TAGO_DEVICE_TOKEN=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Optionale Schalter
TEST_MODUS=false      # true = schnelle Reaktionsintervalle (~1 min) zum Testen
RELAIS_DEBUG=false    # true = GPIO-Zustand (gpio_read + pinctrl) nach jedem Schalten loggen
```

| Variable | Bedeutung |
|---|---|
| `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USE_SSL` | Mailserver (SSL z. B. Port 465). |
| `SMTP_LOGIN`, `SMTP_PASSWORT` | Zugangsdaten des Absenderkontos. |
| `EMAIL_ABSENDER` | Absenderadresse (Default: `SMTP_LOGIN`). |
| `EMAIL_EMPFAENGER` | Empfänger, mehrere per Komma. |
| `TAGO_DEVICE_TOKEN` | Device-Token des Tago.io-Geräts. |
| `TEST_MODUS` | `true` → Mess-/Sollwert-/Push-Intervall 60 s statt 5 min. |
| `RELAIS_DEBUG` | `true` → physischen GPIO-Zustand nach jedem Schalten loggen. |

Ohne konfigurierte SMTP-Daten wird der E-Mail-Versand übersprungen (kein Fehler);
ohne `TAGO_DEVICE_TOKEN` schlägt der Tago-Push mit einer Log-Meldung fehl.

## Konfiguration im Code

Feste Parameter stehen gesammelt im Abschnitt `CONFIG` in
[`kuehlungssteuerung.py`](kuehlungssteuerung.py):

| Konstante | Bedeutung |
|---|---|
| `SENSOR_1_ID`, `SENSOR_2_ID` | 1-Wire-IDs (`ls /sys/bus/w1/devices/`, beginnen mit `28-`). |
| `GPIO_CHIP` | gpiochip-Index (Pi 4/Zero i. d. R. `0`, entspricht `gpioset -c 0 ...`). |
| `RELAIS_1_GPIO`, `RELAIS_2_GPIO` | GPIO-Pins der Relais (23 / 24). |
| `RELAIS_ACTIVE_HIGH` | `False` für active-low-Platinen (LOW = an). |
| `TEMP1/2_SCHWELLE_AN`, `..._AUS` | Ein-/Ausschaltschwellen der Hysterese (AUS < AN!). |
| `CSV_SCHREIB_INTERVALL_SEK` | CSV-Schreibtakt (fix 5 min). |
| `EMAIL_INTERVALL_SEK` | E-Mail-Takt (fix 1 h). |
| `CSV_ROTATIONS_TAGE` | Aufbewahrungsdauer der CSV-Dateien (28 Tage). |
| `TAGO_API_URL` | Region muss zum Account passen: US `api.tago.io`, EU `api.eu-w1.tago.io`. |

### Hysterese

Pro Kreislauf gilt: **einschalten**, wenn die Temperatur über die AN-Schwelle steigt;
**ausschalten**, wenn sie unter die AUS-Schwelle fällt. Dazwischen (im Totband) bleibt
der Zustand unverändert. Es muss immer `AUS < AN` gelten – ungültige Sollwerte aus
Tago werden verworfen und die bisherigen behalten.

## Betrieb & Fehlersuche

```bash
# Neustart nach Code-Änderung
cd /home/pi/kuehlungssteuerung && git pull      # ohne sudo!
sudo systemctl restart kuehlungssteuerung

# Live-Log
journalctl -u kuehlungssteuerung -f

# Relais manuell testen (Dienst vorher stoppen)
sudo systemctl stop kuehlungssteuerung
gpioget -c 0 23           # aktuellen Pegel lesen
gpioset -c 0 23=1         # HIGH setzen (hält bis Strg+C)
```

- **Relais schaltet nicht / falsch herum**: `RELAIS_ACTIVE_HIGH` prüfen; mit
  `RELAIS_DEBUG=true` den physischen Pegel im Log gegenprüfen.
- **Reaktion auf Dashboard-Änderung dauert lange**: im Normalbetrieb bis ~10 min
  (5-min-Intervalle). Zum Testen `TEST_MODUS=true` setzen.
- **Keine E-Mail**: SMTP-Daten in der env-Datei prüfen; `EMAIL_EMPFAENGER` ohne
  Klammern/Anführungszeichen.
- **`status=217/USER`**: der in der Unit angegebene Benutzer existiert nicht.

## Sicherheitshinweise

- Keine Zugangsdaten/Token im Code oder in Git – ausschließlich in
  `/etc/kuehlungssteuerung.env`.
- Wird ein Token versehentlich veröffentlicht, in Tago.io neu generieren.
