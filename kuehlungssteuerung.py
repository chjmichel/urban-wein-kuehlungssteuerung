#!/usr/bin/env python3
"""
Weinkuehlungssteuerung fuer Raspberry Pi Zero W (ARMv6, Raspberry Pi OS Bullseye/Bookworm Lite).

Liest zyklisch zwei DS18B20 1-Wire Temperatursensoren aus, schaltet zwei Relais
ueber GPIO, schreibt alle 15 Minuten einen Datensatz in eine rotierende CSV-Datei
und verschickt stuendlich eine Statusmail inkl. CSV-Anhang.

Alle anpassbaren Werte (Sensor-IDs, GPIOs, Schwellwerte, SMTP-Zugangsdaten) stehen
gesammelt im Abschnitt CONFIG.
"""

from __future__ import annotations

import csv
import logging
import shutil
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
SENSOR_1_ID = "28-000000000001"  # TODO: durch echte ID von Sensor 1 ersetzen
SENSOR_2_ID = "28-000000000002"  # TODO: durch echte ID von Sensor 2 ersetzen

# --- Relais -----------------------------------------------------------------
RELAIS_1_GPIO = 23  # TODO: ggf. anpassen
RELAIS_2_GPIO = 24  # TODO: ggf. anpassen
# Manche Relaisplatinen schalten "active low" (LOW = an). Falls das Relais
# invertiert reagiert, hier auf True stellen.
RELAIS_ACTIVE_HIGH = True

# --- Regel-Logik (Platzhalter, bitte an eigene Anforderungen anpassen) ------
TEMP1_SCHWELLE_AN = 18.0   # Relais 1 einschalten, wenn Temp1 > diesem Wert
TEMP1_SCHWELLE_AUS = 16.0  # Relais 1 ausschalten, wenn Temp1 < diesem Wert (Hysterese)
TEMP2_SCHWELLE_AN = 18.0   # Relais 2 einschalten, wenn Temp2 > diesem Wert
TEMP2_SCHWELLE_AUS = 16.0  # Relais 2 ausschalten, wenn Temp2 < diesem Wert (Hysterese)

# --- Zeitintervalle ----------------------------------------------------------
MESS_INTERVALL_SEK = 60           # 1 Minute
CSV_SCHREIB_INTERVALL_SEK = 15 * 60   # 15 Minuten
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

def sende_status_email(csv_logger: CsvLogger, messwerte_letzte_stunde: list) -> None:
    """Verschickt eine Zusammenfassung der letzten Stunde inkl. aktueller CSV als Anhang."""
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
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        logger.error("E-Mail-Versand fehlgeschlagen (z.B. WLAN-Verlust): %s", exc)


# --------------------------------------------------------------------------- #
# Relais-Regel-Logik (mit einfacher Hysterese)
# --------------------------------------------------------------------------- #

def relais_logik_anwenden(temp: Optional[float], relais: RelaisController, schwelle_an: float, schwelle_aus: float) -> None:
    if temp is None:
        logger.warning("%s: keine gueltige Temperatur, Relaisstatus bleibt unveraendert", relais.name)
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

    # monotone Referenzzeit verwenden, damit Systemzeitsprünge (NTP) die
    # Intervalle nicht durcheinanderbringen
    letzter_csv_schreibvorgang = time.monotonic() - CSV_SCHREIB_INTERVALL_SEK
    letzter_email_versand = time.monotonic() - EMAIL_INTERVALL_SEK

    try:
        while True:
            schleifen_start = time.monotonic()

            try:
                temp1 = sensor1.lesen()
                temp2 = sensor2.lesen()
            except Exception as exc:  # zusaetzliches Sicherheitsnetz
                logger.error("Unerwarteter Fehler beim Sensor-Lesen: %s", exc)
                temp1, temp2 = None, None

            try:
                relais_logik_anwenden(temp1, relais1, TEMP1_SCHWELLE_AN, TEMP1_SCHWELLE_AUS)
                relais_logik_anwenden(temp2, relais2, TEMP2_SCHWELLE_AN, TEMP2_SCHWELLE_AUS)
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

            if time.monotonic() - letzter_csv_schreibvorgang >= CSV_SCHREIB_INTERVALL_SEK:
                try:
                    csv_logger.zeile_schreiben(jetzt, temp1, temp2, relais1.status, relais2.status)
                except Exception as exc:
                    logger.error("Unerwarteter Fehler beim CSV-Schreiben: %s", exc)
                letzter_csv_schreibvorgang = time.monotonic()

            if time.monotonic() - letzter_email_versand >= EMAIL_INTERVALL_SEK:
                try:
                    sende_status_email(csv_logger, stunden_puffer)
                except Exception as exc:
                    logger.error("Unerwarteter Fehler beim E-Mail-Versand: %s", exc)
                letzter_email_versand = time.monotonic()
                stunden_puffer = []

            # Restzeit bis zur naechsten vollen Minute abwarten (nicht blockierend fuer 1h,
            # sondern kurze Schlafphasen, damit das Skript reaktionsfaehig bleibt)
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
