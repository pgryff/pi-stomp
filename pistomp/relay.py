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

import logging
import os
from pathlib import Path
import RPi.GPIO as GPIO
import time


class Relay:

    def __init__(self, set_pin, reset_pin):
        self.enabled = False
        self.set_pin = set_pin
        self.reset_pin = reset_pin
        self.sentinel_file = os.path.join(os.path.expanduser("~"), ".relay_set%d" % set_pin)

        GPIO.setup(reset_pin, GPIO.OUT)
        GPIO.output(reset_pin, GPIO.LOW)
        GPIO.setup(set_pin, GPIO.OUT)
        GPIO.output(set_pin, GPIO.LOW)

    def init_state(self):
        set = os.path.isfile(self.sentinel_file)
        if set:
            self.enable()
        else:
            self.disable()
        return set

    def enable(self):
        GPIO.output(self.set_pin, GPIO.HIGH)
        time.sleep(0.04)
        self.enabled = True
        GPIO.output(self.set_pin, GPIO.LOW)
        logging.debug("Relay on: %d" % self.set_pin)

        Path(self.sentinel_file).touch()

    def disable(self):
        GPIO.output(self.reset_pin, GPIO.HIGH)
        time.sleep(0.04)
        self.enabled = False
        GPIO.output(self.reset_pin, GPIO.LOW)
        logging.debug("Relay off: %d" % self.reset_pin)

        if os.path.isfile(self.sentinel_file):
            os.remove(self.sentinel_file)

