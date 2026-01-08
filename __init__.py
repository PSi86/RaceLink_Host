'''Created by Peter Simandl "psi" in 2025
    Works with Rotorhazard 4.0'''

import logging
import time
import struct
from typing import Optional, Union #necessary for type hinting with Optional
from struct import pack
from datetime import datetime
import json
#from RHRace import WinCondition
#import RHUtils
#import Results
from eventmanager import Evt
from EventActions import ActionEffect
from data_import import DataImporter
from data_export import DataExporter
from RHUI import UIField, UIFieldType, UIFieldSelectOption
#from VRxControl import VRxController, VRxDevice, VRxDeviceMethod
from .gatecontrol_webui import register_gc_blueprint

# ---- transport import (tolerant to both package and flat layout) ----
try:
    from .gc_transport import (
        LP, EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE,
        LoRaUSB, _mac_last3_from_hex
    )
except Exception:
    from gc_transport import (
        LP, EV_ERROR, EV_RX_WINDOW_OPEN, EV_RX_WINDOW_CLOSED, EV_TX_DONE,
        LoRaUSB, _mac_last3_from_hex
    )

logger = logging.getLogger(__name__)

#MAC_ADDR_OPT_NAME = 'comm_gate_mac'

def initialize(rhapi):
    global gc_instance
    # TODO: wegen webui nötig?:
    #global gc_instance, gc_devicelist, gc_grouplist, GC_DeviceGroup, logger

    gc_instance = GateControl_LoRa(
        rhapi,
        'GateControl_LoRa',
        'GateControl'
    )

    register_gc_blueprint(
            rhapi,
            gc_instance=gc_instance,
            gc_devicelist=gc_devicelist,
            gc_grouplist=gc_grouplist,
            GC_DeviceGroup=GC_DeviceGroup,
            logger=logger
        )

    #gc_instance.register_settings()
    #gc_instance.loadDeviceList() #not possible here as the database is not ready at this point. Register Startup callback instead
    #gc_instance.init_settings()

    rhapi.events.on(Evt.DATA_IMPORT_INITIALIZE, gc_instance.register_gc_dataimporter)
    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, gc_instance.register_gc_dataexporter)
    rhapi.events.on(Evt.ACTIONS_INITIALIZE, gc_instance.registerActions)

    #rhapi.events.on(Evt.STARTUP, gc_instance.registerHandlers)
    #rhapi.events.on(Evt.STARTUP, gc_instance.load_from_db) # now done in onStartup()
    rhapi.events.on(Evt.STARTUP, gc_instance.onStartup)

    rhapi.events.on(Evt.RACE_START, gc_instance.onRaceStart)
    rhapi.events.on(Evt.RACE_FINISH, gc_instance.onRaceFinish)
    rhapi.events.on(Evt.RACE_STOP, gc_instance.onRaceStop)

    ''' now done in register_settings()
    rhapi.ui.register_panel('esp_gc_settings', 'GateControl Plugin', 'settings')
    #rhapi.fields.register_option(UIField('esp_gc_device_config', "Device Config", UIFieldType.TEXT, private=True), 'esp_gc_settings') #rhapi.db.option_set('esp_gc_device_config', device data_json)
    rhapi.fields.register_option(UIField('esp_gc_groups_config', "Groups Config", UIFieldType.TEXT, private=False), 'esp_gc_settings') #rhapi.db.option_set('esp_gc_groups_config', groups data_json)
    #TODO select field with existing groups and an additional entry "create new group". if this one is selected the code will check for a string in the text field to name the new group
    # the select field will have to by dynamic and also the current field esp_gc_assignToGroup should be renamed to esp_gc_newGroupName
    #rhapi.fields.register_option(UIField('esp_gc_assignToGroup', "Select Group", UIFieldType.SELECT, options=self.uiEffectList, private=False), 'esp_gc_settings')
    rhapi.fields.register_option(UIField('esp_gc_assignToGroup', "New Group", UIFieldType.TEXT, private=False), 'esp_gc_settings') #TODO change to esp_gc_newGroupName
    rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_btn_set_defaults', "Save Configuration", gc_instance.save_to_db, args={'manual':True})
    rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_btn_get_devices', "Discover Devices", gc_instance.discoveryAction, args={'manual':True})
    rhapi.fields.register_option(UIField('psi_comms_port', "Manual Port Override", UIFieldType.TEXT), 'esp_gc_settings')
    rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_run_autodetect', "Run Port Assignment", gc_instance.discoverPort, args={'manual':True})'''

