#!/usr/bin/env python

# import normal packages
import platform
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusShellyemService:
  def __init__(self, servicename, paths, productname='Shelly EM', connection='Shelly EM HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    customname = config['DEFAULT']['CustomName']
    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)
    self._paths = paths

    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)

    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    #self._dbusservice.add_path('/ProductId', 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
    #self._dbusservice.add_path('/ProductId', 0xFFFF) # id assigned by Victron Support from SDM630v2.py
    #self._dbusservice.add_path('/ProductId', 45069) # found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Engerie Meter
    self._dbusservice.add_path('/ProductId', 0xB023) # id needs to be assigned by Victron Support current value for testing
    self._dbusservice.add_path('/DeviceType', 345) # found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Engerie Meter
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', customname)
    self._dbusservice.add_path('/Latency', None)
    self._dbusservice.add_path('/FirmwareVersion', 0.1)
    self._dbusservice.add_path('/HardwareVersion', 0)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/Role', 'grid')
    self._dbusservice.add_path('/Position', 0) # normaly only needed for pvinverter
    self._dbusservice.add_path('/Serial', self._getShellySerial())
    self._dbusservice.add_path('/UpdateIndex', 0)

    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    self._dbusservice.register()

    # last update
    self._lastUpdate = 0

    # add _update function 'timer'
    gobject.timeout_add(250, self._update) # pause 250ms before the next request

    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)

  def _getShellySerial(self):
    meter_data = self._getShellyData()

    if not meter_data['mac']:
        raise ValueError("Response does not contain 'mac' attribute")

    serial = meter_data['mac']
    return serial


  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config;


  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']

    if not value:
        value = 0

    return int(value)

  def _getMeterNoConfig(self):
        config = self._getconfig()
        MeterNo = config['DEFAULT']['GridOrPV']
        return MeterNo
    
  def _getShellyStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']

    if accessType == 'OnPremise':
        URL = "http://%s:%s@%s/status" % (config['ONPREMISE']['Username'], config['ONPREMISE']['Password'], config['ONPREMISE']['Host'])
        URL = URL.replace(":@", "")
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))

    return URL

  def _getShellyData(self):
    URL = self._getShellyStatusUrl()
    try:
        meter_r = requests.get(url=URL, timeout=10)  # Add a timeout for better control
        meter_r.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)
        meter_data = meter_r.json()
        return meter_data
    except requests.exceptions.ConnectionError:
        logging.error("Connection error: Unable to reach Shelly EM at %s", URL)
    except requests.exceptions.Timeout:
        logging.error("Timeout error: Shelly EM at %s took too long to respond", URL)
    except requests.exceptions.RequestException as e:
        logging.error("HTTP request error: %s", str(e))
    except ValueError as e:
        logging.error("Invalid JSON received from Shelly EM: %s", str(e))
    return None  # Return None if any exception occurs

  def _signOfLife(self):
    #logging.info("--- Start: sign of life ---")
    #logging.info("Last _update() call: %s" % (self._lastUpdate))
    #logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    #logging.info("--- End: sign of life ---")
    return True

  def _update(self):
    try:
        # Fetch Shelly EM data
        logging.debug("Fetching data from Shelly EM...")
        meter_data = self._getShellyData()

        # Check if Shelly EM data is available
        if not meter_data or 'emeters' not in meter_data:
            logging.warning("No data received from Shelly EM. Skipping this update cycle.")
            return True  # Skip the update if no data is available

        logging.debug("Shelly EM data fetched successfully.")

        # Get configuration
        logging.debug("Fetching configuration...")
        config = self._getConfig()
        MeterNo = int(config['DEFAULT']['MeterNo'])
        logging.debug(f"MeterNo: {MeterNo}")

        # Extract values from meter data
        voltage = meter_data['emeters'][MeterNo]['voltage']
        power = meter_data['emeters'][MeterNo]['power']
        total_energy = meter_data['emeters'][MeterNo]['total'] / 1000
        total_returned = meter_data['emeters'][MeterNo]['total_returned'] / 1000

        # Calculate current (handle division by zero)
        if voltage == 0:
            logging.warning("Voltage is 0, setting current to 0 to avoid division by zero.")
            current = 0
        else:
            current = power / voltage

        # Log the extracted values
        logging.debug(f"Voltage: {voltage}, Power: {power}, Current: {current}")
        logging.debug(f"Total Energy: {total_energy}, Total Returned: {total_returned}")

        # Update DBus service
        self._dbusservice['/Ac/L1/Voltage'] = voltage
        self._dbusservice['/Ac/L1/Current'] = current
        self._dbusservice['/Ac/Current'] = current
        self._dbusservice['/Ac/L1/Power'] = power
        self._dbusservice['/Ac/Power'] = power if power != 0 else 0
        self._dbusservice['/Ac/L1/Energy/Forward'] = total_energy
        self._dbusservice['/Ac/L1/Energy/Reverse'] = total_returned
        self._dbusservice['/Ac/Energy/Forward'] = total_energy
        self._dbusservice['/Ac/Energy/Reverse'] = total_returned

        # Update the UpdateIndex and lastUpdate
        index = self._dbusservice['/UpdateIndex'] + 1
        self._dbusservice['/UpdateIndex'] = index if index <= 255 else 0
        self._lastUpdate = time.time()

        # Log data to verify DBus service updates
        logging.debug(f"UpdateIndex: {self._dbusservice['/UpdateIndex']}")
        logging.debug("House Consumption (/Ac/Power): %s", self._dbusservice['/Ac/Power'])
        logging.debug("House Forward (/Ac/Energy/Forward): %s", self._dbusservice['/Ac/Energy/Forward'])
        logging.debug("House Reverse (/Ac/Energy/Reverse): %s", self._dbusservice['/Ac/Energy/Reverse'])

    except KeyError as e:
        logging.error("Key error: Missing expected data in Shelly EM response or config: %s", e)
    except requests.exceptions.RequestException as e:
        logging.error("Network error during Shelly EM data fetch: %s", e)
    except Exception as e:
        logging.critical("Unhandled error in _update: %s", e, exc_info=True)

    return True

  def _handlechangedvalue(self, path, value):
    logging.debug("someone else updated %s to %s" % (path, value))
    return True # accept the change

def getServiceConfig():
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    GridOrPV = config['DEFAULT']['GridOrPV']
    return GridOrPV
  

def main():
  #configure logging
  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging.INFO,
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])

  try:
      logging.info("Start");

      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)

      #formatting
      _kwh = lambda p, v: (str(round(v, 2)) + 'KWh')
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _v = lambda p, v: (str(round(v, 1)) + 'V')

      #start our main-service
      GridOrPV = getServiceConfig()
      pvac_output = DbusShellyemService(
        servicename='com.victronenergy.' + GridOrPV, #grid', #grid or pvinverter
        paths={
          '/Ac/Energy/Forward': {'initial': None, 'textformat': _kwh}, # energy bought from the grid
          '/Ac/Energy/Reverse': {'initial': None, 'textformat': _kwh}, # energy sold to the grid
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          '/Ac/Current': {'initial': 0, 'textformat': _a},
          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/L1/Energy/Reverse': {'initial': None, 'textformat': _kwh},

        })

      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
