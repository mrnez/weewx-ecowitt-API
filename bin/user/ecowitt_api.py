
# ecowitt_api.py
# WeeWX Data Service for Ecowitt API
import json
import weewx
import weewx.units
from weewx.wxengine import StdService
from weeutil.weeutil import to_bool
import urllib.request
from urllib.error import URLError, HTTPError

VERSION = "0.6.1"

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

# --- Helpers to flatten nested payload and convert pressure units ---
def _flatten_measurements(obj, prefix=None):
    """Flatten nested Ecowitt measurement dicts into key -> (value, unit)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and 'value' in v:
                out[key] = (v.get('value'), v.get('unit'))
            else:
                out.update(_flatten_measurements(v, key))
    return out

def _convert_pressure(value, unit):
    """Return pressure in mbar/hPa for WeeWX (numeric equals)."""
    try:
        val = float(value)
    except (ValueError, TypeError):
        return None
    u = (unit or '').strip().lower()
    if u == 'inhg':
        return val * 33.863886
    elif u in ('hpa', 'mbar', 'mb'):
        return val
    else:
        return val

class EcowittAPI(StdService):
    def __init__(self, engine, config_dict):
        super(EcowittAPI, self).__init__(engine, config_dict)
        ecowitt_dict = config_dict.get('EcowittAPI', {})
        self.application_key = ecowitt_dict.get('application_key')
        self.api_key = ecowitt_dict.get('api_key')
        self.mac = ecowitt_dict.get('mac')

        # --- Config sanity checks (do not log secrets) ---
        missing = []
        if not self.application_key: missing.append('application_key')
        if not self.api_key: missing.append('api_key')
        if not self.mac: missing.append('mac')
        unit_system_name = ecowitt_dict.get('unit_system', 'METRICWX').strip().upper()
        if unit_system_name not in weewx.units.unit_constants:
            raise ValueError("EcowittAPI: Unknown unit system: %s" % unit_system_name)
        if missing:
            raise ValueError("EcowittAPI: Missing required config keys: %s" % ", ".join(missing))

        self.unit_system = weewx.units.unit_constants[unit_system_name]
        self.ignore_value_error = to_bool(ecowitt_dict.get('ignore_value_error', False))
        self.label_map = ecowitt_dict.get('label_map', {})

        # Operational logging (privacy-safe)
        loginf(f"EcowittAPI v{VERSION} initialized; unit_system={unit_system_name}")

        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

    def new_archive_record(self, event):
        # Build URL but do not log it (contains secrets)
        url = (
            "https://api.ecowitt.net/api/v3/device/real_time"
            f"?application_key={self.application_key}"
            f"&api_key={self.api_key}"
            f"&mac={self.mac}"
            "&call_back=all"
        )

        # Privacy-safe status message
        loginf("EcowittAPI: fetching Ecowitt data")

        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                payload_text = response.read().decode('utf-8')
            # Validation guard: ensure JSON and shape
            try:
                data = json.loads(payload_text)
            except Exception as je:
                logerr(f"EcowittAPI: JSON parse error: {je}")
                return

            if not isinstance(data, dict):
                logerr("EcowittAPI: invalid response type (expected dict)")
                return

            # Optional API status code check
            code = data.get('code')
            if code is not None and code != 0:
                msg = data.get('msg', 'unknown error')
                logerr(f"EcowittAPI: API returned error code {code}: {msg}")
                return

            if 'data' not in data or not isinstance(data['data'], dict):
                logerr("EcowittAPI: invalid response (missing 'data' dict)")
                return

            sensors = data['data']
            flat = _flatten_measurements(sensors)

            processed = 0
            skipped = 0
            new_record_data = {}

            for key_path, (raw_value, unit) in flat.items():
                # Skip non-numeric
                try:
                    val = float(raw_value)
                except (ValueError, TypeError):
                    skipped += 1
                    if self.ignore_value_error:
                        continue
                    logdbg(f"Skipping non-numeric value for {key_path}: {raw_value}")
                    continue

                mapped_key = self.label_map.get(key_path, key_path)

                # Convert pressure where needed
                if key_path in ('pressure.relative', 'pressure.absolute'):
                    converted = _convert_pressure(raw_value, unit)
                    if converted is None:
                        skipped += 1
                        if self.ignore_value_error:
                            continue
                        logdbg(f"Invalid pressure value for {key_path}: {raw_value} {unit}")
                        continue
                    val = converted

                new_record_data[mapped_key] = val
                processed += 1

            # Ensure units and convert to event record's system
            if 'usUnits' not in new_record_data:
                new_record_data['usUnits'] = self.unit_system
            source_units = event.record.get('usUnits', self.unit_system)
            target_data = weewx.units.to_std_system(new_record_data, source_units)
            event.record.update(target_data)

            # Operational summary (privacy-safe)
            loginf(
                "EcowittAPI: record updated "
                f"(processed={processed}, skipped={skipped}, "
                f"barometer={new_record_data.get('barometer')}, "
                f"pressure={new_record_data.get('pressure')})"
            )

        except HTTPError as e:
            logerr(f"EcowittAPI: HTTP error {e.code}: {e.reason}")
        except URLError as e:
            logerr(f"EcowittAPI: URL error: {e.reason}")
        except Exception as e:
            # Generic catch-all (no secrets in logs)
            logerr(f"EcowittAPI: error fetching data: {e}")