class GateControl_LoRa():
    def __init__(self, rhapi, name, label):
        self.lora = None
        self.ready = False
        self._rhapi = rhapi
        self.name = name
        self.label = label
        self.lora = None          # wird in discoverPort() gesetzt
        self.ready = False
        self.action_reg_fn = None
        self.deviceCfgValid = False #only true when device config is loaded sucessfully
        self.groupCfgValid = False #only true when group config is loaded sucessfully
        self.uiDeviceList = None
        self.uiGroupList = None
        self.uiDiscoveryGroupList = None

        # Basic colors: 1-9; Basic effects: 10-19; Special Effects (WLED only): 20-100
        self.uiEffectList = [
                            UIFieldSelectOption('01', "Red"), #10
                            UIFieldSelectOption('02', "Green"), #20
                            UIFieldSelectOption('03', "Blue"), #30
                            UIFieldSelectOption('04', "White"), #01
                            UIFieldSelectOption('05', "Yellow"), #11
                            UIFieldSelectOption('06', "Cyan"), # 21
                            UIFieldSelectOption('07', "Magenta"), #34

                            UIFieldSelectOption('10', "Blink Multicolor"), #40
                            UIFieldSelectOption('11', "Pulse White"), #41
                            UIFieldSelectOption('12', "Colorloop"), #42
                            UIFieldSelectOption('13', "Blink RGB"), #43

                            UIFieldSelectOption('20', "WLED Chaser"), 
                            UIFieldSelectOption('21', "WLED Chaser inverted"),
                            UIFieldSelectOption('22', "WLED Rainbow"),
                            ]

    #called in load_from_db which is called in onStartup
    def register_settings(self):
        logger.debug("GC: Registering Settings UI Elements")
        temp_uiGroupList = [UIFieldSelectOption(0, "New Group")]
        temp_uiGroupList += self.uiDiscoveryGroupList
        #temp_uiGroupList = self.uiDiscoveryGroupList
        #temp_uiGroupList.append(UIFieldSelectOption(len(self.uiGroupList), "New Group")) # still use lenght of complete uiGroupList here and not the length of the filtered uiDiscoveryGroupList

        self._rhapi.ui.register_panel('esp_gc_settings', 'GateControl Plugin', 'settings')
        self._rhapi.fields.register_option(UIField('esp_gc_device_config', "Device Config", UIFieldType.TEXT, private=False), 'esp_gc_settings') #rhapi.db.option_set('esp_gc_device_config', device data_json)
        self._rhapi.fields.register_option(UIField('esp_gc_groups_config', "Groups Config", UIFieldType.TEXT, private=False), 'esp_gc_settings') #rhapi.db.option_set('esp_gc_groups_config', groups data_json)
        #TODO select field with existing groups and an additional entry "create new group". if this one is selected the code will check for a string in the text field to name the new group
        # the select field will have to by dynamic and also the current field esp_gc_assignToGroup should be renamed to esp_gc_newGroupName
        #rhapi.fields.register_option(UIField('esp_gc_assignToGroup', "Select Group", UIFieldType.SELECT, options=self.uiEffectList, private=False), 'esp_gc_settings')
        self._rhapi.fields.register_option(UIField('esp_gc_assignToGroup', "Add discovered Devices to Group", UIFieldType.SELECT, options=temp_uiGroupList, value=temp_uiGroupList[0].value), 'esp_gc_settings') #new
        self._rhapi.fields.register_option(UIField('esp_gc_assignToNewGroup', "New Group Name", UIFieldType.TEXT, private=False), 'esp_gc_settings') #TODO change to esp_gc_newGroupName
        self._rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_btn_set_defaults', "Save Configuration", self.save_to_db, args={'manual':True})
        self._rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_btn_force_groups', "Set all Groups", self.forceGroups, args={'manual':True})
        self._rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_btn_get_devices', "Discover Devices", self.discoveryAction, args={'manual':True})
        #self._rhapi.fields.register_option(UIField('psi_comms_port', "Manual Port Override", UIFieldType.TEXT), 'esp_gc_settings') # not needed so far - reduce UI complexity
        self._rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_run_autodetect', "Detect USB Communicator", self.discoverPort, args={'manual':True})
        
    '''        
        self._rhapi.ui.register_panel('esp_gc_settings', 'GateControl Plugin', 'settings')
        self._rhapi.fields.register_option(UIField('esp_gc_device_config', "Device Config", UIFieldType.TEXT, private=True), 'esp_gc_settings') #rhapi.db.option_set('esp_gc_device_config', device data_json)
        self._rhapi.fields.register_option(UIField('esp_gc_groups_config', "Groups Config", UIFieldType.TEXT, private=True), 'esp_gc_settings') #rhapi.db.option_set('esp_gc_groups_config', groups data_json)
        self._rhapi.fields.register_option(UIField('psi_comms_port', "Manual Port Override", UIFieldType.TEXT), 'esp_gc_settings')
        self._rhapi.ui.register_quickbutton('esp_gc_settings', 'gc_run_autodetect', "Run Port Assignment", self.discoverPort, args={'manual':True})
    '''

    def onStartup(self, _args):
        self.load_from_db()
        self.discoverPort({})

    def save_to_db(self, args):
        logger.debug("GC: Writing current states to Database")
        #if len(gc_devicelist)>0: # we just accept that the devicelist is empty
        config_str_devices=str([obj.__dict__ for obj in gc_devicelist])
        #else: 
        #    config_str_devices=str([obj.__dict__ for obj in gc_backup_devicelist])
        self._rhapi.db.option_set('esp_gc_device_config', config_str_devices)
 
        if len(gc_grouplist)>=len(gc_backup_grouplist): # minimal sanity check - backup groups are 3 currently TODO implement general sanity checks - especially for import
            config_str_groups=str([obj.__dict__ for obj in gc_grouplist])
        else:
            config_str_groups=str([obj.__dict__ for obj in gc_backup_grouplist])
        self._rhapi.db.option_set('esp_gc_groups_config', config_str_groups)

    def load_from_db(self):
        logger.debug("GC: Applying config from Database")
        config_str_devices=self._rhapi.db.option('esp_gc_device_config', None) #returns exact string from device config field
        config_str_groups=self._rhapi.db.option('esp_gc_groups_config', None) #returns exact string from device config field

        #if config_str_devices is None or len(config_str_devices)<5:
        if config_str_devices is None: # error loading string from db - eg:first start with plugin
            config_str_devices=str([obj.__dict__ for obj in gc_backup_devicelist])
            self._rhapi.db.option_set('esp_gc_device_config', config_str_devices)
        
        if config_str_devices == "": # if there is an empty string in the db
            config_str_devices="[]" # initialize with basic empty list
            self._rhapi.db.option_set('esp_gc_device_config', config_str_devices)
 
        config_list_devices=list(eval(config_str_devices)) #datatype is <class 'list'> with elements of type <class 'dict'>
        gc_devicelist.clear()   #Delete old content of gc_devicelist

        for device in config_list_devices:
            logger.debug(device)
            gc_devicelist.append(GC_Device(
                addr=device['addr'].upper(), 
                type=device['type'], 
                name=device['name'], 
                groupId=device['groupId'], 
                version=device['version'], 
                state=device['state'], 
                effect=device['effect'], 
                brightness=device['brightness']))
            #logger.debug(gc_devicelist[1].name) #check if devicelist is in the expected state after import - works'''


        #if config_str_groups is None or len(config_str_groups)<2:
        if config_str_groups is None or config_str_groups == "":
            #gc_grouplist=gc_backup_grouplist
            config_str_groups=str([obj.__dict__ for obj in gc_backup_grouplist])
            self._rhapi.db.option_set('esp_gc_groups_config', config_str_groups)

        config_list_groups=list(eval(config_str_groups)) #datatype is <class 'list'> with elements of type <class 'dict'>
        gc_grouplist.clear()   #Delete old content of gc_devicelist

        for group in config_list_groups:
            logger.debug(group)
            gc_grouplist.append(GC_DeviceGroup(group['name'], group['static_group'], group['device_type']))
            #logger.debug(gc_grouplist[1].name) #check if grouplist is in the expected state after import - works'''
        
        self.uiDeviceList = self.createUiDevList() # old, but keep this for now
        self.uiGroupList = self.createUiGroupList()
        self.uiDiscoveryGroupList=self.createUiGroupList(True) #this time excluding static groups
        self.register_settings() #new
        self.register_quickset_ui()
        self.registerActions()
        self._rhapi.ui.broadcast_ui('settings')
        self._rhapi.ui.broadcast_ui('run')
    
    def createUiDevList(self):
        logger.debug("GC: Creating UI Device Select Options")
        temp_ui_devlist=[]
        for device in gc_devicelist:
            temp_ui_devlist.append(UIFieldSelectOption(device.addr, device.name))
        return temp_ui_devlist
    
    def createUiGroupList(self, exclude_static=False):
        logger.debug("GC: Creating UI Device Select Options")
        temp_ui_grouplist=[
            #UIFieldSelectOption('value', "Visible Name"),
        ]
        for i, group in enumerate(gc_grouplist):
            if(exclude_static is False or (exclude_static is True and group.static_group == 0)):
                temp_ui_grouplist.append(UIFieldSelectOption(i, group.name))

        return temp_ui_grouplist
    
    def register_quickset_ui(self):
        self._rhapi.ui.register_panel('esp_gc_quickset', 'GateControl Quickset', 'run')
        #self._rhapi.fields.register_option(UIField('gc_quickset_device', "Gate Group", UIFieldType.SELECT, options=self.uiDeviceList, value=self.uiDeviceList[0].value), 'esp_gc_quickset') #old - now using groups in UI only
        self._rhapi.fields.register_option(UIField('gc_quickset_group', "Gate Group", UIFieldType.SELECT, options=self.uiGroupList, value=self.uiGroupList[0].value), 'esp_gc_quickset')
        self._rhapi.fields.register_option(UIField('gc_quickset_effect', "Color", UIFieldType.SELECT, options=self.uiEffectList, value='01'), 'esp_gc_quickset')
        self._rhapi.fields.register_option(UIField('gc_quickset_brightness', "Brightness", UIFieldType.BASIC_INT, value=70), 'esp_gc_quickset')
        #self._rhapi.ui.register_quickbutton('esp_gc_quickset', 'run_quickset', "Apply", self.gateSwitch, args={'manual':True}) #old - now using groups in UI only
        self._rhapi.ui.register_quickbutton('esp_gc_quickset', 'run_quickset', "Apply", self.groupSwitch, args={'manual':True})

    def registerActions(self, args=None):
        #TODO register actions AFTER loading the devicelist from database - need to better understand the args here
        logger.debug("Registering GateControl Actions")

        if args:
            if 'register_fn' in args:
                self.action_reg_fn=args['register_fn'] # save caller argument for dynamic changes of ui actions elements
                logger.debug("Saved Actions Register Function in GateControl Instance")

        if not args and self.action_reg_fn:
            for effect in [
                ActionEffect(
                    "GateControl Action",
                    self.groupSwitch, #self.gateSwitch #old
                    [
                        #UIField('gc_action_device', "Gate Group", UIFieldType.SELECT, options=self.uiDeviceList, value=self.uiDeviceList[0].value), #old - now using groups instead
                        UIField('gc_action_group', "Gate Group", UIFieldType.SELECT, options=self.uiGroupList, value=self.uiGroupList[0].value),
                        UIField('gc_action_effect', "Color", UIFieldType.SELECT, options=self.uiEffectList, value='01'),
                        UIField('gc_action_brightness', "Brightness", UIFieldType.BASIC_INT, value=70)
                        
                    ],
                    name='gcaction',
                )
            ]:
                self.action_reg_fn(effect)

    def register_gc_dataimporter(self, args):        
        for importer in [
            DataImporter(
                'GateControl Config JSON',
                gc_import_json, #self.import_gc_settings,
                None,
                [
                    UIField('gc_import_devices', "Import Devices", UIFieldType.CHECKBOX, value=False),
                    UIField('gc_import_devgroups', "Import Groups", UIFieldType.CHECKBOX, value=False),
                ]
            ),
        ]:
            args['register_fn'](importer)

    def register_gc_dataexporter(self, args):        
        for exporter in [
            DataExporter(
                "GateControl Config JSON",
                gc_write_json,
                gc_config_json_output
            )
        ]:
            args['register_fn'](exporter)
    
    #not used currently
    def registerHandlers(self, args):
        self._rhapi.ui.message_notify("text")
        #rhapi.events.on(Evt.STARTUP, self.onStartup)
        #rhapi.events.on(Evt.RACE_START, self.onRaceStart)
        #rhapi.events.on(Evt.RACE_FINISH, self.onRaceFinish)
        #rhapi.events.on(Evt.RACE_STOP, self.onRaceStop)
    
    def gateSwitch(self, action, args=None):
        # control triggered by ActionEffect
        if 'gc_action_device' in action:
            logger.debug('Action triggered')
            targetDevice=self.getDeviceFromAddress(action['gc_action_device'])
            if targetDevice is None:
                logger.warning("gateSwitch: device not found: %r", action['gc_action_device']); return
            targetDevice.brightness=int(action['gc_action_brightness'])
            targetDevice.effect=int(action['gc_action_effect'])
            
            if int(action['gc_action_brightness'])==0:
                targetDevice.state=0
            else:
                targetDevice.state=1
            
            logger.debug('sendGateControl action call - device')
            self.sendGateControl(targetDevice)

        # control triggered by UI button press    
        if 'manual' in action:
            logger.debug('Manual triggered')
            targetDevice=self.getDeviceFromAddress(self._rhapi.db.option('gc_quickset_device', None))
            if targetDevice is None:
                logger.warning("gateSwitch(manual): device not found in DB option"); return
            targetDevice.brightness=int(self._rhapi.db.option('gc_quickset_brightness', None))
            targetDevice.effect=int(self._rhapi.db.option('gc_quickset_effect', None))

            if int(self._rhapi.db.option('gc_quickset_brightness', None))==0:
                targetDevice.state=0
            else:
                targetDevice.state=1
            #text = self._rhapi.db.option('gc_quickset_brightness', None)
            #self._rhapi.ui.message_notify(text)

            logger.debug('sendGateControl manual call - device')
            self.sendGateControl(targetDevice)

    def groupSwitch(self, action, args=None):
        # control triggered by ActionEffect
        if 'gc_action_group' in action:
            logger.debug('Action triggered')
            targetGroup=int(action['gc_action_group']) #TODO
            targetBrightness=int(action['gc_action_brightness'])
            targetEffect=int(action['gc_action_effect'])
            
            if int(action['gc_action_brightness'])==0:
                targetState=0
            else:
                targetState=1
            
            logger.debug('GC: groupSwitch called by Action (event based)')
            self.sendGroupControl(targetGroup,targetState,targetEffect,targetBrightness)

        # control triggered by UI button press    
        if 'manual' in action:
            logger.debug('Manual triggered')
            targetGroup=int(self._rhapi.db.option('gc_quickset_group', None))
            targetBrightness=int(self._rhapi.db.option('gc_quickset_brightness', None))
            targetEffect=int(self._rhapi.db.option('gc_quickset_effect', None))

            if int(self._rhapi.db.option('gc_quickset_brightness', None))==0:
                targetState=0
            else:
                targetState=1
            #text = self._rhapi.db.option('gc_quickset_brightness', None)
            #self._rhapi.ui.message_notify(text)

            logger.debug('GC: groupSwitch called from UI')
            self.sendGroupControl(targetGroup,targetState,int(targetEffect),targetBrightness)

    
    def discoverPort(self, args):
        """Initialize communicator via LoRaUSB only. No direct serial here."""
        port = self._rhapi.db.option('psi_comms_port', None)
        try:
            self.lora = LoRaUSB(port=port, on_event=None)
            ok = self.lora.discover_and_open()
            if ok:
                self.lora.start()
                self.ready = True
                used = self.lora.port or 'unknown'
                mac = getattr(self.lora, "ident_mac", None)
                if mac:
                    logger.info("GateControl Communicator ready on %s with MAC: %s", used, mac)
                    if 'manual' in args:
                        self._rhapi.ui.message_notify(self._rhapi.__("GateControl Communicator ready on {} with MAC: {}").format(used, mac))
                return
            else:
                self.ready = False
                logger.warning("No GateControl Communicator module discovered or configured")
                if 'manual' in args:
                    self._rhapi.ui.message_notify(self._rhapi.__("No GateControl Communicator module discovered or configured"))
        except Exception as ex:
            self.ready = False
            logger.error("LoRaUSB init failed: %s", ex)
            if 'manual' in args:
                self._rhapi.ui.message_notify(self._rhapi.__("Failed to initialize communicator: {}").format(str(ex)))

    def onRaceStart(self, _args):
        #TODO: Schedule start message per race
        logger.warning("GateControl Race Start Event")
        #if self.ready:
            #osdData = GATE_Data(0, 0, 0)
            #self.sendBroadcastMessage(osdData)

    def onRaceFinish(self, _args):
        logger.warning("GateControl Race Finish Event")
        #if self.ready:
            #osdData = GATE_Data(0, 0, 0)
            #self.sendBroadcastMessage(osdData)

    def onRaceStop(self, _args):
        logger.warning("GateControl Race Stop Event")
        #if self.ready:
            #osdData = GATE_Data(0, 0, 0)
            #self.sendBroadcastMessage(osdData)

    def onSendMessage(self, args):
        logger.warning("Event onSendMessage")
        #if self.ready:
            #osdData = GATE_Data(0, 0, 0)
            #self.sendBroadcastMessage(osdData)
    
    def discoveryAction(self, args):
        
        group_selected = int(self._rhapi.db.option('esp_gc_assignToGroup', None)) #returns exact string from device config field
        new_group_str = self._rhapi.db.option('esp_gc_assignToNewGroup', None) # TESTING - could also change None to an empty string? check docs
        
        #add logic: check if the selected group == "New Group" if yes, then use the new_group_str to search for already existing group in list (may happen if user types in an existing name instead of a new one)
        # last element is always the "New Group" element which is only visible in UI

        if group_selected == 0: # last element selected="New Group" indices end one number lower than the lenght of list (index is starting from 0)
            #autogenerate new groupname based on current time
            if(not new_group_str or len(new_group_str) == 0):
                new_group_str = "New Group"

            new_group_str += " " + datetime.now().strftime('%Y%m%d_%H%M%S')
            #gc_grouplist.append(GC_DeviceGroup(new_group_str))
            group_selected=len(gc_grouplist) # create a new group with index equal to the current length of the gc_grouplist
        
        '''group_index=-1
        for i, group in enumerate(gc_grouplist):
            if group.name == new_group_str:
                group_index=i
                break
        if group_index is -1:
            gc_grouplist.append(GC_DeviceGroup(new_group_str))
            group_index=len(gc_grouplist)-1'''

        #groupId=gc_grouplist.index(group_input)
        #self._rhapi.db.option_set('esp_gc_assignToGroup', group)

        num_found=self.getDevices(groupFilter=0,addToGroup=group_selected) # only discover unconfigured devices (groupID=0)

        if(num_found>0 and group_selected==len(gc_grouplist)): # a device replied and was set to our group and the groupID is not there yet
            gc_grouplist.append(GC_DeviceGroup(new_group_str)) # only add new group if we detected at least one device that has been added to the new groupId
            self.uiGroupList = self.createUiGroupList()
            self.uiDiscoveryGroupList = self.createUiGroupList(True)
            self.register_settings()
            self.register_quickset_ui()
            self.registerActions()
            self._rhapi.ui.broadcast_ui('settings')
            self._rhapi.ui.broadcast_ui('run')

    #groupFilter: 255(default, ignore groups, get all devices), 0(unconfigured devices) 1-254(valid groups)
    #optionally: query status of one device specifically
    #if no targetDevice obj is supplied a broadcast will be done, querying all devices of the group in groupFilter
    #if a targetDevice obj is supplied to the function this device will be called by its mac and the group is read back and updated in gc_devicelist
    def getDevices(self, groupFilter=255, targetDevice=None, addToGroup=-1):
        if not getattr(self, "lora", None):
            logger.warning("getDevices: communicator not ready")
            return 0

        if targetDevice is None:
            recv3 = b'\xFF\xFF\xFF'
            groupId = int(groupFilter) & 0xFF
        else:
            recv3 = _mac_last3_from_hex(targetDevice.addr)
            groupId = int(targetDevice.groupId) & 0xFF

        # Queue leeren und Request senden
        self.lora.drain_events(0.0)
        logger.debug("GET_DEVICES -> recv3=%s group=%d flags=%d", recv3.hex().upper(), groupId, 0)
        self.lora.send_get_devices(recv3=recv3, group_id=groupId, flags=0)

        found = 0
        window_deadline = None
        hard_fallback = time.time() + 6.0     # harter Fallback, falls OPEN/CLOSED nicht kommen

        global gc_devicelist
        while True:
            # Events gepuffert abholen (Reader-Thread liefert kontinuierlich)
            for ev in self.lora.drain_events(timeout_s=0.1):
                t = ev.get("type")

                if t == EV_ERROR:
                    logger.error("Transport error: %s", ev)

                elif t == EV_RX_WINDOW_OPEN:
                    # Deadline anhand gemeldeter Fensterzeit kalkulieren (zzgl. Sicherheitszuschlag)
                    ms = int(ev.get("window_ms", 0))
                    window_deadline = time.time() + (ms / 1000.0) + 0.4
                    logger.debug("RX window open for %d ms (deadline %.3fs)", ms, window_deadline)

                elif t == EV_RX_WINDOW_CLOSED:
                    logger.debug("RX window closed (delta=%s) -> finish discovery", ev.get("rx_count_delta"))
                    hard_fallback = time.time() - 1.0  # Loop verlassen
                    break

                elif ev.get("opc") == LP.OPC_DEVICES and ev.get("reply") == "IDENTIFY_REPLY":
                    mac6 = ev.get("mac6", b"")
                    mac_hex = mac6.hex().upper()
                    logger.debug("Identify Reply MAC6: %s", mac_hex)

                    dev = self.getDeviceFromAddress(mac_hex)
                    if dev is None:
                        logger.info("New device discovered: %s", mac_hex)
                        dev = GC_Device(addr=mac_hex, type=GC_Type.WLED_CUSTOM, name=f"WLED {mac_hex}")
                        gc_devicelist.append(dev)
                        if hasattr(self, "createUiDevList"):
                            self.uiDeviceList = self.createUiDevList()

                    dev.update_from_identify(
                        ev.get("version", 0),
                        ev.get("caps", 0),
                        ev.get("groupId", 0),
                        mac6,
                        ev.get("host_rssi", 0),
                        ev.get("host_snr", 0),
                    )
                    found += 1

            # Beenden, wenn CLOSED kam (hard_fallback in die Vergangenheit gesetzt)
            if time.time() > hard_fallback:
                break

            # Beenden, wenn wir ein OPEN gesehen haben und die Deadline + Toleranz erreicht ist
            if window_deadline and time.time() > (window_deadline + 0.2):
                logger.debug("RX window deadline reached without CLOSED -> finishing")
                break

            # Andernfalls weiterpolling
            # (keine zusätzliche Verlängerung – OPEN bestimmt die Länge)
            continue

        # Optional: unkonfigurierte Geräte der gewünschten Gruppe zuweisen
        if addToGroup > 0 and addToGroup < 255:
            for dev in list(gc_devicelist):
                if dev.groupId == 0:
                    dev.groupId = addToGroup
                    self.setGateGroupId(dev)

        if hasattr(self, "_rhapi") and hasattr(self._rhapi, "ui"):
            self._rhapi.ui.message_notify(
                "Device Discovery finished with {} devices found and added to GroupId: {}".format(found, addToGroup)
            )
        return found
    
    def getStatus(self, groupFilter=255, targetDevice=None):
        if not getattr(self, "lora", None):
            logger.warning("getStatus: communicator not ready")
            return 0

        if targetDevice is None:
            recv3 = b'\xFF\xFF\xFF'
            groupId = int(groupFilter) & 0xFF
        else:
            recv3 = _mac_last3_from_hex(targetDevice.addr)
            groupId = int(targetDevice.groupId) & 0xFF

        self.lora.drain_events(0.0)
        self.lora.send_get_status(recv3=recv3, group_id=groupId, flags=0)

        updated = 0
        window_deadline = None
        hard_fallback = time.time() + 6.0

        while True:
            for ev in self.lora.drain_events(timeout_s=0.1):
                t = ev.get("type")

                if t == EV_RX_WINDOW_OPEN:
                    ms = int(ev.get("window_ms", 0))
                    window_deadline = time.time() + (ms / 1000.0) + 0.4

                elif t == EV_RX_WINDOW_CLOSED:
                    hard_fallback = time.time() - 1.0
                    break

                elif ev.get("opc") == LP.OPC_STATUS and ev.get("reply") == "STATUS_REPLY":
                    sender3_hex = self._to_hex_str(ev.get("sender3"))
                    match = self.getDeviceFromAddress(sender3_hex)
                    if match:
                        match.update_from_status(
                            ev.get("state", 0),
                            ev.get("effect", 0),
                            ev.get("brightness", 0),
                            ev.get("vbat_mV", 0),
                            ev.get("node_rssi", 0),
                            ev.get("node_snr", 0),
                            ev.get("host_rssi", 0),
                            ev.get("host_snr", 0),
                        )
                        updated += 1
                        logger.debug("STATUS_REPLY from %s", sender3_hex)

            if time.time() > hard_fallback:
                break
            if window_deadline and time.time() > (window_deadline + 0.2):
                break

        return updated
 
    # setGateGroupId will send the GroupID from devicelist to the device, 
    # idea: combine the sendGateControl and the setGateGroupId Function as they are doing basically the same.
    # difference is only the cmd and
    def setGateGroupId(self, targetDevice: "GC_Device", forceSet: bool = False, wait_for_ack: bool = True) -> bool:
        if not getattr(self, "lora", None):
            logger.warning("setGateGroupId: communicator not ready")
            return False

        recv3 = _mac_last3_from_hex(targetDevice.addr)
        group_id = int(targetDevice.groupId) & 0xFF
        is_broadcast = (recv3 == b'\xFF\xFF\xFF')

        self.lora.drain_events(0.0)

        # gezielt: last_ack leeren und Startzeit merken
        if not is_broadcast:
            targetDevice.ack_clear()
        send_t0 = time.time()

        self.lora.send_set_group(recv3, group_id)
        logger.debug("Setting Device %s to Group ID %d", targetDevice.addr, group_id)

        if not wait_for_ack or is_broadcast:
            return True

        window_deadline = None
        hard_fallback = time.time() + 6.0

        while True:
            for ev in self.lora.drain_events(timeout_s=0.1):
                t = ev.get("type")

                if t == EV_RX_WINDOW_OPEN:
                    ms = int(ev.get("window_ms", 0))
                    window_deadline = time.time() + (ms / 1000.0) + 0.4

                elif t == EV_RX_WINDOW_CLOSED:
                    hard_fallback = time.time() - 1.0
                    break

                elif ev.get("opc") == LP.OPC_ACK:
                    self._handle_ack_event(ev)

            if time.time() > hard_fallback:
                break
            if window_deadline and time.time() > (window_deadline + 0.2):
                break

        ok = (targetDevice.ack_ok()
            and targetDevice.last_ack.get("opcode") == LP.OPC_SET_GROUP
            and targetDevice.last_ack.get("ts", 0) > send_t0)

        if not ok:
            logger.warning("No ACK_OK for SET_GROUP to %s (status=%s, opcode=%s, ts_ok=%s)",
                        targetDevice.addr,
                        targetDevice.last_ack.get("status"),
                        targetDevice.last_ack.get("opcode"),
                        targetDevice.last_ack.get("ts", 0) > send_t0)
        return ok

    # Configure all devices in gc_devicelist with the groupIds stored here in RH. This will also affect previously configured devices (overwrite of configured groupId)
    # if sanityCheck=true(default) then gc_devicelist entries are changed to 0 if the groupId does not exist. if sanityCheck=false then no changes to gc_devicelist are made
    def forceGroups(self, args=None, sanityCheck:bool=True):
        
        logger.debug("Forcing all known devices to their stored groups.")
        #check length of grouplist. Every device from gc_devicelist with a groupId >= length of gc_grouplist will be set to groupId 0, also check for groupId 1,2 and reset to 0
        num_groups=len(gc_grouplist)

        for device in gc_devicelist: # iterate the devicelist and update all devices with the matching groupId to the state that will be sent out
            if sanityCheck==True and device.groupId >= num_groups: # group is not devicetype dependent - as long as the groupId matches the device will be updated
                device.groupId = 0
            self.setGateGroupId(device, forceSet=True) #override groupId (forceSet)
            time.sleep(0.2) #wait 200ms before sending next device

    # Called from gateSwitch
    # not used at all currently - effectively replaced by sendGroupControl because we now use broadcasts instead of individual MAC based commands
    # could be used but right now it was more effective to use individual specialty functions like setGateGroupId for MAC-based commands
    def sendGateControl(self, targetDevice, state, effect, brightness):
        """Gezielte WLED_CONTROL an einen einzelnen Knoten (last3 aus targetDevice.addr)."""
        if not getattr(self, "lora", None):
            logger.warning("sendGateControl: communicator not ready")
            return
        recv3 = _mac_last3_from_hex(targetDevice.addr)
        groupId = int(targetDevice.groupId) & 0xFF  # je nach FW-Konzept; sonst 0 lassen
        self.lora.send_wled_control(
            recv3,
            groupId,
            int(state) & 0xFF,
            int(effect) & 0xFF,
            int(brightness) & 0xFF,
        )
        # lokalen Cache updaten
        targetDevice.state = int(state); targetDevice.effect = int(effect); targetDevice.brightness = int(brightness)
        logger.debug("GC: Updated Device {}: State={}, Effect={}, Brightness={}".format(targetDevice.addr, targetDevice.state, targetDevice.effect, targetDevice.brightness))

    # Used for UI Quickset, Event based Actions - effectively replaces sendGateControl
    def sendGroupControl(self, gcGroupId, gcState, gcEffect, gcBrightness):
        """Broadcast WLED_CONTROL an Gruppe; lokale Cache-States aktualisieren."""
        if not getattr(self, "lora", None):
            logger.warning("sendGroupControl: communicator not ready")
            return

        # lokale UI/Caches pflegen (dein bisheriges Verhalten)
        # TODO: prüfen, was noch gebraucht wird neue devices werden mit device type 24 angelegt und sollte auch bei ESPNOW_GATE (20) angesprochen werden
        # hierarchie wie früher parent:espnow_gate children: basic_ir_gate, wled_custom
        # funktioniert nicht, da die nodes diese logik nicht kennen - also nur gleiche device types ansprechen oder groupDeviceType==0 (alle im groupId)
        # idee: ein flag in der discovery_reply einführen, um verschiedene unterklassen zu definieren, die neue oberklasse ist dann einfach 0 (alle) oder LP.TYPE_WLED_CUSTOM
        # TODO: am besten erstmal alle device types bis auf wled_custom entfernen und nur noch wled_custom nutzen - dann ist die logik klarer
        
        groupDeviceType = int(gc_grouplist[gcGroupId].device_type)
        for device in gc_devicelist:
            if groupDeviceType == 0:
                if device.groupId == gcGroupId:
                    device.state = int(gcState); device.effect = int(gcEffect); device.brightness = int(gcBrightness)
            elif groupDeviceType == int(GC_Type.ESPNOW_GATE):
                if device.type in (int(GC_Type.BASIC_IR_GATE), int(GC_Type.WLED_CUSTOM)):
                    device.state = int(gcState); device.effect = int(gcEffect); device.brightness = int(gcBrightness)
            elif groupDeviceType == int(device.type):
                device.state = int(gcState); device.effect = int(gcEffect); device.brightness = int(gcBrightness)

        # LoRaProto senden (Broadcast an last3=FFFFFF)
        self.lora.send_wled_control(
            b'\xFF\xFF\xFF',
            int(gcGroupId) & 0xFF,
            int(gcState) & 0xFF,
            int(gcEffect) & 0xFF,
            int(gcBrightness) & 0xFF,
        )

    def _handle_ack_event(self, ev: dict) -> None:
        """ACK vom Transport auf das passende GC_Device abbilden (überschreibt last_ack)."""
        try:
            sender3_hex = self._to_hex_str(ev.get("sender3"))  # bytes -> "A1B2C3"
            dev = self.getDeviceFromAddress(sender3_hex) if sender3_hex else None
            if not dev:
                return

            # bevorzugt geparste Felder verwenden, Fallback auf body_raw
            ack_of = ev.get("ack_of")
            ack_status = ev.get("ack_status")
            ack_seq = ev.get("ack_seq")
            host_rssi = ev.get("host_rssi")
            host_snr = ev.get("host_snr")
            
            if ack_of is None or ack_status is None:
                return
            
            logger.debug("ACK from %s: ack_of=%s status=%s seq=%s",
                        sender3_hex, ev.get("ack_of"), ev.get("ack_status"), ev.get("ack_seq"))

            dev.ack_update(int(ack_of), int(ack_status), ack_seq, host_rssi, host_snr)

        except Exception:
            logger.exception("ACK handling failed")

    def getDeviceFromAddress(self, addr: str) -> Optional["GC_Device"]:
        """MAC als String ohne Trennzeichen: 12 (voll) oder 6 (last3)."""
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            for d in gc_devicelist:
                if (d.addr or "").upper() == s:
                    return d
            return None
        if len(s) == 6:
            for d in gc_devicelist:
                if (d.addr or "").upper().endswith(s):
                    return d
            return None
        return None
    
    @staticmethod
    def _to_hex_str(addr: Union[str, bytes, bytearray, None]) -> str:
        if addr is None:
            return ""
        if isinstance(addr, (bytes, bytearray)):
            return bytes(addr).hex().upper()
        return str(addr).strip().replace(":", "").replace(" ", "").upper()


