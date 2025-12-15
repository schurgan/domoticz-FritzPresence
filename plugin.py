# Plugin für Fritz!Box
#
# Author: belze/schurgan
# Komplett überarbeitet by ChatGPT (12.2025)
#

"""
<?xml version="1.0" encoding="UTF-8"?>
<plugin key="FritzPresence" name="Fritz!Presence Plugin"
        author="belze" version="0.8.0-fixed"
        externallink="https://github.com/belzetrigger/domoticz-FritzPresence">

    <description>
        <h2>Fritz!Presence</h2>
        Uses Fritz!Box router information to detect device presence.
    </description>

    <params>
        <param field="Mode1" label="Hostname or IP" width="200px"
               required="true" default="fritz.box"/>

        <param field="Username" label="User" width="200px" required="true"/>

        <param field="Password" label="Password" width="200px"
               required="true" password="true"/>

        <param field="Port" label="Domoticz Port" width="75px"
               required="true" default="8080"/>

        <param field="Mode4" label="Update every x minutes" width="200px"
               required="true" default="5"/>

        <param field="Mode5" label="MAC Addresses (; separated)" width="350px"/>

        <param field="Mode6" label="Debug" width="75px">
            <options>
                <option label="Debug" value="Debug"/>
                <option label="Normal" value="Normal" default="true"/>
            </options>
        </param>
    </params>
</plugin>
"""

# ------------------------------------------------------------
# Imports
# ------------------------------------------------------------
import Domoticz
import urllib.request
from urllib.parse import quote
from datetime import datetime, timedelta
from typing import List

from blz import blzHelperInterface

try:
    from fritzhelper.fritzHelper import FritzHelper
except ImportError as e:
    FritzHelper = None
    Domoticz.Error(f"Missing fritzhelper library: {e}")

# ------------------------------------------------------------
# Constants
# ------------------------------------------------------------
UNIT_CMD_SWITCH_IDX = 1
UNIT_DEV_START_IDX = 2
UNIT_CMD_SWITCH_NAME = "FP - Admin"

UNIT_CMD_SWITCH_OPTIONS = {
    'LevelNames': '|+ WiFi|+ ethernet|+ all active|+ all|- all',
    'LevelOffHidden': 'true',
    'SelectorStyle': '0'
}

LVL_WIFI = 10
LVL_ETH = 20
LVL_ACTIVE = 30
LVL_ALL = 40
LVL_REMOVE = 50

