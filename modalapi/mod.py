# This file is part of pi-stomp.
#
# pi-stomp is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pi-stomp is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pi-stomp.  If not, see <https://www.gnu.org/licenses/>.

import json
import logging
import os
import requests as req
import subprocess
import sys
import time
import yaml

import common.token as Token
import common.util as util
import pistomp.analogswitch as AnalogSwitch
import pistomp.controller as Controller
import modalapi.pedalboard as Pedalboard

from pistomp.analogmidicontrol import AnalogMidiControl
from pistomp.footswitch import Footswitch
from enum import Enum
from pathlib import Path

#sys.path.append('/usr/lib/python3.5/site-packages')  # TODO possibly /usr/local/modep/mod-ui
#from mod.development import FakeHost as Host

class TopEncoderMode(Enum):
    DEFAULT = 0
    PRESET_SELECT = 1
    PRESET_SELECTED = 2
    PEDALBOARD_SELECT = 3
    PEDALBOARD_SELECTED = 4
    SYSTEM_MENU = 5

class BotEncoderMode(Enum):
    DEFAULT = 0
    DEEP_EDIT = 1
    VALUE_EDIT = 2


class Mod:
    __single = None

    def __init__(self, lcd):
        logging.info("Init mod")
        if Mod.__single:
            raise Mod.__single
        Mod.__single = self

        self.lcd = lcd
        self.root_uri = "http://localhost:80/"

        self.pedalboards = {}  # TODO make the ordering of entries deterministic
        self.pedalboard_list = []  # TODO LAME to have two lists
        self.selected_pedalboard_index = 0
        self.selected_preset_index = 0
        self.selected_plugin_index = 0
        self.selected_parameter_index = 0
        self.parameter_tweak_amount = 8

        self.plugin_dict = {}

        self.hardware = None

        self.top_encoder_mode = TopEncoderMode.DEFAULT
        self.bot_encoder_mode = BotEncoderMode.DEFAULT

        self.current = None  # pointer to Current class
        self.deep = None     # pointer to current Deep class

        self.selected_menu_index = 0
        self.menu_items = None

    # Container for dynamic data which is unique to the "current" pedalboard
    # The self.current pointed above will point to this object which gets
    # replaced when a different pedalboard is made current (old Current object
    # gets deleted and a new one added via self.set_current_pedalboard()
    class Current:
        def __init__(self, pedalboard):
            self.pedalboard = pedalboard
            self.presets = {}
            self.preset_index = 0
            self.analog_controllers = {}  # { type: (plugin_name, param_name) }

    class Deep:
        def __init__(self, plugin):
            self.plugin = plugin
            self.parameters = list(plugin.parameters.values())
            self.selected_parameter_index = 0
            self.selected_parameter = None
            self.value = 0  # TODO shouldn't need this

    #
    # Hardware
    #

    def add_hardware(self, hardware):
        self.hardware = hardware

    def top_encoder_sw(self, value):
        # State machine for top rotary encoder
        mode = self.top_encoder_mode
        if value == AnalogSwitch.Value.RELEASED:
            if mode == TopEncoderMode.PRESET_SELECT:
                self.top_encoder_mode = TopEncoderMode.PEDALBOARD_SELECT
            elif mode == TopEncoderMode.PEDALBOARD_SELECT:
                self.top_encoder_mode = TopEncoderMode.PRESET_SELECT
            elif mode == TopEncoderMode.PRESET_SELECTED:
                self.preset_change()
                self.top_encoder_mode = TopEncoderMode.PRESET_SELECT
            elif mode == TopEncoderMode.PEDALBOARD_SELECTED:
                self.pedalboard_change()
                self.top_encoder_mode = TopEncoderMode.DEFAULT
            elif mode == TopEncoderMode.SYSTEM_MENU:
                self.menu_action()
            else:
                if len(self.current.presets) > 0:
                    self.top_encoder_mode = TopEncoderMode.PRESET_SELECT
                else:
                    self.top_encoder_mode = TopEncoderMode.PEDALBOARD_SELECT
            self.update_lcd_title()
        elif value == AnalogSwitch.Value.LONGPRESSED:
            #if self.top_encoder_mode == TopEncoderMode.DEFAULT:
            #    logging.debug("double long")
            #    subprocess.call("/usr/local/modep/modep-btn-scripts/my_toggle_wifi_hotspot.sh")
            #else:
            if mode == TopEncoderMode.DEFAULT:
                self.top_encoder_mode = TopEncoderMode.SYSTEM_MENU
                self.system_menu_show()
            else:
                self.top_encoder_mode = TopEncoderMode.DEFAULT
                self.update_lcd()

    def top_encoder_select(self, direction):
        # State machine for top encoder switch
        mode = self.top_encoder_mode
        if mode == TopEncoderMode.PEDALBOARD_SELECT or mode == TopEncoderMode.PEDALBOARD_SELECTED:
            self.pedalboard_select(direction)
            self.top_encoder_mode = TopEncoderMode.PEDALBOARD_SELECTED
        elif mode == TopEncoderMode.PRESET_SELECT or mode == TopEncoderMode.PRESET_SELECTED:
            self.preset_select(direction)
            self.top_encoder_mode = TopEncoderMode.PRESET_SELECTED
        elif mode == TopEncoderMode.SYSTEM_MENU:
            self.menu_select(direction)

    def bottom_encoder_sw(self, value):
        # State machine for bottom rotary encoder switch
        if self.top_encoder_mode == TopEncoderMode.SYSTEM_MENU:
            return  # Ignore bottom encoder if top encoder has navigated to the system menu
        mode = self.bot_encoder_mode
        if value == AnalogSwitch.Value.RELEASED:
            if mode == BotEncoderMode.DEFAULT:
                self.toggle_plugin_bypass()
            elif mode == BotEncoderMode.DEEP_EDIT:
                self.menu_action()
            #elif mode == BotEncoderMode.VALUE_EDIT:
            #    self.parameter_value_change()
        elif value == AnalogSwitch.Value.LONGPRESSED:
            if mode == BotEncoderMode.DEFAULT or BotEncoderMode.VALUE_EDIT:
                self.bot_encoder_mode = BotEncoderMode.DEEP_EDIT
                self.parameter_edit_show()
            else:
                self.bot_encoder_mode = BotEncoderMode.DEFAULT
                self.update_lcd()

    def bot_encoder_select(self, direction):
        mode = self.bot_encoder_mode
        if mode == BotEncoderMode.DEFAULT:
            self.plugin_select(direction)
        elif mode == BotEncoderMode.DEEP_EDIT:
            #self.parameter_select(direction)
            self.menu_select(direction)
        elif mode == BotEncoderMode.VALUE_EDIT:
            self.parameter_value_change(direction)

    #
    # Pedalboard Stuff
    #

    def load_pedalboards(self):
        url = self.root_uri + "pedalboard/list"

        try:
            resp = req.get(url)
        except:  # TODO
            logging.error("Cannot connect to mod-host")
            sys.exit()

        if resp.status_code != 200:
            logging.error("Cannot connect to mod-host.  Status: %s" % resp.status_code)
            sys.exit()

        pbs = json.loads(resp.text)
        for pb in pbs:
            logging.info("Loading pedalboard info: %s" % pb[Token.TITLE])
            bundle = pb[Token.BUNDLE]
            title = pb[Token.TITLE]
            pedalboard = Pedalboard.Pedalboard(title, bundle)
            pedalboard.load_bundle(bundle, self.plugin_dict)
            self.pedalboards[bundle] = pedalboard
            self.pedalboard_list.append(pedalboard)
            #logging.debug("dump: %s" % pedalboard.to_json())

        # TODO - example of querying host
        #bund = self.get_current_pedalboard()
        #self.host.load(bund, False)
        #logging.debug("Preset: %s %d" % (bund, self.host.pedalboard_preset))  # this value not initialized
        #logging.debug("Preset: %s" % self.get_current_preset_name())

    def get_current_pedalboard_bundle_path(self):
        url = self.root_uri + "pedalboard/current"
        try:
            resp = req.get(url)
            # TODO pass code define
            if resp.status_code == 200:
                return resp.text
        except:
            return None

    def set_current_pedalboard(self, pedalboard):
        # Delete previous "current"
        del self.current

        # Create a new "current"
        self.current = self.Current(pedalboard)

        # Load Pedalboard specific config (overrides default set during initial hardware init)
        config_file = Path(pedalboard.bundle) / "config.yml"
        cfg = None
        if config_file.exists():
            with open(config_file.as_posix(), 'r') as ymlfile:
                cfg = yaml.load(ymlfile, Loader=yaml.SafeLoader)
        self.hardware.reinit(cfg)

        # Initialize the data
        self.bind_current_pedalboard()
        self.load_current_presets()
        self.update_lcd()

    def bind_current_pedalboard(self):
        # "current" being the pedalboard mod-host says is current
        # The pedalboard data has already been loaded, but this will overlay
        # any real time settings
        footswitch_plugins = []
        if self.current.pedalboard:
            #logging.debug(self.current.pedalboard.to_json())
            for plugin in self.current.pedalboard.plugins:
                if plugin is None or plugin.parameters is None:
                    continue
                for sym, param in plugin.parameters.items():
                    if param.binding is not None:
                        controller = self.hardware.controllers.get(param.binding)
                        if controller is not None:
                            # TODO possibly use a setter instead of accessing var directly
                            # What if multiple params could map to the same controller?
                            controller.parameter = param
                            controller.set_value(param.value)
                            plugin.controllers.append(controller)
                            if isinstance(controller, Footswitch):
                                # TODO sort this list so selection orders correctly (sort on midi_CC?)
                                plugin.has_footswitch = True
                                footswitch_plugins.append(plugin)
                            elif isinstance(controller, AnalogMidiControl):
                                self.current.analog_controllers[controller.type] = (plugin.instance_id, param.name)

            # Move Footswitch controlled plugins to the end of the list
            self.current.pedalboard.plugins = [elem for elem in self.current.pedalboard.plugins
                                               if elem.has_footswitch is False]
            self.current.pedalboard.plugins += footswitch_plugins

    def pedalboard_select(self, direction):
        cur_idx = self.selected_pedalboard_index
        next_idx = ((cur_idx - 1) if (direction is 1) else (cur_idx + 1)) % len(self.pedalboard_list)
        if self.pedalboard_list[next_idx].bundle in self.pedalboards:
            self.lcd.draw_title(self.pedalboard_list[next_idx].title, None, True, False)
            self.selected_pedalboard_index = next_idx

    def pedalboard_change(self):
        logging.info("Pedalboard change")
        if self.selected_pedalboard_index < len(self.pedalboard_list):
            self.lcd.draw_info_message("Loading...")

            resp1 = req.get(self.root_uri + "reset")
            if resp1.status_code != 200:
                logging.error("Bad Reset request")

            uri = self.root_uri + "pedalboard/load_bundle/"
            bundlepath = self.pedalboard_list[self.selected_pedalboard_index].bundle
            data = {"bundlepath": bundlepath}
            resp2 = req.post(uri, data)
            if resp2.status_code != 200:
                logging.error("Bad Rest request: %s %s  status: %d" % (uri, data, resp2.status_code))

            # Now that it's presumably changed, load the dynamic "current" data
            self.set_current_pedalboard(self.pedalboard_list[self.selected_pedalboard_index])

    #
    # Preset Stuff
    #

    def load_current_presets(self):
        url = self.root_uri + "snapshot/list"
        try:
            resp = req.get(url)
            if resp.status_code == 200:
                pass
        except:
            return None
        dict = json.loads(resp.text)
        for key, name in dict.items():
            if key.isdigit():
                index = int(key)
                self.current.presets[index] = name
        return resp.text

    def next_preset_index(self, dict, current, incr):
        # This essentially applies modulo to a set of potentially discontinuous keys
        # a missing key occurs when a preset is deleted
        indices = list(dict.keys())
        if current not in indices:
            return -1
        cur = indices.index(current)
        if incr:
            if cur < len(indices) - 1:
                return indices[cur + 1]
            return min(indices)
        else:
            if cur > 0:
                return indices[cur - 1]
            return max(indices)

    def preset_select(self, direction):
        index = self.next_preset_index(self.current.presets, self.selected_preset_index, direction is 1)
        if index < 0:
            return
        self.selected_preset_index = index
        self.lcd.draw_title(self.current.pedalboard.title, self.current.presets[index], False, True)

    def preset_change(self):
        index = self.selected_preset_index
        logging.info("preset change: %d" % index)
        self.lcd.draw_info_message("Loading...")
        url = (self.root_uri + "snapshot/load?id=%d" % index)
        # req.get(self.root_uri + "reset")
        resp = req.get(url)
        if resp.status_code != 200:
            logging.error("Bad Rest request: %s status: %d" % (url, resp.status_code))
        self.current.preset_index = index

        #load of the preset might have changed plugin bypass status
        self.preset_change_plugin_update()

    def preset_incr_and_change(self):
        self.preset_select(1)
        self.preset_change()

    def preset_decr_and_change(self):
        self.preset_select(-1)
        self.preset_change()

    def preset_change_plugin_update(self):
        # Now that the preset has changed on the host, update plugin bypass indicators
        for p in self.current.pedalboard.plugins:
            uri = self.root_uri + "effect/parameter/pi_stomp_get//graph" + p.instance_id + "/:bypass"
            try:
                resp = req.get(uri)
                if resp.status_code == 200:
                    p.set_bypass(resp.text == "true")
            except:
                logging.error("failed to get bypass value for: %s" % p.instance_id)
                continue
        self.lcd.draw_bound_plugins(self.current.pedalboard.plugins, self.hardware.footswitches)
        self.lcd.draw_plugins(self.current.pedalboard.plugins)
        self.lcd.draw_analog_assignments(self.current.analog_controllers)

    #
    # Plugin Stuff
    #

    def get_selected_instance(self):
        if self.current.pedalboard is not None:
            pb = self.current.pedalboard
            inst = pb.plugins[self.selected_plugin_index]
            if inst is not None:
                return inst
        return None

    def plugin_select(self, direction):
        #enc = encoder.get_data()
        if self.current.pedalboard is not None:
            pb = self.current.pedalboard
            index = ((self.selected_plugin_index + 1) if (direction is 1)
                    else (self.selected_plugin_index - 1)) % len(pb.plugins)
            #index = self.next_plugin(pb.plugins, enc)
            plugin = pb.plugins[index]  # TODO check index
            self.selected_plugin_index = index
            self.lcd.draw_plugin_select(plugin)

    def toggle_plugin_bypass(self):
        logging.debug("toggle_plugin_bypass")
        inst = self.get_selected_instance()
        if inst is not None:
            if inst.has_footswitch:
                for c in inst.controllers:
                    if isinstance(c, Footswitch):
                        c.toggle(0)
                        return
            # Regular (non footswitch plugin)
            url = self.root_uri + "effect/parameter/pi_stomp_set//graph%s/:bypass" % inst.instance_id
            value = inst.toggle_bypass()
            code = self.parameter_set_send(url, "1" if value else "0", 200)
            if (code != 200):
                inst.toggle_bypass()  # toggle back to original value since request wasn't successful
            self.update_lcd_plugins()

    #
    # Generic Menu functions
    #

    def menu_select(self, direction):
        index = ((self.selected_menu_index - 1) if (direction is not 1)
                 else (self.selected_menu_index + 1)) % (len(self.menu_items))
        self.lcd.menu_highlight(index)
        self.selected_menu_index = index

    def menu_action(self):
        item = list(sorted(self.menu_items))[self.selected_menu_index]
        self.menu_items[item][Token.ACTION]()

    def menu_back(self):
        self.top_encoder_mode = TopEncoderMode.DEFAULT
        self.bot_encoder_mode = BotEncoderMode.DEFAULT
        self.update_lcd()

    #
    # System Menu
    #

    def system_menu_show(self):
        self.menu_items = {"0": {Token.NAME: "< Back to main screen", Token.ACTION: self.menu_back},
                           "1": {Token.NAME: "Save current pedalboard", Token.ACTION: self.system_menu_save_current_pb},
                           "2": {Token.NAME: "Soft restart & reload", Token.ACTION: self.system_menu_reload},
                           "3": {Token.NAME: "Restart sound engine", Token.ACTION: self.system_menu_restart_sound},
                           "4": {Token.NAME: "Hardware reboot", Token.ACTION: self.system_menu_reboot}}
        self.lcd.menu_show("System menu", self.menu_items)
        self.selected_menu_index = 0
        self.lcd.menu_highlight(0)

    def system_menu_save_current_pb(self):
        logging.debug("save current")
        # TODO this works to save the pedalboard values, but just default, not Preset values
        # Figure out how to save preset (host.py:preset_save_replace)
        # TODO this also causes a problem if self.current.pedalboard.title != mod-host title
        # which can happen if the pedalboard is changed via MOD UI, not via hardware
        url = self.root_uri + "pedalboard/save"
        try:
            resp = req.post(url, data={"asNew": "0", "title": self.current.pedalboard.title})
            if resp.status_code != 200:
                logging.error("Bad Rest request: %s status: %d" % (url, resp.status_code))
            else:
                logging.debug("saved")
        except:
            logging.error("status %s" % resp.status_code)
            return

    def system_menu_reload(self):
        logging.info("Exiting main process, systemctl should restart if enabled")
        sys.exit(0)

    def system_menu_restart_sound(self):
        self.lcd.splash_show()
        logging.info("Restart sound engine (jack)")
        os.system('systemctl restart jack')

    def system_menu_reboot(self):
        self.lcd.splash_show()
        logging.info("Hardware Reboot")
        os.system('systemctl reboot')

    #
    # Parameter Edit
    #

    def parameter_edit_show(self):
        plugin = self.get_selected_instance()
        self.deep = self.Deep(plugin)  # TODO this creates a new obj every time menu is shown, singleton?
        self.deep.selected_parameter_index = 0
        self.menu_items = {"0": {"name": "< Back to main screen", "action": self.menu_back}}
        i = 1
        for p in self.deep.parameters:
            self.menu_items[str(i)] = {Token.NAME: p.name,
                                       Token.ACTION: self.parameter_value_show,
                                       Token.PARAMETER: p}
            i = i + 1
        self.lcd.menu_show(plugin.instance_id, self.menu_items)
        self.selected_menu_index = 0
        self.lcd.menu_highlight(0)

    def parameter_value_show(self):
        self.bot_encoder_mode = BotEncoderMode.VALUE_EDIT
        item = list(sorted(self.menu_items))[self.selected_menu_index]
        if not item:
            return
        param = self.menu_items[item][Token.PARAMETER]
        self.deep.selected_parameter = param
        self.lcd.draw_value_edit(self.deep.plugin.instance_id, param, param.value)

    def parameter_value_change(self, direction):
        param = self.deep.selected_parameter
        value = float(param.value)
        # TODO tweak value won't change from call to call, cache it
        tweak = util.renormalize_float(self.parameter_tweak_amount, 0, 127, param.minimum, param.maximum)
        new_value = round(((value - tweak) if (direction is not 1) else (value + tweak)), 2)
        if new_value > param.maximum:
            new_value = param.maximum
        if new_value < param.minimum:
            new_value = param.minimum
        if new_value is value:
            return
        self.deep.selected_parameter.value = new_value  # TODO somewhat risky to change value before committed
        self.parameter_value_commit()
        self.lcd.draw_value_edit_graph(param, new_value)

    def parameter_value_commit(self):
        param = self.deep.selected_parameter
        url = self.root_uri + "effect/parameter/pi_stomp_set//graph%s/%s" % (self.deep.plugin.instance_id, param.symbol)
        formatted_value = ("%.1f" % param.value)
        self.parameter_set_send(url, formatted_value, 200)

    def parameter_set_send(self, url, value, expect_code):
        logging.debug("request: %s" % url)
        try:
            resp = None
            if value is not None:
                logging.debug("value: %s" % value)
                resp = req.post(url, json={"value": value})
            if resp.status_code != expect_code:
                logging.error("Bad Rest request: %s status: %d" % (url, resp.status_code))
            else:
                logging.debug("Parameter %s changed to: %d" % (param.name, new_value))
        except:
            logging.debug("status: %s" % resp.status_code)
            return resp.status_code

    #
    # LCD Stuff
    #

    def update_lcd(self):  # TODO rename to imply the home screen
        self.update_lcd_title()
        self.lcd.draw_analog_assignments(self.current.analog_controllers)
        self.lcd.draw_plugins(self.current.pedalboard.plugins)
        self.lcd.draw_bound_plugins(self.current.pedalboard.plugins, self.hardware.footswitches)
        self.lcd.draw_plugin_select()

    def update_lcd_title(self):
        invert_pb = False
        invert_pre = False
        if self.top_encoder_mode == TopEncoderMode.PEDALBOARD_SELECT:
            invert_pb = True
        if self.top_encoder_mode == TopEncoderMode.PRESET_SELECT:
            invert_pre = True
        self.lcd.draw_title(self.current.pedalboard.title,
                            util.DICT_GET(self.current.presets, self.current.preset_index), invert_pb, invert_pre)

    def update_lcd_plugins(self):
        self.lcd.draw_plugins(self.current.pedalboard.plugins)

    def update_lcd_fs(self):
        self.lcd.draw_bound_plugins(self.current.pedalboard.plugins, self.hardware.footswitches)
