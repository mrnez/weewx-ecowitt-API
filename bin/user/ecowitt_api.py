
# ecowitt_api.py
# ----------------
# WeeWX Data Service for Ecowitt API
#
# Purpose
#   This service fetches real-time weather data from the Ecowitt API and updates
#   the WeeWX archive record. It supports dynamic mapping of Ecowitt fields to
#   WeeWX observation names via weewx.conf and converts all values from Ecowitt's
#   native units (usually US units) to the station's configured unit system
#   (METRICWX or US) using WeeWX's unit conversion framework.
#
# Configuration (weewx.conf)
#   [EcowittAPI]
#       application_key = <secret>
#       api_key         = <secret>
#       mac             = <device MAC>
#       unit_system     = METRICWX        # or US / METRIC
#       label_map = {
#           "indoor.temperature": "inTemp",
#           "indoor.humidity":    "inHumidity",
#           "outdoor.temperature": "outTemp",
#           "pressure.relative":   "barometer",  # sea-level pressure
#           "pressure.absolute":   "pressure"    # station pressure
#       }
#

import json  # Parse Ecowitt API responses
import weewx  # Core WeeWX integration
import weewx.units  # Unit conversion utilities
from weewx.wxengine import StdService  # Base class for WeeWX services
import urllib.request  # HTTP requests to Ecowitt API
from urllib.error import URLError, HTTPError  # Network errors

VERSION = "0.8"  # Updated version identifier

# -----------------------------------------------------------------------------
# Logging helpers (compatible with WeeWX logger or syslog fallback)
# -----------------------------------------------------------------------------
try:
    import weeutil.logger
    import logging
    log = logging.getLogger(__name__)
    def logdbg(msg): log.debug(msg)
    def loginf(msg): log.info(msg)
    def logerr(msg): log.error(msg)
except ImportError:
    import syslog
    def logmsg(level, msg): syslog.syslog(level, 'ecowitt_api: %s' % msg)
    def logdbg(msg): logmsg(syslog.LOG_DEBUG, msg)
    def loginf(msg): logmsg(syslog.LOG_INFO, msg)
    def logerr(msg): logmsg(syslog.LOG_ERR, msg)

# -----------------------------------------------------------------------------
# Helper: Flatten nested Ecowitt JSON into key paths for easy mapping
# Example input:
#   {
#     "indoor": {"temperature": {"value": "72.5", "unit": "F"}}
#   }
# Example output:
#   {
#     "indoor.temperature": ("72.5", "F")
#   }
# -----------------------------------------------------------------------------
def _flatten_measurements(obj, prefix=None):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and 'value' in v:
                out[key] = (v.get('value'), v.get('unit'))
            else:
                out.update(_flatten_measurements(v, key))
    return out

