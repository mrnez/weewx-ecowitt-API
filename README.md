# weewx-ecowitt-API
WeeWX extension data service that fetches real-time observations from the Ecowitt Cloud API and writes them into WeeWX archive records.


# Ecowitt API WeeWX Extension (v0.6.1)
Author: Anthony Knezevic (aided by Copilot) — va7nez@outlook.com


## Description
The intent of this extension is to augment the capture of data from an Ecowitt weather station.  
The original use case is to draw additional data derived at the gateway or display of an Ecowitt 
weather station (e.g. relative and absolute pressure, rainfall rates) and inject them into the 
archive to enrich data directly captured from the weather station - for instance, when using Weewx-SDR 
(https://github.com/matthewwall/weewx-sdr) for data capture.

This was all created with the help of copilot and referencing the many great extensions avaialable 
already for Weewx. I am sharing this in the hope it can be of use to others, and I hope others can 
further elaborate on the idea.

The extension script uses the data service in WeeWX. The idea behind this is to allow it to run
alongside the driver and main loop to derive additional data fields.  It runs on the same timing as 
the archive record.  You may find occasional errors if the API call is running shorter than a minute 
cycle.


## Prerequisites
-Ecowitt weather station
-Ecowitt GW1000/2000 gateway, OR
-Ecowitt display, e.g. HP2560_C, HP2561, etc
-Weather station registered with ecowitt.net (account required)


## References
Ecowitt.net API: https://doc.ecowitt.net/web/#/apiv3en?page_id=17
Ecowitt.net App and API key: https://www.ecowitt.net/home/user
Ecotwitt.net device MAC: https://www.ecowitt.net/home/manage


## How It Works
Configuration
You provide your Ecowitt API credentials (application_key, api_key, mac) and choose a unit system 
(US or METRICWX).
A label_map defines how Ecowitt keys (e.g., pressure.relative) map to WeeWX fields (e.g., barometer).

Data Fetching
At each archive interval, the service calls Ecowitt’s API for real-time data.
The JSON payload is validated for structure and status before processing.

Data Processing
Nested measurements are flattened into simple keys.
Pressure values are converted from inHg to mbar/hPa for consistency.
Non-numeric values are skipped (or ignored silently if configured).

Integration
The processed data is merged into the current archive record.
WeeWX’s unit conversion ensures values match your chosen unit system.

Logging & Safety
Operational logs summarize updates (processed/skipped counts, key metrics).
No API keys, MAC address, or full URLs are ever logged.
Validation guards prevent crashes from malformed payloads.


## Installation
1. Copy `ecowitt_api.zip` to your WeeWX system.
2. Install using:
   ```bash
   weectl extension install ecowitt_api.zip
   ```

## Configuration
The installer defines the service and adds the following section to `weewx.conf`:

```ini
[EcowittAPI]
    application_key = YOUR_APP_KEY
    api_key = YOUR_API_KEY
    mac = YOUR_MAC_ADDRESS
    unit_system = METRICWX
    ignore_value_error = False
    [[label_map]]
        tempin = inTemp
        humidityin = inHumidity
        temp = outTemp
        humidity = outHumidity
        pressure.relative = barometer
        pressure.absolute = pressure
```

It also defines:
```ini
[Engine]
    [[Services]]
        data_services = user.ecowitt_api.EcowittAPI
```

## Notes
- This service flattens Ecowitt's nested payload and converts pressure from inHg to mbar/hPa before handing values to WeeWX.
- Logging is privacy-safe: application key, API key, MAC address, and full URLs are **never** logged.
- Set `unit_system` to match your WeeWX archive (e.g., `US` or `METRICWX`).


## Uninstall
```bash
weectl extension uninstall ecowitt_api
```
This will remove the `EcowittAPI` section and only the `user.ecowitt_api.EcowittAPI` token from `data_services`, preserving the rest of your `[Engine]` configuration.