# ------------------------------------------------------------
# Plugin Class
# ------------------------------------------------------------
class BasePlugin:

    def __init__(self):
        self.debug = False
        self.fritz = None
        self.nextpoll = datetime.now()
        self.pollinterval = 300
        self.host = None
        self.user = None
        self.password = None
        self.macList: List[str] = []

    # --------------------------------------------------------

    def onStart(self):
        self.debug = (Parameters["Mode6"] == "Debug")
        Domoticz.Debugging(1 if self.debug else 0)

        Domoticz.Log("FritzPresence starting")

        # Poll interval
        try:
            minutes = max(1, min(60, int(Parameters["Mode4"])))
            self.pollinterval = minutes * 60
        except Exception:
            Domoticz.Error("Invalid polling interval")
            return

        # Credentials
        self.host = Parameters["Mode1"]
        self.user = Parameters["Username"]
        self.password = Parameters["Password"]

        if blzHelperInterface.isBlank(self.user) or blzHelperInterface.isBlank(self.password):
            Domoticz.Error("Username / Password missing")
            return

        if FritzHelper is None:
            Domoticz.Error("FritzHelper not available – aborting")
            return

        # MAC list
        if Parameters["Mode5"]:
            self.macList = [m.strip() for m in Parameters["Mode5"].split(';') if blzHelperInterface.isValidMAC(m)]
        else:
            Domoticz.Log("No MAC addresses configured")

        self._createAdminSwitch()
        self._createInitialDevices()

        self.fritz = FritzHelper(self.host, self.user, self.password, self.macList)
        # Register all existing Domoticz devices (MACs) in helper
        for u in Devices:
            if Devices[u].Unit >= UNIT_DEV_START_IDX:
                mac = Devices[u].DeviceID
                try:
                    if hasattr(self.fritz, "addDeviceByMac"):
                        self.fritz.addDeviceByMac(mac)
                    elif hasattr(self.fritz, "addDevice"):
                        # fallback if helper expects dict
                        self.fritz.addDevice({"mac": mac, "name": Devices[u].Name, "status": 0, "ip": ""})
                except Exception as e:
                    Domoticz.Error(f"Register onStart failed for {mac}: {e}")
        Domoticz.Log("FritzPresence started successfully")

    # --------------------------------------------------------

    def onStop(self):
        if self.fritz:
            self.fritz.stop()
        Domoticz.Log("FritzPresence stopped")

    # --------------------------------------------------------

    def onCommand(self, Unit, Command, Level, Hue):
        Command = Command.strip()

        # Admin selector
        if Unit == UNIT_CMD_SWITCH_IDX:
            if Command != "Set":
                return

            if Level == LVL_WIFI:
                self._createFromHosts(self.fritz.getWifiHosts())
            elif Level == LVL_ETH:
                self._createFromHosts(self.fritz.getEthernetHosts())
            elif Level == LVL_ACTIVE:
                self._createFromHosts(self.fritz.getActiveHosts())
            elif Level == LVL_ALL:
                self._createFromHosts(self.fritz.getAllHosts())
            elif Level == LVL_REMOVE:
                self._removeAllDevices()
            return

        # Normal device → WOL
        if Command == "On":
            mac = Devices[Unit].DeviceID
            self.fritz.wakeOnLan(mac)

    # --------------------------------------------------------

    def onHeartbeat(self):
        now = datetime.now()
        if now < self.nextpoll:
            return

        self.nextpoll = now + timedelta(seconds=self.pollinterval)

        if not self.fritz:
            return

        self.fritz.readStatus()

        for x in Devices:
            if Devices[x].Unit < UNIT_DEV_START_IDX:
                continue

            mac = Devices[x].DeviceID

            try:
                connected = 1 if self.fritz.isDeviceConnected(mac) else 0
                name = self.fritz.getDeviceName(mac)
            except ValueError:
                Domoticz.Error(f"Helper kennt {mac} noch nicht – registriere nachträglich.")
                try:
                    if hasattr(self.fritz, "addDeviceByMac"):
                        self.fritz.addDeviceByMac(mac)
                    connected = 0
                    name = Devices[x].Name
                except Exception as e:
                    Domoticz.Error(f"Nachregistrierung fehlgeschlagen für {mac}: {e}")
                    connected = 0
                    name = Devices[x].Name

        self._updateDevice(x, connected, "", name)

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    def _createAdminSwitch(self):
        if UNIT_CMD_SWITCH_IDX not in Devices:
            Domoticz.Device(
                Name=UNIT_CMD_SWITCH_NAME,
                Unit=UNIT_CMD_SWITCH_IDX,
                TypeName="Selector Switch",
                Switchtype=18,
                Options=UNIT_CMD_SWITCH_OPTIONS,
                Used=1
            ).Create()

    def _createInitialDevices(self):
        for i, mac in enumerate(self.macList):
            unit = UNIT_DEV_START_IDX + i
            if unit not in Devices:
                Domoticz.Device(
                    Name=mac,
                    Unit=unit,
                    TypeName="Switch",
                    DeviceID=mac,
                    Used=1
                ).Create()

    def _removeAllDevices(self):
        for x in list(Devices):
            if Devices[x].Unit >= UNIT_DEV_START_IDX:
                Devices[x].Delete()

    def _createFromHosts(self, hosts):
        for host in hosts:
            mac = host.get("mac")
            name = host.get("name", mac)
            if not mac:
                continue

            if not self._findUnit(mac):
                unit = max(Devices) + 1 if Devices else UNIT_DEV_START_IDX
                Domoticz.Device(
                    Name=name,
                    Unit=unit,
                    TypeName="Switch",
                    DeviceID=mac,
                    Used=1
                ).Create()

    def _findUnit(self, devId):
        for x in Devices:
            if Devices[x].DeviceID == devId:
                return x
        return None

    def _updateDevice(self, unit, nValue, sValue, name):
        dev = Devices[unit]
        if dev.nValue != nValue or dev.Name != name:
            dev.Update(nValue=nValue, sValue=sValue, Name=name)

            # Safe rename via JSON API
            url = f"http://127.0.0.1:{Parameters['Port']}/json.htm?param=renamedevice&type=command&idx={dev.ID}&name={quote(name)}"
            try:
                urllib.request.urlopen(url)
            except Exception:
                pass


# ------------------------------------------------------------
# Domoticz entry points
# ------------------------------------------------------------
_plugin = BasePlugin()

def onStart(): _plugin.onStart()
def onStop(): _plugin.onStop()
def onCommand(Unit, Command, Level, Hue): _plugin.onCommand(Unit, Command, Level, Hue)
def onHeartbeat(): _plugin.onHeartbeat()