# --- im GC_Device-ctor (bestehende Felder beibehalten), optional ergänzen:
class GC_Device():
    def __init__(self, addr:str, type:int, name:str, groupId:int=0, version:int=0, caps:int=0,
                 voltage_mV:int=0, node_rssi:int=0, node_snr:int=0, state:int=1, effect:int=1, brightness:int=70):
        self.addr:str = addr
        self.type:int = type
        self.name:str = name
        self.version:int = version              # GateControl Version -> via IDENTIFY_REPLY
        self.caps:int = caps                       # capability flags (IDENTIFY_REPLY)
        self.voltage_mV:int = voltage_mV
        self.node_rssi:int = node_rssi
        self.node_snr:int = node_snr
        self.groupId:int = groupId
        self.state:int = state
        self.effect:int = effect
        self.brightness:int = brightness

        # neu/optional für Protokoll & Link-Metriken:

        self.host_rssi:int = 0                  # RSSI am Master (aus USB-Forward)
        self.host_snr:int = 0                   # SNR am Master (aus USB-Forward)
        self.last_seen_ts = 0               # Unixzeit letzte Antwort

        # ACK-Status: letzte ACK-Antwort des Geräts
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def update_from_identify(self, version, caps, groupId, mac6_bytes, host_rssi=None, host_snr=None):
        # Firmware-Version
        self.version = int(version) if version is not None else self.version
        self.caps = int(caps) if caps is not None else self.caps

        # Group nur überschreiben, wenn Gerät bisher “unconfigured” war – so bleibt deine lokale Zuweisung stabil
        if self.groupId == 0 and groupId:
            self.groupId = int(groupId) & 0xFF

        # MAC wird nicht überschrieben (bleibt immer gleich), wird nur bei Neuanlage gesetzt
        #if self.addr is None and mac6_bytes is not None:
        #    self.addr = mac6_bytes

        # Link-Metriken des Master-Empfangs (falls vorhanden)
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

        self.last_seen_ts = time.time()

    def update_from_status(self, state, effect, brightness, vbat_mV, node_rssi, node_snr, host_rssi=None, host_snr=None):
        # Zustand
        self.state = int(state) if state is not None else self.state
        self.effect = int(effect) if effect is not None else self.effect
        self.brightness = int(brightness) if brightness is not None else self.brightness
        # Telemetrie
        self.voltage_mV = int(vbat_mV) if vbat_mV is not None else self.voltage_mV
        self.node_rssi  = int(node_rssi) if node_rssi is not None else self.node_rssi
        self.node_snr   = int(node_snr) if node_snr is not None else self.node_snr
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)
        self.last_seen_ts = time.time()

    # --- ACK Helpers (generisch für jeden Opcode) ---
    def ack_clear(self) -> None:
        self.last_ack = {"ok": False, "opcode": None, "status": None, "seq": None, "ts": 0.0}

    def ack_update(self, opcode:int, status:int, seq:int|None=None, host_rssi=None, host_snr=None) -> None:
        self.last_ack = {
            "ok": (int(status) == 0),
            "opcode": int(opcode),
            "status": int(status),
            "seq": (int(seq) if seq is not None else None),
            "ts": time.time(),
        }
        if host_rssi is not None:
            self.host_rssi = int(host_rssi)
        if host_snr is not None:
            self.host_snr = int(host_snr)

    def ack_ok(self) -> bool:
        return bool(self.last_ack["ok"])


