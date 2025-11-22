
# install.py
# WeeWX extension installer for Ecowitt API
# Uses setup.ExtensionInstaller with clear description and safe uninstall.

import configobj
from setup import ExtensionInstaller

class EcowittAPIInstaller(ExtensionInstaller):
    def __init__(self):
        super(EcowittAPIInstaller, self).__init__(
            version='0.6.1',
            name='ecowitt_api',
            description='WeeWX data service that fetches real-time observations from the Ecowitt Cloud API and writes them into WeeWX archive records.',
            author='Anthony Knezevic (aided by Copilot)',
            author_email='va7nez@outlook.com',
            files=[('bin/user', ['bin/user/ecowitt_api.py'])],
            data_services='user.ecowitt_api.EcowittAPI',
            config={
                'EcowittAPI': {
                    'application_key': 'YOUR_APP_KEY',
                    'api_key': 'YOUR_API_KEY',
                    'mac': 'YOUR_MAC_ADDRESS',
                    'unit_system': 'METRICWX',
                    'ignore_value_error': 'False',
                    'label_map': {
                        'tempin': 'inTemp',
                        'humidityin': 'inHumidity',
                        'temp': 'outTemp',
                        'humidity': 'outHumidity',
                        'pressure.relative': 'barometer',
                        'pressure.absolute': 'pressure'
                    }
                }
            }
        )

    def uninstall(self, config_path):
        """Safely remove only our service token and section; preserve [Engine]."""
        try:
            cfg = configobj.ConfigObj(config_path, encoding='utf-8')
        except Exception:
            cfg = configobj.ConfigObj(config_path)

        token = 'user.ecowitt_api.EcowittAPI'
        # Remove token from data_services
        if 'Engine' in cfg and 'Services' in cfg['Engine']:
            services = cfg['Engine']['Services']
            if 'data_services' in services:
                items = [s.strip() for s in str(services['data_services']).split(',') if s.strip()]
                if token in items:
                    items = [s for s in items if s != token]
                    services['data_services'] = ', '.join(items) if items else ''
        # Remove our section
        if 'EcowittAPI' in cfg:
            del cfg['EcowittAPI']
        # Write back
        try:
            cfg.write()
        except Exception:
            cfg = configobj.ConfigObj(config_path)
            cfg.write()
        # Intentionally do NOT call super().uninstall(config_path)


def loader():
    return EcowittAPIInstaller()
