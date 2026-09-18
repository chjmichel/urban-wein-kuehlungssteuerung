#!/usr/bin/env python3
"""
Weinkuehlungssteuerung fuer Raspberry Pi Zero W (ARMv6, Raspberry Pi OS Bullseye/Bookworm Lite).

Liest zyklisch zwei DS18B20 1-Wire Temperatursensoren aus und schaltet zwei Relais
ueber GPIO (Hysterese-Regelung). Zusaetzlich:
  - schreibt alle 5 Minuten einen Datensatz in eine rotierende CSV-Datei,
  - verschickt stuendlich eine Statusmail inkl. CSV-Anhang,
  - pusht die Messwerte und aktiven Schwellwerte an ein Tago.io-Dashboard und
    holt von dort per Fernsteuerung neue Schwellwerte (Sollwerte).

Alle anpassbaren Werte (Sensor-IDs, GPIOs, Schwellwerte, SMTP- und Tago-Zugangsdaten)
stehen gesammelt im Abschnitt CONFIG.
"""

from __future__ import annotations

import csv
import logging
import os
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from statistics import mean
from typing import Optional

# --------------------------------------------------------------------------- #
# CONFIG - hier alle Platzhalter an die eigene Hardware/Umgebung anpassen
# --------------------------------------------------------------------------- #

# --- 1-Wire Sensoren -------------------------------------------------------
# Geraete-IDs findet man mit: ls /sys/bus/w1/devices/
# (Ordner beginnen mit "28-...", das 1-Wire-Interface muss vorher per
#  raspi-config bzw. /boot/config.txt aktiviert werden: dtoverlay=w1-gpio)
W1_BASE_PATH = Path("/sys/bus/w1/devices")
SENSOR_1_ID = "28-000000c8f311"
SENSOR_2_ID = "28-000000cb40fb"

# --- Relais -----------------------------------------------------------------
RELAIS_1_GPIO = 23  # TODO: ggf. anpassen
RELAIS_2_GPIO = 24  # TODO: ggf. anpassen
# Viele Relaisplatinen (v.a. mit Optokoppler) schalten "active low": GPIO LOW = an.
# Fuer solche Platinen muss dieser Wert False sein, sonst sind Anzeige und
# physischer Schaltzustand invertiert. Nur bei "active high"-Platinen auf True.
RELAIS_ACTIVE_HIGH = False

# --- Regel-Logik (Platzhalter, bitte an eigene Anforderungen anpassen) ------
TEMP1_SCHWELLE_AN = 16.5   # Relais 1 einschalten, wenn Temp1 > diesem Wert
TEMP1_SCHWELLE_AUS = 15.5  # Relais 1 ausschalten, wenn Temp1 < diesem Wert (Hysterese)
TEMP2_SCHWELLE_AN = 16.5   # Relais 2 einschalten, wenn Temp2 > diesem Wert
TEMP2_SCHWELLE_AUS = 15.5  # Relais 2 ausschalten, wenn Temp2 < diesem Wert (Hysterese)

# --- Zeitintervalle ----------------------------------------------------------
MESS_INTERVALL_SEK = 300              # 5 Minuten
CSV_SCHREIB_INTERVALL_SEK = 5 * 60   # 5 Minuten
EMAIL_INTERVALL_SEK = 60 * 60         # 1 Stunde
CSV_ROTATIONS_TAGE = 28               # 4 Wochen

# --- CSV ---------------------------------------------------------------------
DATEN_VERZEICHNIS = Path("/home/pi/kuehlungssteuerung/daten")  # TODO: ggf. anpassen
CSV_DATEINAME_PREFIX = "messungen"
CSV_HEADER = [
    "zeitstempel",
    "temperatur_1_c",
    "temperatur_2_c",
    "relais_1_status",
    "relais_2_status",
]

# --- Logging -------------------------------------------------------------------
LOG_DATEI = Path("/home/pi/kuehlungssteuerung/kuehlungssteuerung.log")  # TODO: ggf. anpassen
LOG_LEVEL = logging.INFO