class GC_DeviceGroup():

    def __init__(self, name:str, static_group:int=0, device_type:int=0):
        self.name: str = name # UI Name of Device
        self.static_group: int = static_group # if static_group is false it needs to be initialized (device_indices will be ignored)
        self.device_type: int = int(device_type) #device number in the gc_devicelist


class GC_Type():
    IDENTIFY_COMMUNICATOR = 1 # Same as used from TBS Fusion OSD VRX Plugin
    #DISPLAY_DATA = 0x10 #Used with TBS Fusion OSD VRX Plugin - intend is to keep compatibility for running both codes on one esp comm device together
    
    ESPNOW_GATE = 20 # unified message structure - groups only work with this type 

    BASIC_IR_GATE = 21 # IR Area Controller will identify with this code, also accepts devType 20 (unified) commands
    CUSTOM_IR_GATE = 22 # not used currently

    WIZMOTE_GATE = 23 # standard WLED type (does not support self identification, or unified type commmands (20))
    WLED_CUSTOM = 24 # once custom WLED fw is built this will be the identifier
    
    GET_DEVICES = 30 # only devices with groupId != 0 should respond here
    SET_GROUP = 31 #send this command to make a device store the received groupId

    #def __repr__(self):
    #    return '<GATE> P{} L{}\n {}\n {}\n {}'.format(self.state, self.effect, self.brightness)
   

