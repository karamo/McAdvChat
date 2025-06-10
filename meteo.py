#!/usr/bin/env python3
"""
Wetter-Service für Ham Radio LoRa Integration - HYBRID VERSION
DWD BrightSky als Primärquelle + OpenMeteo für fehlende Parameter
Intelligente Daten-Fusion für optimale Genauigkeit
"""

import requests
import json
import logging
import sys
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Any, List
import time

VERSION="v0.46.0"

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('weather_service')

has_console = sys.stdout.isatty()


class WeatherServiceError(Exception):
    """Custom Exception für Wetter-Service Fehler"""
    pass

class WeatherService:
    """
    Hybrid Wetter-Service: DWD primär + OpenMeteo für fehlende Parameter
    Optimale Datenqualität durch intelligente Fusion
    """
    
    def __init__(self, lat = 48.4031, lon = 11.7497, stat_name = "Freising", max_age_minutes: int = 30):
        self.freising_lat = lat
        self.freising_lon = lon
        self.stat_name = stat_name
        
        # Maximales Alter der Wetterdaten in Minuten
        self.max_age_minutes = max_age_minutes
        
        # Request timeout und retry config
        self.timeout = 10
        self.max_retries = 2
        
        logger.info(f"WeatherService initialisiert für {self.stat_name} {self.freising_lat}/{self.freising_lon}, Hybrid-Modus: DWD + OpenMeteo")
    
    def get_weather_data(self) -> Dict[str, Any]:
        """
        Hybrid-Methode: DWD primär, OpenMeteo für fehlende Parameter
        """
        logger.debug("Starte Hybrid-Wetterabfrage...")
        
        # 1. Versuche DWD BrightSky zu laden
        dwd_data = None
        try:
            logger.debug("📡 Lade DWD BrightSky Daten...")
            dwd_data = self._get_brightsky_weather()
            
            # Zeitvalidierung für DWD
            age_check = self._validate_data_age(dwd_data)
            if age_check["valid"]:

                if not self._has_valid_core_data(dwd_data):
                    logger.warning("❌ DWD liefert None für Kernparameter → Fallback auf OpenMeteo")
                    dwd_data = None  # DWD verwerfen
                else:
                    logger.debug(f"✅ DWD-Daten verfügbar und aktuell ({age_check['age_minutes']:.1f} Min alt)")

            elif not self._has_valid_core_data(dwd_data):
                logger.debug("⚠️  DWD liefert None-Werte → Fallback auf OpenMeteo")
                dwd_data = None
            else:
                logger.debug(f"⚠️  DWD-Daten zu alt: {age_check['reason']}")
                dwd_data = None  # Verwerfe alte DWD-Daten
                
        except Exception as e:
            logger.warning(f"❌ DWD BrightSky nicht verfügbar: {e}")
            dwd_data = None
        
        # 2. Lade OpenMeteo Daten (immer als Backup/Ergänzung)
        openmeteo_data = None
        try:
            logger.debug("📡 Lade OpenMeteo Daten...")
            openmeteo_data = self._get_openmeteo_weather()
            logger.debug("✅ OpenMeteo-Daten verfügbar")
        except Exception as e:
            logger.warning(f"❌ OpenMeteo nicht verfügbar: {e}")
            openmeteo_data = None
        
        # 3. Daten-Fusion: Bestes aus beiden Welten
        if dwd_data is None and openmeteo_data is None:
            # Kompletter Fehler
            return {
                "error": "Alle Wetter-APIs nicht verfügbar",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "location": f"{self.freising_lat}/{self.freising_lon}"
            }
        elif dwd_data is None:
            # Nur OpenMeteo verfügbar
            logger.debug("🔄 Nutze ausschließlich OpenMeteo")
            openmeteo_data["data_source"] = "OpenMeteo (Fallback)"
            openmeteo_data["timestamp"] = datetime.now(timezone.utc).isoformat()
            return openmeteo_data
        elif openmeteo_data is None:
            # Nur DWD verfügbar
            logger.debug("🔄 Nutze ausschließlich DWD (OpenMeteo nicht verfügbar)")
            dwd_data["data_source"] = "DWD_BrightSky (ohne Ergänzung)"
            dwd_data["timestamp"] = datetime.now(timezone.utc).isoformat()
            return dwd_data
        else:
            # Beide verfügbar - FUSION!
            logger.debug("🔄 Führe Daten-Fusion durch: DWD primär + OpenMeteo Ergänzung")
            fused_data = self._fuse_weather_data(dwd_data, openmeteo_data)
            fused_data["timestamp"] = datetime.now(timezone.utc).isoformat()
            return fused_data

    def _has_valid_core_data(self, weather_data: Dict[str, Any]) -> bool:
        """
        Prüfe ob die wichtigsten Wetterdaten verfügbar sind
        Wenn DWD None für Kernparameter liefert → Fallback auf OpenMeteo
        """
        # Definiere kritische Kernparameter
        core_params = [
            ("temperatur_celsius", "Temperatur"),
            ("luftdruck_hpa", "Luftdruck")
        ]
        
        invalid_params = []
        
        for param, param_name in core_params:
            value = weather_data.get(param)
            if value is None:
                invalid_params.append(param_name)
                logger.debug(f"❌ DWD {param_name}: None")
            else:
                logger.debug(f"✅ DWD {param_name}: {value}")
        
        if invalid_params:
            logger.debug(f"❌ DWD liefert None für kritische Parameter: {', '.join(invalid_params)}")
            return False
        
        logger.debug("✅ DWD Kernparameter sind gültig")
        return True
    


    def _fuse_weather_data(self, dwd_data: Dict[str, Any], openmeteo_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Intelligente Daten-Fusion: DWD hat Priorität, OpenMeteo ergänzt fehlende Werte
        """
        logger.debug("🧩 Starte intelligente Daten-Fusion...")
        
        # Basis: DWD-Daten kopieren
        fused = dwd_data.copy()
        
        # Liste der kritischen Parameter die ergänzt werden können
        parameters_to_supplement = [
            ("windgeschwindigkeit_kmh", "Wind-Geschwindigkeit"),
            ("windrichtung_grad", "Wind-Richtung"),
            ("wolkenbedeckung_prozent", "Wolkenbedeckung"),
            ("sichtweite_meter", "Sichtweite"),
            ("niederschlag_mm", "Niederschlag"),
            ("luftfeuchtigkeit_prozent", "Luftfeuchtigkeit"),
        ]
        
        supplemented_params = []
        kept_dwd_params = []
        
        for param, param_name in parameters_to_supplement:
            dwd_value = dwd_data.get(param)
            openmeteo_value = openmeteo_data.get(param)
            
            if dwd_value is None and openmeteo_value is not None:
                # DWD hat keinen Wert, OpenMeteo ergänzt
                fused[param] = openmeteo_value
                supplemented_params.append(param_name)
                logger.debug(f"  ➕ {param_name}: {openmeteo_value} (von OpenMeteo ergänzt)")
            elif dwd_value is not None:
                # DWD-Wert behalten
                kept_dwd_params.append(f"{param_name}: {dwd_value}")
                logger.debug(f"  ✅ {param_name}: {dwd_value} (DWD behalten)")
            else:
                # Beide None
                logger.debug(f"  ⚠️  {param_name}: Nicht verfügbar")
        
        # Datenquellen-Info zusammenstellen
        if supplemented_params:
            source_info = f"DWD_BrightSky + OpenMeteo ({', '.join(supplemented_params)})"
            logger.debug(f"✅ Fusion abgeschlossen: {len(supplemented_params)} Parameter von OpenMeteo ergänzt")
        else:
            source_info = "DWD_BrightSky (vollständig)"
            logger.debug("✅ Fusion abgeschlossen: DWD-Daten waren vollständig")
        
        fused["data_source"] = source_info
        fused["supplemented_parameters"] = supplemented_params
        
        # Qualitätsbewertung
        fused["data_quality"] = self._assess_data_quality(fused)
        
        return fused
    
    def _assess_data_quality(self, weather_data: Dict[str, Any]) -> str:
        """
        Bewerte die Qualität der fusionierten Daten
        """
        # Kritische Parameter prüfen
        critical_params = [
            "temperatur_celsius", "luftfeuchtigkeit_prozent", "luftdruck_hpa",
            "windgeschwindigkeit_kmh", "wolkenbedeckung_prozent"
        ]
        
        available_critical = sum(1 for param in critical_params if weather_data.get(param) is not None)
        total_critical = len(critical_params)
        
        quality_score = (available_critical / total_critical) * 100
        
        if quality_score >= 100:
            return "Exzellent (alle Parameter)"
        elif quality_score >= 80:
            return "Sehr gut (fast alle Parameter)"
        elif quality_score >= 60:
            return "Gut (wichtigste Parameter)"
        elif quality_score >= 40:
            return "Ausreichend (Grundparameter)"
        else:
            return "Unvollständig (kritische Parameter fehlen)"
    
    def _validate_data_age(self, weather_data: Dict[str, Any]) -> Dict[str, Any]:
        """Validierung des Datenalters"""
        messzeitpunkt_str = weather_data.get("messzeitpunkt")
        
        if not messzeitpunkt_str or messzeitpunkt_str == "unbekannt":
            return {
                "valid": False,
                "age_minutes": float('inf'),
                "reason": "Kein Messzeitpunkt verfügbar"
            }
        
        try:
            if messzeitpunkt_str.endswith('+00:00'):
                measurement_time = datetime.fromisoformat(messzeitpunkt_str)
            elif 'T' in messzeitpunkt_str and len(messzeitpunkt_str) == 16:
                naive_time = datetime.fromisoformat(messzeitpunkt_str)
                measurement_time = naive_time.replace(tzinfo=timezone.utc) - timedelta(hours=2)
            else:
                measurement_time = datetime.fromisoformat(messzeitpunkt_str.replace('Z', '+00:00'))
            
            now = datetime.now(timezone.utc)
            age_delta = now - measurement_time
            age_minutes = age_delta.total_seconds() / 60
            
            if age_minutes < 0:
                return {
                    "valid": False,
                    "age_minutes": abs(age_minutes),
                    "reason": f"Daten sind {abs(age_minutes):.1f} Min in der Zukunft (Forecast)"
                }
            
            is_valid = age_minutes <= self.max_age_minutes
            return {
                "valid": is_valid,
                "age_minutes": age_minutes,
                "reason": f"Daten sind {age_minutes:.1f} Min alt" + ("" if is_valid else f" (> {self.max_age_minutes} Min)")
            }
            
        except (ValueError, TypeError) as e:
            return {
                "valid": False,
                "age_minutes": float('inf'),
                "reason": f"Ungültiger Messzeitpunkt: {e}"
            }
    
    def _calculate_humidity_from_dewpoint(self, temperature_c: float, dewpoint_c: float) -> Optional[int]:
        """Berechne relative Luftfeuchtigkeit aus Temperatur und Taupunkt"""
        try:
            a, b = 17.27, 237.7
            alpha_temp = (a * temperature_c) / (b + temperature_c)
            es_temp = 6.112 * math.exp(alpha_temp)
            alpha_dew = (a * dewpoint_c) / (b + dewpoint_c)
            es_dew = 6.112 * math.exp(alpha_dew)
            relative_humidity = (es_dew / es_temp) * 100
            return round(max(0, min(100, relative_humidity)))
        except (ValueError, ZeroDivisionError, OverflowError):
            return None
    
    def _get_brightsky_weather(self) -> Dict[str, Any]:
        """DWD Bright Sky API"""
        urls_to_try = [
            {
                "url": "https://api.brightsky.dev/current_weather",
                "params": {"lat": self.freising_lat, "lon": self.freising_lon},
                "name": "current_weather"
            },
            {
                "url": "https://api.brightsky.dev/weather",
                "params": {
                    "lat": self.freising_lat, 
                    "lon": self.freising_lon,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "last": 24
                },
                "name": "weather_recent"
            }
        ]
        
        for endpoint in urls_to_try:
            try:
                response = self._make_request(endpoint["url"], endpoint["params"])
                data = response.json()
                
                if not isinstance(data, dict) or "weather" not in data:
                    continue
                
                weather_records = data["weather"]
                
                if isinstance(weather_records, dict):
                    current = weather_records
                elif isinstance(weather_records, list) and len(weather_records) > 0:
                    current = self._find_most_recent_record(weather_records)
                else:
                    continue
                
                if not current:
                    continue
                
                result = self._process_brightsky_record(current, data)
                if result:
                    return result
                    
            except Exception as e:
                logger.debug(f"BrightSky {endpoint['name']} fehlgeschlagen: {e}")
                continue
        
        raise WeatherServiceError("Alle BrightSky Endpunkte fehlgeschlagen")
    
    def _find_most_recent_record(self, weather_records: list) -> Optional[Dict]:
        """Finde den aktuellsten Datensatz"""
        if not weather_records:
            return None
        
        try:
            def parse_timestamp(record):
                ts_str = record.get("timestamp", "")
                if ts_str:
                    return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                return datetime.min.replace(tzinfo=timezone.utc)
            
            sorted_records = sorted(weather_records, key=parse_timestamp, reverse=True)
            now = datetime.now(timezone.utc)
            
            for record in sorted_records:
                try:
                    record_time = parse_timestamp(record)
                    if record_time <= now:
                        return record
                except:
                    continue
            
            return sorted_records[0]
        except Exception:
            return weather_records[0]
    
    def _process_brightsky_record(self, current: Dict, full_data: Dict) -> Optional[Dict[str, Any]]:
        """Verarbeite einen BrightSky Wetter-Record"""
        # Station Info
        station_name = "unbekannt"
        if "sources" in full_data and full_data["sources"] and len(full_data["sources"]) > 0:
            first_source = full_data["sources"][0]
            if isinstance(first_source, dict):
                station_name = first_source.get("station_name", "unbekannt")
        
        # Basis-Daten extrahieren
        temperature = self._safe_float(current.get("temperature"))
        dewpoint = self._safe_float(current.get("dew_point"))
        relative_humidity_raw = current.get("relative_humidity")
        
        # Luftfeuchtigkeit
        luftfeuchtigkeit = None
        if relative_humidity_raw is not None:
            luftfeuchtigkeit = self._safe_int(relative_humidity_raw)
        elif temperature is not None and dewpoint is not None:
            luftfeuchtigkeit = self._calculate_humidity_from_dewpoint(temperature, dewpoint)
        
        result = {
            "temperatur_celsius": temperature,
            "luftfeuchtigkeit_prozent": luftfeuchtigkeit,
            "luftdruck_hpa": self._safe_float(current.get("pressure_msl")),
            "windgeschwindigkeit_kmh": self._safe_float(current.get("wind_speed")),
            "windrichtung_grad": self._safe_int(current.get("wind_direction")),
            "wolkenbedeckung_prozent": self._safe_int(current.get("cloud_cover")),
            "sichtweite_meter": self._safe_int(current.get("visibility")),
            "niederschlag_mm": self._safe_float(current.get("precipitation")),
            "bedingung": current.get("condition", "unbekannt"),
            "dwd_station": station_name,
            "messzeitpunkt": current.get("timestamp", "unbekannt"),
            "taupunkt_celsius": dewpoint
        }
        
        return result
    
    def _get_openmeteo_weather(self) -> Dict[str, Any]:
        """Open-Meteo API"""
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.freising_lat,
            "longitude": self.freising_lon,
            "current": "temperature_2m,relative_humidity_2m,pressure_msl,cloud_cover,wind_speed_10m,wind_direction_10m,visibility,precipitation",
            "timezone": "Europe/Berlin"
        }
        
        response = self._make_request(url, params)
        data = response.json()

        if has_console:
            print("openmeteo debug:",data)
        
        if "current" not in data:
            raise WeatherServiceError("Keine aktuellen Open-Meteo-Daten verfügbar")
        
        current = data["current"]
        
        return {
            "temperatur_celsius": self._safe_float(current.get("temperature_2m")),
            "luftfeuchtigkeit_prozent": self._safe_int(current.get("relative_humidity_2m")),
            "luftdruck_hpa": self._safe_float(current.get("pressure_msl")),
            "windgeschwindigkeit_kmh": self._safe_float(current.get("wind_speed_10m")),
            "windrichtung_grad": self._safe_int(current.get("wind_direction_10m")),
            "wolkenbedeckung_prozent": self._safe_int(current.get("cloud_cover")),
            "sichtweite_meter": self._safe_int(current.get("visibility")),
            "niederschlag_mm": self._safe_float(current.get("precipitation")),
            "bedingung": "automatisch",
            "dwd_station": "Open-Meteo Modell",
            "messzeitpunkt": current.get("time", "unbekannt")
        }
    
    def _make_request(self, url: str, params: Dict) -> requests.Response:
        """Robuste HTTP-Request mit Retry-Logic"""
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(url, params=params, timeout=self.timeout,
                                     headers={"User-Agent": "HamRadio-WeatherService/1.0"})
                response.raise_for_status()
                return response
            except requests.exceptions.Timeout:
                if attempt == self.max_retries:
                    raise WeatherServiceError("Request Timeout")
                time.sleep(1)
            except requests.exceptions.RequestException as e:
                if attempt == self.max_retries:
                    raise WeatherServiceError(f"HTTP-Fehler: {e}")
                time.sleep(1)
    
    def _safe_float(self, value: Any) -> Optional[float]:
        """Sichere Float-Konvertierung"""
        try:
            return float(value) if value is not None else None
        except (ValueError, TypeError):
            return None
    
    def _safe_int(self, value: Any) -> Optional[int]:
        """Sichere Int-Konvertierung"""
        try:
            return int(float(value)) if value is not None else None
        except (ValueError, TypeError):
            return None
    
    def _calculate_cloud_coverage_description(self, cloud_percent: Optional[int]) -> str:
        """Berechne Wolkenbedeckung in Achteln (/8) und Beschreibung"""
        if cloud_percent is None:
            return "unbekannt"
        
        eighths = round(cloud_percent / 12.5)
        eighths = max(0, min(8, eighths))
        
        if eighths == 0:
            return "sonnig"
        elif eighths <= 1:
            return f"{eighths}/8 (heiter)"
        elif eighths <= 3:
            return f"{eighths}/8 (aufgelockert bewölkt)"
        elif eighths <= 6:
            return f"{eighths}/8 (teilweise bewölkt)"
        else:
            return "bewölkt"
    
    def format_for_lora(self, weather_data: Dict[str, Any]) -> str:
        """Ham Radio optimiertes LoRa-Format"""
        if "error" in weather_data:
            return f"WX ERR: {weather_data['error'][:25]}"
        
        temp = weather_data.get("temperatur_celsius", 0)
        humid = weather_data.get("luftfeuchtigkeit_prozent", 0)
        press = weather_data.get("luftdruck_hpa", 0)

        if temp is None:
            temp = 0.0
            logger.debug("⚠️  Temperatur None → 0.0")
        if humid is None:
            humid = 0
            logger.debug("⚠️  Luftfeuchtigkeit None → 0")
        if press is None:
            press = 0.0
            logger.debug("⚠️  Luftdruck None → 0.0" )
        
        # Wind
        wind_speed = weather_data.get("windgeschwindigkeit_kmh", 0) or 0
        wind_dir = weather_data.get("windrichtung_grad")

        if wind_speed >= 1:
           wind_compass = self._wind_direction_to_compass(wind_dir)
           if wind_compass:
               wind_info = f"Wind {wind_speed:.1f}km/h {wind_compass}"
           else:
               wind_info = f"Wind {wind_speed:.1f}km/h"
        else:
           wind_info = "windstill" 

        # Wolkenbedeckung
        clouds_percent = weather_data.get("wolkenbedeckung_prozent")
        cloud_desc = self._calculate_cloud_coverage_description(clouds_percent)
        
        # Niederschlag (optional)
        rain_mm = weather_data.get("niederschlag_mm", 0) or 0
        rain_info = f", {rain_mm:.1f}mm rain" if rain_mm > 0.1 else ""
        
        lora_msg = f"🌤️ WX {self.stat_name}: {temp:.1f}C {humid}% rF, {press:.1f}hPa, {wind_info}, {cloud_desc}{rain_info}"
        
        if len(lora_msg) > 149:
            lora_msg = f"WX {self.stat_name}: {temp:.1f}C {humid}%rF {press:.1f}hPa {wind_info} {cloud_desc}{rain_info}"
        
        return lora_msg

    def _wind_direction_to_compass(self, degrees: Optional[int]) -> str:
        """
        Konvertiere Windrichtung von Grad zu Himmelsrichtung
        232° → SW
        """
        if degrees is None:
            return ""
        
        # Normalisiere auf 0-359°
        degrees = degrees % 360
        
        # 16 Himmelsrichtungen für präzise Angabe
        directions = [
            "N",   "NNE", "NE",  "ENE",
            "E",   "ESE", "SE",  "SSE", 
            "S",   "SSW", "SW",  "WSW",
            "W",   "WNW", "NW",  "NNW"
        ]
        
        # Jede Richtung umfasst 22.5° (360° / 16)
        # +11.25° für Rundung zur nächsten Richtung
        index = round((degrees + 11.25) / 22.5) % 16
        
        return directions[index]
    
    def get_verbose_report(self, weather_data: Dict[str, Any]) -> str:
        """Ausführlicher Wetterbericht mit Fusion-Details"""
        if "error" in weather_data:
            return f"❌ FEHLER: {weather_data['error']}"
        
        # Basis-Info
        temp = weather_data.get('temperatur_celsius', 'N/A')
        humid = weather_data.get('luftfeuchtigkeit_prozent', 'N/A')
        press = weather_data.get('luftdruck_hpa', 'N/A')
        
        # Wind-Info
        wind_speed = weather_data.get("windgeschwindigkeit_kmh")
        wind_dir = weather_data.get("windrichtung_grad")
        if wind_speed is not None and wind_dir is not None:
            wind_text = f"{wind_speed:.1f} km/h aus {wind_dir}°"
        elif wind_speed is not None:
            wind_text = f"{wind_speed:.1f} km/h"
        else:
            wind_text = "N/A"
        
        # Wolken-Info
        clouds_percent = weather_data.get("wolkenbedeckung_prozent")
        cloud_desc = self._calculate_cloud_coverage_description(clouds_percent)
        cloud_text = f"{clouds_percent}% ({cloud_desc})" if clouds_percent is not None else "N/A"
        
        # Fusion-Info
        fusion_info = ""
        if "supplemented_parameters" in weather_data and weather_data["supplemented_parameters"]:
            supplemented = ", ".join(weather_data["supplemented_parameters"])
            fusion_info = f"🔗 Fusion:         {supplemented} von OpenMeteo ergänzt\n"
        
        quality_info = ""
        if "data_quality" in weather_data:
            quality_info = f"⭐ Qualität:       {weather_data['data_quality']}\n"
        
        # Zusätzliche Infos
        extra_info = ""
        if weather_data.get("data_source", "").startswith("DWD") and "taupunkt_celsius" in weather_data:
            extra_info = f"🌡️ Taupunkt:       {weather_data.get('taupunkt_celsius', 'N/A')}°C\n"
        
        # Niederschlag
        rain_mm = weather_data.get("niederschlag_mm", 0) or 0
        rain_info = f"🌧️  Niederschlag:   {rain_mm:.1f} mm\n" if rain_mm > 0 else ""
        
        report = f"""
🌤️  {self.stat_name} {self.freising_lat}/{self.freising_lon} - {weather_data.get('timestamp', 'N/A')[:19]}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🌡️  Temperatur:     {temp}°C
💧  Luftfeuchtigkeit: {humid}%
{extra_info}🔽  Luftdruck:      {press} hPa
💨  Wind:           {wind_text}
☁️  Wolkenbedeckung: {cloud_text}
👁️  Sichtweite:     {weather_data.get('sichtweite_meter', 'N/A')} m
{rain_info}🏢  Station:        {weather_data.get('dwd_station', 'N/A')}
📡  Quelle:         {weather_data.get('data_source', 'N/A')}
{fusion_info}{quality_info}⏰  Messzeitpunkt:  {weather_data.get('messzeitpunkt', 'N/A')[:19]}

📻 LoRa Format: {self.format_for_lora(weather_data)}
        """
        return report.strip()

def main():
    """Produktions-Version"""
    #Freising
    stat_name="Freising"
    lat = 48.4031
    lon = 11.7497

    #Leonding, OÖ
    #stat_name="Leonding"
    #lat = 48.279331
    #lon = 14.248746

    print("🚀 Ham Radio Wetter-Service - HYBRID VERSION")
    print("🔗 DWD BrightSky primär + OpenMeteo Ergänzung")
    print(f"📍 Standort: {lat}/{lon}")
    print("-" * 70)
    
    weather_service = WeatherService(lat, lon, stat_name, max_age_minutes=30)
    
    try:
        weather_data = weather_service.get_weather_data()
        print(weather_service.get_verbose_report(weather_data))
        
        if "error" not in weather_data:
            lora_packet = weather_service.format_for_lora(weather_data)
            print(f"\n📦 LoRa Ham Radio Nachricht:")
            print(f"   {lora_packet}")
            print(f"📏 Länge: {len(lora_packet)} Zeichen")
        
    except KeyboardInterrupt:
        print("\n🛑 Test durch Benutzer abgebrochen")
    except Exception as e:
        logger.error(f"Unerwarteter Fehler: {e}")
        sys.exit(1)

def get_current_weather() -> Dict[str, Any]:
    """API-Funktion für Integration"""
    weather_service = WeatherService()
    return weather_service.get_weather_data()

if __name__ == "__main__":
    main()