# --- SMTP / E-Mail -------------------------------------------------------------
SMTP_SERVER = "smtp.example.com"      # TODO: SMTP-Server eintragen
SMTP_PORT = 587                       # TODO: z.B. 587 (STARTTLS) oder 465 (SSL)
SMTP_USE_SSL = False                  # True fuer Port 465, False fuer STARTTLS (587)
SMTP_LOGIN = "user@example.com"       # TODO: SMTP-Login eintragen
SMTP_PASSWORT = "changeme"            # TODO: SMTP-Passwort eintragen (besser: ueber Umgebungsvariable laden)
EMAIL_ABSENDER = "user@example.com"   # TODO: Absenderadresse eintragen
EMAIL_EMPFAENGER = ["empfaenger@example.com"]  # TODO: Empfaengerliste eintragen
EMAIL_BETREFF_PREFIX = "Weinkuehlung Status"

# --- Tago.io -------------------------------------------------------------------
# Daten werden per HTTPS an die Tago.io Data-API gepusht. Voraussetzung:
# In Tago.io ein Device (Connector "Custom HTTPS") anlegen und dessen
# Device-Token hier eintragen (Device -> Tokens).
TAGO_AKTIV = True                       # auf False setzen, um den Push abzuschalten
# Region muss zum Tago-Account passen: US = api.tago.io, EU = api.eu-w1.tago.io
TAGO_API_URL = "https://api.eu-w1.tago.io/data"
# Token NICHT im Code speichern: aus der Umgebungsvariable TAGO_DEVICE_TOKEN laden.
# Auf dem Pi wird sie ueber /etc/kuehlungssteuerung.env gesetzt (siehe systemd-Unit),
# lokal zum Testen z.B. mit: export TAGO_DEVICE_TOKEN="..."
TAGO_DEVICE_TOKEN = os.environ.get("TAGO_DEVICE_TOKEN", "changeme")
TAGO_PUSH_INTERVALL_SEK = 5 * 60        # wie oft an Tago gesendet wird (5 Minuten)
TAGO_TIMEOUT_SEK = 30                   # Netzwerk-Timeout (WLAN-Verlust abfangen)

# --- Tago.io Sollwert-Fernsteuerung -------------------------------------------
# Die Schwellwerte koennen aus der Ferne ueber Tago.io gesetzt werden. Dazu im
# Tago-Dashboard je ein Eingabe-Widget (z.B. "Input Form" oder "Slider") anlegen,
# das in die unten genannten Variablen des Devices schreibt. Der Pi holt diese
# Werte regelmaessig ab. Die Werte in der CONFIG oben dienen als Startwerte,
# solange in Tago noch kein Sollwert gesetzt wurde.
TAGO_SOLLWERTE_AKTIV = True             # auf False setzen, um die Fernsteuerung abzuschalten
TAGO_SOLLWERT_INTERVALL_SEK = 5 * 60    # wie oft Sollwerte von Tago geholt werden (5 Minuten)
TAGO_VAR_SW_AN_1 = "sollwert_an_1"      # Variablenname im Dashboard fuer Einschaltschwelle Sensor 1
TAGO_VAR_SW_AUS_1 = "sollwert_aus_1"    # Ausschaltschwelle Sensor 1
TAGO_VAR_SW_AN_2 = "sollwert_an_2"      # Einschaltschwelle Sensor 2
TAGO_VAR_SW_AUS_2 = "sollwert_aus_2"    # Ausschaltschwelle Sensor 2

# --------------------------------------------------------------------------- #
# Logging Setup
# --------------------------------------------------------------------------- #

LOG_DATEI.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DATEI, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("kuehlungssteuerung")


# --------------------------------------------------------------------------- #
# Zeitsteuerung
# --------------------------------------------------------------------------- #

class Intervall:
    """Einfacher, wiederkehrender Timer auf Basis der monotonen Uhr.

    ``faellig()`` liefert True, sobald die eingestellte Dauer seit dem letzten
    Ausloesen verstrichen ist, und startet die Wartezeit dann automatisch neu.
    Die monotone Uhr wird verwendet, damit Systemzeitspruenge (z.B. durch NTP)
    die Intervalle nicht durcheinanderbringen.
    """

    def __init__(self, dauer_sek: float, sofort_faellig: bool = True):
        self.dauer_sek = dauer_sek
        # sofort_faellig=True -> beim ersten Aufruf gleich ausloesen
        self._letzter = time.monotonic() - dauer_sek if sofort_faellig else time.monotonic()

    def faellig(self) -> bool:
        if time.monotonic() - self._letzter >= self.dauer_sek:
            self._letzter = time.monotonic()
            return True
        return False


# --------------------------------------------------------------------------- #
# Sensorik
# --------------------------------------------------------------------------- #