# reference                      MAC            Type                        Name                       Group   State   Effect  Brightness
#gc_backup_devicelist=[GC_Device('111111111111', GC_Type.WIZMOTE_GATE,    'Dummy IR Gate Area 1',     0,      1,      '01',   62)]
gc_backup_devicelist=[]

gc_backup_grouplist=[GC_DeviceGroup('All WLED Gates', 1, int(GC_Type.WLED_CUSTOM))]

gc_devicelist: list["GC_Device"] = []
gc_grouplist: list["GC_DeviceGroup"] = []

# GC Data Exporter write function
def gc_write_json(data):
    payload = json.dumps(data, indent='\t')
    #logger.debug(payload)

    return {
        'data': payload,
        'encoding': 'application/json',
        'ext': 'json'
    }

# GC Data Exporter data collector function
def gc_config_json_output(rhapi=None):
    payload = {} # dictionary
    payload['help'] = ['See help tags below current configuration elements']

    payload['gc_devices'] = [obj.__dict__ for obj in gc_devicelist]

    #json_string = json.dumps([obj.__dict__ for obj in gc_devicelist])
    
    payload['gc_groups'] = [obj.__dict__ for obj in gc_grouplist]

    #detailed help after active elements
    payload['help/gc_devices'] = ['Device List of known devices']
    payload['help/gc_devices/addr'] = ['MAC of the device without \':\' as separator']
    payload['help/gc_devices/type'] = ['BASIC_IR_GATE:21, CUSTOM_IR_GATE:22, WIZMOTE_GATE:23, WLED_CUSTOM:24']
    payload['help/gc_devices/name'] = ['UI: shown name of a device']
    payload['help/gc_devices/groupId'] = ['Used to group devices for control. Valid numbers start with 3 (0-2 are reserved for device type based groups)']
    payload['help/gc_devices/state'] = ['0:off, 1:on, other values are unused currently']
    payload['help/gc_devices/effect'] = ['1-255: correspond to predefined colors and effects. 1-7: colors, 10-13: effects available on all device types, 20-255: special effects (WLED only)']
    payload['help/gc_devices/brightness'] = ['0: off, 1-255:dimming, special function with value 1: IR Controllers will spam the \'darker\' signal to set IR devices to absolute minimum brightness.']
    payload['help/gc_groups'] = ['Lookup list for the groupId definitions in the device entries']
    payload['help/gc_groups/name'] = ['UI: shown name of a group']
    payload['help/gc_groups/static_group'] = ['0: normal, changeable group, 1: predefined group that will be read only in UI']
    payload['help/gc_groups/device_type'] = ['0:call all devices set to this group\'s id. Device_type: 20,21,22 - send to all devices of that type ignoring groupIds']
    payload['help/backup'] = ['If there is an issue with configuration you can create a clean config based on the example elements. (delete \'_backup\' from element name)']

    #add backup / example definitions at the end
    #payload['gc_devices_backup'] = [{"addr": "111111111111","type": 23,"name": "Dummy IR Gate Area 1","groupId": 0,"state": 1,"effect": "01","brightness": 70}, {"addr": "3030F918123C","type": 21,"name": "IR Controller 3030F918123C","groupId": 4,"state": 1,"effect": 1,"brightness": 1}]
    #payload['gc_groups_backup'] = [{'name': 'All WLED and IR Gates', 'static_group': 1, 'device_type': 20}, {'name': 'All WLED Gates', 'static_group': 1, 'device_type': 24}, {'name': 'All IR Gates', 'static_group': 1, 'device_type': 21}]
    payload['gc_devices_backup'] = [{"addr": "3C84279EBFE4","type": 24,"name": "WLED 3C84279EBFE4","groupId": 0,"state": 1,"effect": 1,"brightness": 70}]
    payload['gc_groups_backup'] = [{'name': 'All WLED Gates', 'static_group': 1, 'device_type': 24}]
    #logger.debug(payload)
    return payload

