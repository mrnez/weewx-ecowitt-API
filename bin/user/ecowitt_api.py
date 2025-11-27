
# ecowitt_api.py
# ----------------
# WeeWX Data Service for Ecowitt API
# This service fetches real-time weather data from the Ecowitt API and updates
# the WeeWX archive record. It supports dynamic mapping of Ecowitt fields to
# WeeWX observation names via weewx.conf and converts all values from Ecowitt's
# native units (usually US units) to the station's configured unit system
# (METRICWX or US) using WeeWX's unit conversion framework.

import json  # For parsing Ecowitt API responses
import weewx  # Core WeeWX integration
import weewx.units  # Unit conversion utilities
from weewx.wxengine import StdService  # Base class for WeeWX services
from weeutil.weeutil import to_bool  # Utility for boolean conversion
import urllib.request  # For HTTP requests to Ecowitt API
from urllib.error import URLError, HTTPError  # Handle network errors

VERSION = "0.7"  # Version identifier for this Ecowitt API integration

# setup logging through WeeWX logger
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

# ---------------------------------------------------------------------------
# Helper function: Flatten nested Ecowitt JSON into key paths for easy mapping
# Example:
# {
#   "outdoor": {"temperature": {"value": "39.0", "unit": "ºF"}}
# }
# becomes:
# {
#   "outdoor.temperature": ("39.0", "ºF")
# }
# ---------------------------------------------------------------------------
def _flatten_measurements(obj, prefix=None):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and 'value' in v:
                # Leaf node: store value and unit
                out[key] = (v.get('value'), v.get('unit'))
            else:
                # Recurse into nested dict
                out.update(_flatten_measurements(v, key))
    return out


# ---------------------------------------------------------------------------
# EcowittAPI Service Class
# ---------------------------------------------------------------------------
class EcowittAPI(StdService):
    """
    Main service class: Fetches Ecowitt API data and updates WeeWX archive records.
    """

    def __init__(self, engine, config_dict):
        """
        Initialize service:
        - Load configuration from weewx.conf
        - Validate required keys
        - Set unit system for conversion
        - Bind to NEW_ARCHIVE_RECORD event
        """
        super(EcowittAPI, self).__init__(engine, config_dict)

        # Load configuration from [EcowittAPI] section in weewx.conf
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
            raise ValueError(f"EcowittAPI: Missing required config keys: {', '.join(missing)}")

        # Determine unit system (METRICWX or US) for conversion
        unit_system_name = ecowitt_dict.get('unit_system', 'METRICWX').strip().upper()
        if unit_system_name not in weewx.units.unit_constants:
            raise ValueError(f"EcowittAPI: Unknown unit system: {unit_system_name}")
        self.unit_system = weewx.units.unit_constants[unit_system_name]

        # Load optional label_map for dynamic field mapping
        # Example in weewx.conf:
        # label_map = {
        #   "outdoor.temperature": "outTemp",
        #   "pressure.relative": "altimeter"
        # }
        self.label_map = ecowitt_dict.get('label_map', {})

        # Bind to NEW_ARCHIVE_RECORD so we update archive data every interval
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

        # Log initialization details
        loginf(f"EcowittAPI initialized; unit_system={unit_system_name}")

    # -----------------------------------------------------------------------
    # Core logic: Fetch data from Ecowitt API, convert units, update archive
    # -----------------------------------------------------------------------
    def new_archive_record(self, event):
        """
        Steps:
        1. Call Ecowitt API for real-time data.
        2. Flatten nested JSON into key paths (e.g., 'outdoor.temperature').
        3. Map keys to WeeWX obs names using label_map.
        4. Convert all values from Ecowitt units to station's standard units.
        5. Update event.record with plain floats (QC-safe).
        """

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
            # Perform HTTP GET request to Ecowitt API endpoint
            with urllib.request.urlopen(url, timeout=10) as response:
                payload_text = response.read().decode('utf-8')
                # Parse API response into Python dictionary
                data = json.loads(payload_text)

                # Validate response structure
                if 'data' not in data or not isinstance(data['data'], dict):
                    logerr("EcowittAPI: invalid response (missing 'data' dict)")
                    return

                # Flatten nested JSON structure into flat key-value pairs
                sensors = data['data']
                flat = _flatten_measurements(sensors)

                processed = 0
                skipped = 0
                preconv = {}  # Holds converted values before normalization

                # Decide target pressure unit based on station unit system
                target_pressure_unit = 'mbar' if self.unit_system in (weewx.METRIC, weewx.METRICWX) else 'inHg'

                # Iterate through all flattened keys and process each measurement
                for key_path, (raw_value, unit) in flat.items():
                    try:
                        val = float(raw_value)
                    except (ValueError, TypeError):
                        skipped += 1
                        continue

                    # Map Ecowitt key to WeeWX observation name using label_map from config
                    mapped_key = self.label_map.get(key_path)
                    if not mapped_key:
                        # Optional: log unmapped keys for debugging
                        logdbg(f"EcowittAPI: unmapped key {key_path} (unit={unit}, value={raw_value})")
                        continue

                    # Handle pressure conversion separately
                    if key_path.startswith("pressure."):
                        # Determine source unit and conversion group based on key type
                        src_unit = 'inHg' if (unit or '').lower() == 'inhg' else 'mbar'
                        vt = (val, src_unit, 'group_pressure')
                        # Convert value from Ecowitt units to WeeWX standard units
                        conv = weewx.units.convert(vt, target_pressure_unit)
                        val_out = conv[0] if isinstance(conv, tuple) else getattr(conv, 'value', float(conv))
                        preconv[mapped_key] = val_out
                    else:
                        # For other fields, store raw value; will normalize later
                        preconv[mapped_key] = val

                    processed += 1

                # Add unit system context for normalization
                if 'usUnits' not in preconv:
                    preconv['usUnits'] = self.unit_system

                # Normalize non-pressure values to station standard system
                source_units = event.record.get('usUnits', self.unit_system)
                target_data = weewx.units.to_std_system(preconv, source_units)

                # Ensure all values are plain floats (QC-safe)
                for k, v in list(target_data.items()):
                    if isinstance(v, tuple) and len(v) >= 1:
                        target_data[k] = v[0]
                    elif hasattr(v, 'value'):
                        target_data[k] = v.value

                # Update the archive record with converted values (QC-safe floats)
                event.record.update(target_data)

                # Log summary of processed data for debugging and monitoring
                loginf(
                    f"EcowittAPI: record updated (processed={processed}, skipped={skipped}, "
                    f"barometer={target_data.get('barometer')}, pressure={target_data.get('pressure')})"
                )

        except HTTPError as e:
            logerr(f"EcowittAPI: HTTP error {e.code}: {e.reason}")
        except URLError as e:
            logerr(f"EcowittAPI: URL error: {e.reason}")
        except Exception as e:
            logerr(f"EcowittAPI: error fetching data: {e}")