class DS18B20Sensor:
    """Liest einen DS18B20 1-Wire Sensor direkt ueber das sysfs-Kernelinterface."""

    def __init__(self, sensor_id: str, name: str):
        self.name = name
        self.pfad = W1_BASE_PATH / sensor_id / "w1_slave"

    def lesen(self) -> Optional[float]:
        """Gibt die Temperatur in Grad Celsius zurueck, oder None bei Fehler."""
        try:
            inhalt = self.pfad.read_text().strip().splitlines()
            if len(inhalt) < 2 or not inhalt[0].strip().endswith("YES"):
                logger.warning("%s: CRC-Check fehlgeschlagen / Sensor nicht bereit", self.name)
                return None
            position = inhalt[1].find("t=")
            if position == -1:
                logger.warning("%s: Temperaturwert im sysfs-Output nicht gefunden", self.name)
                return None
            milligrad = int(inhalt[1][position + 2:])
            temperatur = milligrad / 1000.0
            if temperatur == 85.0:
                # 85.0C ist der Power-On-Reset-Wert des DS18B20 -> Messfehler
                logger.warning("%s: Power-On-Reset-Wert (85.0 C) erhalten, verwerfe Messung", self.name)
                return None
            return temperatur
        except FileNotFoundError:
            logger.error("%s: Sensor nicht gefunden unter %s (Verkabelung/1-Wire aktiv?)", self.name, self.pfad)
            return None
        except (ValueError, OSError) as exc:
            logger.error("%s: Fehler beim Lesen des Sensors: %s", self.name, exc)
            return None


# --------------------------------------------------------------------------- #
# Relais
# --------------------------------------------------------------------------- #

class RelaisController:
    """Kapselt die GPIO-Ansteuerung eines Relais ueber gpiozero."""

    def __init__(self, gpio_pin: int, name: str, active_high: bool = True):
        self.name = name
        self._device = None
        try:
            from gpiozero import OutputDevice
            self._device = OutputDevice(gpio_pin, active_high=active_high, initial_value=False)
        except Exception as exc:  # pragma: no cover - Hardwareabhaengig
            logger.error("%s: Relais auf GPIO %s konnte nicht initialisiert werden: %s", name, gpio_pin, exc)

    @property
    def status(self) -> bool:
        if self._device is None:
            return False
        return bool(self._device.value)

    def einschalten(self) -> None:
        self._setzen(True)

    def ausschalten(self) -> None:
        self._setzen(False)

    def _setzen(self, an: bool) -> None:
        if self._device is None:
            logger.error("%s: Relais nicht initialisiert, Schaltbefehl ignoriert", self.name)
            return
        try:
            if an:
                self._device.on()
            else:
                self._device.off()
        except Exception as exc:  # pragma: no cover - Hardwareabhaengig
            logger.error("%s: Fehler beim Schalten des Relais: %s", self.name, exc)

    def schliessen(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# CSV-Logging mit Rotation
# --------------------------------------------------------------------------- #

@dataclass
class CsvLogger:
    verzeichnis: Path
    prefix: str
    rotations_tage: int
    aktuelle_datei: Path = field(init=False)
    start_datum: datetime = field(init=False)

    def __post_init__(self):
        self.verzeichnis.mkdir(parents=True, exist_ok=True)
        self.start_datum = datetime.now()
        self.aktuelle_datei = self._dateiname_fuer(self.start_datum)
        self._sicherstellen_datei_existiert(self.aktuelle_datei)

    def _dateiname_fuer(self, datum: datetime) -> Path:
        return self.verzeichnis / f"{self.prefix}_{datum.strftime('%Y-%m-%d')}.csv"

    def _sicherstellen_datei_existiert(self, pfad: Path) -> None:
        if not pfad.exists():
            try:
                with pfad.open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(CSV_HEADER)
                logger.info("Neue CSV-Datei angelegt: %s", pfad)
            except OSError as exc:
                logger.error("Konnte CSV-Datei %s nicht anlegen: %s", pfad, exc)

    def _rotation_pruefen(self) -> None:
        alter = datetime.now() - self.start_datum
        if alter >= timedelta(days=self.rotations_tage):
            logger.info(
                "CSV-Rotation nach %s Tagen: bisherige Datei %s wird archiviert",
                self.rotations_tage, self.aktuelle_datei,
            )
            self.start_datum = datetime.now()
            self.aktuelle_datei = self._dateiname_fuer(self.start_datum)
            self._sicherstellen_datei_existiert(self.aktuelle_datei)

    def zeile_schreiben(self, zeitstempel: datetime, temp1, temp2, relais1_status: bool, relais2_status: bool) -> None:
        self._rotation_pruefen()
        try:
            with self.aktuelle_datei.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    zeitstempel.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{temp1:.2f}" if temp1 is not None else "NA",
                    f"{temp2:.2f}" if temp2 is not None else "NA",
                    "AN" if relais1_status else "AUS",
                    "AN" if relais2_status else "AUS",
                ])
            logger.info("CSV-Zeile geschrieben in %s", self.aktuelle_datei)
        except OSError as exc:
            logger.error("Fehler beim Schreiben der CSV-Zeile: %s", exc)