# GC Data Importer function: write imported data to DB
def gc_import_json(importer_class, rhapi, source, args):
    #source: imported data byte string (content of json file)
    #args: options (list) selected in UI during import: gc_import_devices, gc_import_devgroups
    #TODO improve error handling: currently after json.loads is sucessful there are no further checks:
    #   Parameters for devices could be missing but the string would be written to the DB and we would have a corrupted config instantly
    #   --> first parse config and only write to DB if there are no errors?
    #   --> also a roll back would be great.
    #if no file content is present cancel import here
    if not source:
        return False
    
    try:
        data = json.loads(source) #transfer loaded json data into parsable dictionary
    except Exception as ex:
        logger.error("Unable to import file: {}".format(str(ex)))
        return False
    
    if 'gc_import_devices' in args and args['gc_import_devices']:
        logger.debug("Checked Device Import Option")
        if 'gc_devices' in data:
            logger.debug("Importing GateControl Devices...")
            # data['gc_devices'] contains list of GC_Device config parameters (not GC_Device class objects)
            #save devices to database
            rhapi.db.option_set('esp_gc_device_config', str(data['gc_devices']))

            #gc_instance.load_from_db()
            
            '''gc_devicelist.clear()   #Delete old content of gc_devicelist
            for device in data['gc_devices']:
                logger.debug(device)
                gc_devicelist.append(GC_Device(device['addr'], device['type'], device['name']))'''

            #logger.debug(gc_devicelist[1].name) #check if devicelist is in the expected state after import - works

        else:
            #todo UI message: "JSON contains no GateControl Devices"
            logger.error("JSON contains no GateControl Devices")
            #return False

    if 'gc_import_devgroups' in args and args['gc_import_devgroups']:
        logger.debug("Checked Group Import Option")
        if 'gc_groups' in data:
            logger.debug("Importing GateControl Groups...")
            #save groups to database
            rhapi.db.option_set('esp_gc_groups_config', str(data['gc_groups']))

            '''gc_grouplist.clear()
            for group in data['gc_groups']:
                logger.debug(group)
                gc_grouplist.append(GC_DeviceGroup(group['name'], group['device_indices']))'''
        else:
            #todo UI message: "JSON contains no GateControl Device Groups"
            logger.error("JSON contains no GateControl Device Groups")
            #return False

    gc_instance.load_from_db()
    return True

#TODO on startup: load gc_devicelist and gc_grouplist from database - reuse parts of import function (separate the object list manipulation from the import function and reuse that)
# currently the device- and group-list are saved to DB and the list objects are updated but UI creation only runs at startup where the lists always contain the cardcoded elements.
# After a reboot the devicelist from this source code file will be used again, so the import has no effect