# -----------------------------------------------------------------------------
# EcowittAPI Service Class
# -----------------------------------------------------------------------------
class EcowittAPI(StdService):
    """
    Fetch Ecowitt API data and update WeeWX archive records.

    Flow (per archive interval):
      1) GET real-time data from Ecowitt API.
      2) Flatten nested JSON into key paths.
      3) Map paths to WeeWX observation names using label_map.
      4) Convert units:
         - Non-pressure: normalize with to_std_system (source US).
         - Pressure family: convert explicitly then merge.
      5) Update event.record with plain floats (QC-safe).
      6) Log a compact summary of observed values written.
    """

    def __init__(self, engine, config_dict):
        super(EcowittAPI, self).__init__(engine, config_dict)

        # Read configuration from [EcowittAPI]
        ecowitt_dict = config_dict.get('EcowittAPI', {})
        self.application_key = ecowitt_dict.get('application_key')
        self.api_key = ecowitt_dict.get('api_key')
        self.mac = ecowitt_dict.get('mac')

        # Validate required keys
        missing = []
        if not self.application_key: missing.append('application_key')
        if not self.api_key: missing.append('api_key')
        if not self.mac: missing.append('mac')
        if missing:
            raise ValueError("EcowittAPI: Missing required config keys: %s" % ', '.join(missing))

        # Determine station unit system (US, METRICWX, METRIC)
        unit_system_name = str(ecowitt_dict.get('unit_system', 'METRICWX')).strip().upper()
        if unit_system_name not in weewx.units.unit_constants:
            raise ValueError("EcowittAPI: Unknown unit system: %s" % unit_system_name)
        self.unit_system = weewx.units.unit_constants[unit_system_name]

        # Dynamic label_map: Ecowitt path -> WeeWX observation name
        self.label_map = ecowitt_dict.get('label_map', {})

        # Bind handler to NEW_ARCHIVE_RECORD
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        loginf("EcowittAPI initialized; unit_system=%s" % unit_system_name)

    # -------------------------------------------------------------------------
    # Core logic
    # -------------------------------------------------------------------------
    def new_archive_record(self, event):
        """Fetch, map, convert, update the archive, then log observed values."""
        # Build API URL (do not log secrets)
        url = (
            "https://api.ecowitt.net/api/v3/device/real_time"
            f"?application_key={self.application_key}"
            f"&api_key={self.api_key}"
            f"&mac={self.mac}"
            "&call_back=all"
        )
        loginf("EcowittAPI: fetching Ecowitt data")

        try:
            # 1) GET Ecowitt API payload
            with urllib.request.urlopen(url, timeout=10) as response:
                payload_text = response.read().decode('utf-8')

            # 2) Parse and validate structure
            data = json.loads(payload_text)
            if 'data' not in data or not isinstance(data['data'], dict):
                logerr("EcowittAPI: invalid response (missing 'data' dict)")
                return

            sensors = data['data']
            flat = _flatten_measurements(sensors)

            processed = 0
            skipped = 0

            # Two buckets for conversion:
            #   - non-pressure observations (numeric, normalized by to_std_system)
            #   - pressure family (unitized tuple converted explicitly)
            non_pressure = {}
            pressure_vts = {}

            # 3) Map and pre-convert
            for key_path, (raw_value, unit) in flat.items():
                # Robust numeric parse
                if raw_value in (None, "", "--", "NA"):
                    skipped += 1
                    continue
                try:
                    val = float(str(raw_value).strip())
                except (ValueError, TypeError):
                    skipped += 1
                    continue

                # Map Ecowitt path -> WeeWX obs name
                mapped = self.label_map.get(key_path)
                if not mapped:
                    logdbg(f"EcowittAPI: unmapped key {key_path} (unit={unit}, value={raw_value})")
                    continue

                # Identify pressure-family obs
                is_pressure_family = key_path.startswith("pressure.") or mapped in ("barometer", "altimeter", "pressure")
                if is_pressure_family:
                    # Normalize Ecowitt unit labels to WeeWX canonical units
                    u = (unit or '').strip().lower()
                    if u in ('hpa', 'hectopascal', 'mb', 'mbar'):
                        src_unit = 'mbar'
                    elif u in ('kpa',):
                        src_unit = 'kPa'
                    elif u in ('pa',):
                        src_unit = 'Pa'
                    elif u in ('mmhg',):
                        src_unit = 'mmHg'
                    elif u in ('inhg', 'in hg', 'in-hg'):
                        src_unit = 'inHg'
                    else:
                        src_unit = 'inHg'  # fallback
                    pressure_vts[mapped] = (val, src_unit, 'group_pressure')
                else:
                    non_pressure[mapped] = val

                processed += 1

            # 4) Normalize non-pressure to station's system
            non_pressure['usUnits'] = weewx.US  # Ecowitt real-time defaults to US
            target = weewx.units.to_std_system(non_pressure, self.unit_system)

            # 5) Convert pressure-family explicitly to station's target unit
            target_pressure_unit = 'mbar' if self.unit_system in (weewx.METRIC, weewx.METRICWX) else 'inHg'
            for obs_name, vt in pressure_vts.items():
                try:
                    conv = weewx.units.convert(vt, target_pressure_unit)
                    # conv can be tuple or ValueTuple; extract numeric value
                    if isinstance(conv, tuple):
                        target[obs_name] = conv[0]
                    elif hasattr(conv, 'value'):
                        target[obs_name] = conv.value
                    else:
                        target[obs_name] = float(conv)
                except Exception as e:
                    logerr(f"EcowittAPI: pressure convert failed for {obs_name} {vt}: {e}")

            # 6) Ensure plain floats (QC-safe)
            for k, v in list(target.items()):
                if isinstance(v, tuple) and len(v) >= 1:
                    target[k] = v[0]
                elif hasattr(v, 'value'):
                    target[k] = v.value

            # 7) Update archive record
            event.record.update(target)

            # 8) Log observed values only (exclude usUnits), sorted for readability
            summary_out = {k: target[k] for k in sorted(target.keys()) if k != 'usUnits'}
            loginf(
                f"EcowittAPI: record updated (processed={processed}, skipped={skipped}, obs_values={summary_out})"
            )

        except HTTPError as e:
            logerr(f"EcowittAPI: HTTP error {e.code}: {e.reason}")
        except URLError as e:
            logerr(f"EcowittAPI: URL error: {e.reason}")
        except Exception as e:
            logerr(f"EcowittAPI: error fetching data: {e}")