# --------------------------------------------------------------------------- #
# E-Mail-Versand
# --------------------------------------------------------------------------- #

def sende_status_email(csv_logger: CsvLogger, messwerte_letzte_stunde: list) -> bool:
    """Verschickt eine Zusammenfassung der letzten Stunde inkl. aktueller CSV als Anhang.

    Gibt True zurueck, wenn die E-Mail versendet wurde, sonst False (z.B. bei WLAN-Verlust).
    """
    if not messwerte_letzte_stunde:
        zusammenfassung = "Keine gueltigen Messwerte in der letzten Stunde verfuegbar."
    else:
        temp1_werte = [m["temp1"] for m in messwerte_letzte_stunde if m["temp1"] is not None]
        temp2_werte = [m["temp2"] for m in messwerte_letzte_stunde if m["temp2"] is not None]
        letzter = messwerte_letzte_stunde[-1]
        zusammenfassung = (
            f"Zeitraum: {messwerte_letzte_stunde[0]['zeit'].strftime('%Y-%m-%d %H:%M')} "
            f"bis {letzter['zeit'].strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Aktueller Wert Temperatur 1: {letzter['temp1']}\n"
            f"Aktueller Wert Temperatur 2: {letzter['temp2']}\n"
            f"Relais 1: {'AN' if letzter['relais1'] else 'AUS'}\n"
            f"Relais 2: {'AN' if letzter['relais2'] else 'AUS'}\n\n"
        )
        if temp1_werte:
            zusammenfassung += f"Durchschnitt Temperatur 1 (letzte Stunde): {mean(temp1_werte):.2f} C\n"
        else:
            zusammenfassung += "Durchschnitt Temperatur 1: keine Daten\n"
        if temp2_werte:
            zusammenfassung += f"Durchschnitt Temperatur 2 (letzte Stunde): {mean(temp2_werte):.2f} C\n"
        else:
            zusammenfassung += "Durchschnitt Temperatur 2: keine Daten\n"

    nachricht = MIMEMultipart()
    nachricht["From"] = EMAIL_ABSENDER
    nachricht["To"] = ", ".join(EMAIL_EMPFAENGER)
    nachricht["Subject"] = f"{EMAIL_BETREFF_PREFIX} - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    nachricht.attach(MIMEText(zusammenfassung, "plain", "utf-8"))

    try:
        anhang_pfad = csv_logger.aktuelle_datei
        if anhang_pfad.exists():
            with anhang_pfad.open("rb") as f:
                anhang = MIMEApplication(f.read(), Name=anhang_pfad.name)
            anhang["Content-Disposition"] = f'attachment; filename="{anhang_pfad.name}"'
            nachricht.attach(anhang)
    except OSError as exc:
        logger.error("Konnte CSV-Anhang nicht lesen: %s", exc)

    try:
        if SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30)
        with server:
            if not SMTP_USE_SSL:
                server.starttls()
            server.login(SMTP_LOGIN, SMTP_PASSWORT)
            server.sendmail(EMAIL_ABSENDER, EMAIL_EMPFAENGER, nachricht.as_string())
        logger.info("Status-E-Mail erfolgreich versendet")
        return True
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        logger.error("E-Mail-Versand fehlgeschlagen (z.B. WLAN-Verlust): %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Tago.io Push
# --------------------------------------------------------------------------- #

def sende_an_tago(temp1: Optional[float], temp2: Optional[float],
                  relais1_status: bool, relais2_status: bool,
                  schwellen: Optional[dict] = None) -> None:
    """Pusht die aktuellen Messwerte per HTTPS an die Tago.io Data-API.

    Wird ``schwellen`` uebergeben, werden auch die gerade aktiven Schwellwerte
    (aktiv_*) mitgeschickt, damit das Dashboard den tatsaechlich angewendeten
    Zustand anzeigt.

    Fehler (z.B. WLAN-Verlust) werden protokolliert, aber nicht weitergereicht,
    damit die Steuerung ununterbrochen weiterlaeuft.
    """
    if not TAGO_AKTIV:
        return

    # Nur gueltige Messwerte senden; None-Werte (Sensorfehler) auslassen.
    nutzlast = []
    if temp1 is not None:
        nutzlast.append({"variable": "temperatur_1", "value": round(temp1, 2), "unit": "C"})
    if temp2 is not None:
        nutzlast.append({"variable": "temperatur_2", "value": round(temp2, 2), "unit": "C"})
    nutzlast.append({"variable": "relais_1", "value": 1 if relais1_status else 0})
    nutzlast.append({"variable": "relais_2", "value": 1 if relais2_status else 0})

    # Aktuell angewendete Schwellwerte zurueckmelden (fuer Anzeige-Widgets im Dashboard).
    if schwellen is not None:
        nutzlast.append({"variable": "aktiv_an_1", "value": schwellen["an1"], "unit": "C"})
        nutzlast.append({"variable": "aktiv_aus_1", "value": schwellen["aus1"], "unit": "C"})
        nutzlast.append({"variable": "aktiv_an_2", "value": schwellen["an2"], "unit": "C"})
        nutzlast.append({"variable": "aktiv_aus_2", "value": schwellen["aus2"], "unit": "C"})

    try:
        import requests  # lazy import, damit das Skript ohne die Lib startbar bleibt
    except ImportError:
        logger.error("Tago-Push: Bibliothek 'requests' nicht installiert (sudo apt install python3-requests)")
        return

    try:
        antwort = requests.post(
            TAGO_API_URL,
            json=nutzlast,
            headers={"Device-Token": TAGO_DEVICE_TOKEN, "Content-Type": "application/json"},
            timeout=TAGO_TIMEOUT_SEK,
        )
        if 200 <= antwort.status_code < 300:
            logger.info("Tago-Push erfolgreich (%d Variablen)", len(nutzlast))
        else:
            logger.error("Tago-Push fehlgeschlagen: HTTP %s - %s", antwort.status_code, antwort.text[:200])
    except Exception as exc:  # requests.RequestException u.a. (Netzwerk/WLAN-Verlust)
        logger.error("Tago-Push fehlgeschlagen (z.B. WLAN-Verlust): %s", exc)


def _tago_letzter_wert(requests_modul, variable: str) -> Optional[float]:
    """Holt den zuletzt gesetzten Wert einer Variable aus dem Tago-Datenspeicher.

    Gibt None zurueck, wenn (noch) kein Wert gesetzt ist oder ein Fehler auftrat.
    """
    try:
        antwort = requests_modul.get(
            TAGO_API_URL,
            params={"variable": variable, "query": "last_value"},
            headers={"Device-Token": TAGO_DEVICE_TOKEN},
            timeout=TAGO_TIMEOUT_SEK,
        )
        if antwort.status_code != 200:
            logger.error("Tago-Sollwert '%s': HTTP %s - %s", variable, antwort.status_code, antwort.text[:200])
            return None
        ergebnis = antwort.json().get("result") or []
        if not ergebnis:
            return None  # im Dashboard wurde fuer diese Variable noch kein Wert gesetzt
        return float(ergebnis[0]["value"])
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("Tago-Sollwert '%s': ungueltige Antwort: %s", variable, exc)
        return None
    except Exception as exc:  # requests.RequestException u.a. (Netzwerk/WLAN-Verlust)
        logger.error("Tago-Sollwert '%s' nicht abrufbar (z.B. WLAN-Verlust): %s", variable, exc)
        return None


def hole_sollwerte_von_tago(schwellen: dict) -> dict:
    """Aktualisiert die Schwellwerte aus dem Tago-Dashboard.

    Uebernimmt nur gueltige Werte und stellt sicher, dass pro Sensor AUS < AN
    bleibt (Hysterese). Bei Fehlern/fehlenden Werten bleiben die bisherigen
    Schwellwerte unveraendert.
    """
    if not TAGO_SOLLWERTE_AKTIV:
        return schwellen

    try:
        import requests  # lazy import, damit das Skript ohne die Lib startbar bleibt
    except ImportError:
        logger.error("Tago-Sollwerte: Bibliothek 'requests' nicht installiert (sudo apt install python3-requests)")
        return schwellen

    zuordnung = {
        "an1": TAGO_VAR_SW_AN_1,
        "aus1": TAGO_VAR_SW_AUS_1,
        "an2": TAGO_VAR_SW_AN_2,
        "aus2": TAGO_VAR_SW_AUS_2,
    }
    neu = dict(schwellen)
    for schluessel, variable in zuordnung.items():
        wert = _tago_letzter_wert(requests, variable)
        if wert is not None:
            neu[schluessel] = wert

    # Hysterese-Invariante pro Sensor pruefen: AUS muss unter AN liegen.
    for sensor, aus_key, an_key in (("Sensor1", "aus1", "an1"), ("Sensor2", "aus2", "an2")):
        if neu[aus_key] >= neu[an_key]:
            logger.error(
                "%s: ungueltige Tago-Sollwerte (AUS %.2f >= AN %.2f), behalte bisherige Werte",
                sensor, neu[aus_key], neu[an_key],
            )
            neu[aus_key] = schwellen[aus_key]
            neu[an_key] = schwellen[an_key]

    if neu != schwellen:
        logger.info(
            "Schwellwerte aktualisiert (Tago): S1 AN=%.2f AUS=%.2f | S2 AN=%.2f AUS=%.2f",
            neu["an1"], neu["aus1"], neu["an2"], neu["aus2"],
        )
    return neu


# --------------------------------------------------------------------------- #
# Relais-Regel-Logik (mit einfacher Hysterese)
# --------------------------------------------------------------------------- #

def relais_logik_anwenden(temp: Optional[float], relais: RelaisController, schwelle_an: float,
                          schwelle_aus: float, initial: bool = False) -> None:
    if temp is None:
        logger.warning("%s: keine gueltige Temperatur, Relaisstatus bleibt unveraendert", relais.name)
        return
    if initial:
        # Beim ersten Durchlauf (Start/Neustart) einen definierten Zustand setzen:
        # ab der AUS-Schwelle (also auch im Band zwischen AUS und AN) einschalten,
        # nur darunter ausschalten. Danach greift die normale Hysterese.
        if temp >= schwelle_aus:
            relais.einschalten()
            logger.info("%s: Startzustand EIN (Temp %.2f C >= %.2f C)", relais.name, temp, schwelle_aus)
        else:
            relais.ausschalten()
            logger.info("%s: Startzustand AUS (Temp %.2f C < %.2f C)", relais.name, temp, schwelle_aus)
        return
    if temp > schwelle_an and not relais.status:
        relais.einschalten()
        logger.info("%s: eingeschaltet (Temp %.2f C > %.2f C)", relais.name, temp, schwelle_an)
    elif temp < schwelle_aus and relais.status:
        relais.ausschalten()
        logger.info("%s: ausgeschaltet (Temp %.2f C < %.2f C)", relais.name, temp, schwelle_aus)


# --------------------------------------------------------------------------- #
# Hauptprogramm
# --------------------------------------------------------------------------- #

def main() -> None:
    logger.info("Kuehlungssteuerung startet")

    sensor1 = DS18B20Sensor(SENSOR_1_ID, "Sensor1")
    sensor2 = DS18B20Sensor(SENSOR_2_ID, "Sensor2")
    relais1 = RelaisController(RELAIS_1_GPIO, "Relais1", RELAIS_ACTIVE_HIGH)
    relais2 = RelaisController(RELAIS_2_GPIO, "Relais2", RELAIS_ACTIVE_HIGH)
    csv_logger = CsvLogger(DATEN_VERZEICHNIS, CSV_DATEINAME_PREFIX, CSV_ROTATIONS_TAGE)

    stunden_puffer: list = []  # Messwerte seit der letzten E-Mail, fuer die Zusammenfassung
    # Startzustand je Sensor getrennt setzen: ein dauerhaft defekter Sensor darf den
    # anderen (funktionierenden) Kanal nicht im hysteresefreien Startmodus festhalten.
    erster_durchlauf_1 = True
    erster_durchlauf_2 = True

    # Schwellwerte zur Laufzeit halten (Startwerte aus CONFIG). Werden ggf. per
    # Tago.io-Fernsteuerung aktualisiert, ohne das Programm neu zu starten.
    schwellen = {
        "an1": TEMP1_SCHWELLE_AN,
        "aus1": TEMP1_SCHWELLE_AUS,
        "an2": TEMP2_SCHWELLE_AN,
        "aus2": TEMP2_SCHWELLE_AUS,
    }

    # monotone Referenzzeit verwenden, damit Systemzeitsprünge (NTP) die
    # Intervalle nicht durcheinanderbringen
    sollwert_timer = Intervall(TAGO_SOLLWERT_INTERVALL_SEK)
    csv_timer = Intervall(CSV_SCHREIB_INTERVALL_SEK)
    tago_push_timer = Intervall(TAGO_PUSH_INTERVALL_SEK)
    # E-Mail bewusst NICHT sofort beim Start senden: sonst kaeme nach jedem
    # (Neu-)Start eine Zusammenfassung mit nur einem Messpunkt.
    email_timer = Intervall(EMAIL_INTERVALL_SEK, sofort_faellig=False)

    try:
        while True:
            schleifen_start = time.monotonic()

            try:
                temp1 = sensor1.lesen()
                temp2 = sensor2.lesen()
            except Exception as exc:  # zusaetzliches Sicherheitsnetz
                logger.error("Unerwarteter Fehler beim Sensor-Lesen: %s", exc)
                temp1, temp2 = None, None

            # Schwellwerte ggf. aus dem Tago-Dashboard aktualisieren (vor der Relaislogik,
            # damit auch der Startzustand bereits die aktuellen Sollwerte verwendet).
            if sollwert_timer.faellig():
                try:
                    schwellen = hole_sollwerte_von_tago(schwellen)
                except Exception as exc:
                    logger.error("Unerwarteter Fehler beim Sollwert-Abruf: %s", exc)

            try:
                relais_logik_anwenden(temp1, relais1, schwellen["an1"], schwellen["aus1"], initial=erster_durchlauf_1)
                relais_logik_anwenden(temp2, relais2, schwellen["an2"], schwellen["aus2"], initial=erster_durchlauf_2)
                # Startzustand je Sensor als gesetzt markieren, sobald dieser Sensor einen
                # gueltigen Wert geliefert hat (nur dann hat relais_logik_anwenden ihn angewendet).
                if erster_durchlauf_1 and temp1 is not None:
                    erster_durchlauf_1 = False
                if erster_durchlauf_2 and temp2 is not None:
                    erster_durchlauf_2 = False
            except Exception as exc:
                logger.error("Unerwarteter Fehler in der Relaislogik: %s", exc)

            jetzt = datetime.now()
            stunden_puffer.append({
                "zeit": jetzt,
                "temp1": temp1,
                "temp2": temp2,
                "relais1": relais1.status,
                "relais2": relais2.status,
            })

            if csv_timer.faellig():
                try:
                    csv_logger.zeile_schreiben(jetzt, temp1, temp2, relais1.status, relais2.status)
                except Exception as exc:
                    logger.error("Unerwarteter Fehler beim CSV-Schreiben: %s", exc)

            if tago_push_timer.faellig():
                try:
                    sende_an_tago(temp1, temp2, relais1.status, relais2.status, schwellen)
                except Exception as exc:
                    logger.error("Unerwarteter Fehler beim Tago-Push: %s", exc)

            if email_timer.faellig():
                erfolg = False
                try:
                    erfolg = sende_status_email(csv_logger, stunden_puffer)
                except Exception as exc:
                    logger.error("Unerwarteter Fehler beim E-Mail-Versand: %s", exc)
                # Puffer nur leeren, wenn die E-Mail wirklich raus ist – sonst gingen die
                # Messwerte der letzten Stunde bei WLAN-Verlust verloren.
                if erfolg:
                    stunden_puffer = []

            # Bis zum naechsten Messzyklus warten. Es wird nur die Restzeit geschlafen,
            # damit die tatsaechliche Auslesefrequenz nahe an MESS_INTERVALL_SEK bleibt.
            verbrauchte_zeit = time.monotonic() - schleifen_start
            schlafzeit = max(0.0, MESS_INTERVALL_SEK - verbrauchte_zeit)
            time.sleep(schlafzeit)

    except KeyboardInterrupt:
        logger.info("Beende Kuehlungssteuerung (KeyboardInterrupt)")
    finally:
        relais1.schliessen()
        relais2.schliessen()


if __name__ == "__main__":
    main()
