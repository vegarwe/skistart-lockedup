import asyncio
import json
import nest_asyncio
import os
import time
import threading
import tornado.httpserver
import tornado.web
import tornado.websocket
import tornado.platform.asyncio

import RPi.GPIO as GPIO

from pynfc import Nfc, Desfire, TimeoutException, nfc

INPUT_PIN_0 = 18
INPUT_PIN_1 = 24
SCRIPT_PATH = os.path.dirname(os.path.abspath(__file__))

class SkiPort:
    def __init__(self, number):
        self.number = number
        self.card_uid = None
        self.door_state = 1
        self.door_status = 'open'

    def __str__(self):
        return f'{self.number} - {self.card_uid}'

class SkiManager:
    def __init__(self, count):
        if count > 2:
            raise Exception("Do not support more than two ports (yet)")
        self._ports = [SkiPort(i) for i in range(count)]
        self._input_pins = [INPUT_PIN_0, INPUT_PIN_1][:count]
        self._keep_running = False
        self._rfid_thread = None
        self._pin_thread = None

    @property
    def status(self):
        return json.dumps({
            'type': 'status',
            'rack': [
                {
                    "status":       'occupied' if i.card_uid else 'available',
                    "door_state":   i.door_state,
                    "door_status":  i.door_status,
                    "card_uid":     str(i.card_uid)
                }
                for i in self._ports]
            })

    def _send_status_change(self):
        async def _send_status_async():
            for ws_client in WSHandler.participants:
                ws_client.write_message(self.status)

        asyncio.get_event_loop().run_until_complete(_send_status_async())

    def _send_log(self, entry):
        async def _send_log_async():
            for ws_client in WSHandler.participants:
                ws_client.write_message(json.dumps({ 'type': 'log', 'entry': entry}))

        print(entry)
        asyncio.get_event_loop().run_until_complete(_send_log_async())

    def unlock(self, idx):
        print('unlock', idx)
        self._ports[idx].card_uid = None

        self._send_status_change()

    def handle_card(self, target):
        # Release port
        for port in self._ports:
            if  port.card_uid == target.uid:
                port.card_uid = None
                port.door_status = 'open' if port.door_state else 'unlocked'
                self._send_status_change()
                self._send_log('release port %s %s' % (target.uid, port))
                return

        # Assign port
        for port in self._ports:
            if  port.card_uid is None:
                port.card_uid = target.uid
                port.door_status = 'open' if port.door_state else 'locked'
                self._send_status_change()
                self._send_log('assign  port %s %s' % (target.uid, port))
                break
        else:
            # Rack is full
            self._send_log('rack is full %s' % (target.uid))

    def run_rfid(self):
        n = Nfc("pn532_uart:/dev/ttyUSB0:115200")

        try:
            last_uid = None
            last_timeout = None
            for target in n.poll():
                if not self._keep_running:
                    return
                if last_timeout and last_timeout < time.time():
                    last_timeout = None
                    last_uid = None
                    #print('waiting for card')
                if last_uid == target.uid:
                    last_timeout = time.time() + 0.5
                    time.sleep(.1)
                    continue

                last_uid = target.uid
                last_timeout = time.time() + 5

                try:
                    self.handle_card(target)
                except TimeoutException:
                    pass
        except KeyboardInterrupt:
            pass

    def set_door_state(self, idx, input_state):
        self._ports[idx].door_state = input_state
        if  self._ports[idx].card_uid is None:
            self._ports[idx].door_status = 'open'   if input_state else 'closed'
        else:
            self._ports[idx].door_status = 'forced' if input_state else 'locked'
            #if  self._ports[idx].door_status == 'open' and input_state:
            #    self._ports[idx].door_status = 'open - available'
            #else:
            #    self._ports[idx].door_status = 'forced' if input_state else 'locked'
        self._send_status_change()
        self._send_log('door state changed %s' % (self._ports[idx].door_status))

    def run_pin_state(self):
        GPIO.setmode(GPIO.BCM)
        for pin in self._input_pins:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        prev_input = [1    for _ in self._input_pins]
        curr_input = [None for _ in self._input_pins]
        while self._keep_running:
            time.sleep(0.2)
            for idx, pin in enumerate(self._input_pins):
                curr_input[idx] = GPIO.input(pin)

                if prev_input[idx] != curr_input[idx]:
                    self.set_door_state(idx, curr_input[idx])
                prev_input[idx] = curr_input[idx]

        GPIO.cleanup()

        # GPIO.add_event_detect(10,GPIO.RISING,callback=button_callback) # Setup event on pin 10 rising edge
        # GPIO.wait_for_edge(channel, GPIO.RISING)

    def start(self):
        self._keep_running = True

        self._rfid_thread = threading.Thread(target=self.run_rfid)
        self._rfid_thread.start()

        self._pin_thread = threading.Thread(target=self.run_pin_state)
        self._pin_thread.start()

    def stop(self):
        self._keep_running = False
        if self._rfid_thread:
            pass # TODO!!!
            #self._rfid_thread.join()
        self._rfid_thread = None
        self._pin_thread = None

    def __del__(self):
        self.stop()


class WSHandler(tornado.websocket.WebSocketHandler):
    skimanager = None
    participants = set()

    def check_origin(self, origin):
        return True

    def open(self, *args, **kwargs):
        print('connection opened')
        self.participants.add(self)
        self.write_message(self.skimanager.status)
        self.write_message(json.dumps({ 'type': 'log', 'entry': 'connected'}))

    def on_message(self, message):
        pass
        print('message received %s' % message)

    def on_close(self):
        print('connection closed')
        self.participants.remove(self)

class MainHandler(tornado.web.RequestHandler):
    skimanager = None
    config = {}

    def __check_auth(self):
        if 'auth_data' not in self.config:
            return True

        auth_cookie = self.get_secure_cookie("auth_data")
        if not auth_cookie:
            return False

        return auth_cookie.decode() == self.config['auth_data']

    def put(self, path):
        if not self.__check_auth():
            return self.send_error(401)
        if not self.skimanager:
            return self.send_error(412)

        if   path == 'api/cmd':
            cmd = json.loads(self.request.body.decode('utf-8'))
            print('cmd ', cmd, self.skimanager)
            self.set_status(200)
            self.write(self.skimanager.status)
        elif path == 'api/unlock':
            port = json.loads(self.request.body.decode('utf-8'))
            self.skimanager.unlock(port['number'])

            self.set_status(200)
            self.write(self.skimanager.status)
        else:
            self.send_error(405)

    def get(self, path):
        if path in ['', 'index.html']:
            self.set_status(200)
            self.set_header("Content-type", "text/html")
            self.write(open(os.path.join(SCRIPT_PATH, 'index.html')).read())
        #elif path in ['login', 'login.html']:
        #    if not self.__check_auth():
        #        self.set_status(200)
        #        self.set_header("Content-type", "text/html")
        #        self.write(open(os.path.join(SCRIPT_PATH, 'login.html')).read())
        #        return
        #    if 'auth_data' in self.config:
        #        self.set_secure_cookie("auth_data", self.config['auth_data'], secure=True, expires_days=900)
        #    self.redirect('index.html')
        else:
            self.send_error(404)

    def post(self, path):
        if path in ['login', 'login.html']:
            auth_data = self.get_body_argument("password", default=None, strip=False)
            print('todo', auth_data) # TODO
            #if auth_data == self.config['auth_data']:
            #    self.set_secure_cookie("auth_data", self.config['auth_data'], secure=True, expires_days=900)
            #    self.redirect('index.html')
            #else:
            #    self.set_status(200)
            #    self.set_header("Content-type", "text/html")
            #    self.write(open(os.path.join(SCRIPT_PATH, 'login.html')).read())
        else:
            self.send_error(404)

def main():
    nest_asyncio.apply()

    application = tornado.web.Application([
        (r'/ws', WSHandler),
        (r"/static/(.*)", tornado.web.StaticFileHandler, dict(path=SCRIPT_PATH)),
        (r'/(.*)', MainHandler),
    ], cookie_secret="d0870884-495c-4758-8c86-24383dc0ee69")

    tornado.platform.asyncio.AsyncIOMainLoop().install()
    loop = asyncio.get_event_loop()

    print('waiting for card, ctrl-c to abort')
    config = json.load(open(os.path.join(SCRIPT_PATH, 'server.json')))
    skimanager = SkiManager(2)
    skimanager.start()

    WSHandler.skimanager = skimanager
    MainHandler.skimanager = skimanager
    MainHandler.config = config

    http_server = tornado.httpserver.HTTPServer(application)
    if 'http_addr' in config:
        http_server.listen(config['http_port'], address=config['http_addr'])
    else:
        http_server.listen(config['http_port'])

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    print("Stopping loop")
    loop.stop()
    print("Stopping skimanager")
    skimanager.stop()

if __name__ == "__main__":
    main()


# DESFIRE_DEFAULT_KEY = b'\x00' * 8
# MIFARE_BLANK_TOKEN = b'\xFF' * 1024 * 4
#did_auth = target.auth(DESFIRE_DEFAULT_KEY if type(target) == Desfire else MIFARE_BLANK_TOKEN)


#import pynfc
#print(pynfc)
#/home/vegarwe/.local/lib/python3.9/site-packages/pynfc/__init__.py
#import sys
#sys.exit(1)

#nfc_initiator_target_is_present.argtypes   = [ctypes.POINTER(struct_nfc_device), ctypes.POINTER(struct_c__SA_nfc_target)]
#nfc_initiator_poll_target.argtypes         = [ctypes.POINTER(struct_nfc_device), ctypes.POINTER(struct_c__SA_nfc_modulation),
#                                                       size_t, uint8_t, uint8_t, ctypes.POINTER(struct_c__SA_nfc_target)]
#nfc_close.argtypes                         = [ctypes.POINTER(struct_nfc_device)]
# <pynfc.nfc.LP_struct_nfc_device object at 0xb633ab68> <pynfc.nfc.struct_c__SA_nfc_target object at 0xb633ae80>
#is_present = nfc.nfc_initiator_target_is_present
#print(target.uid)
#print(n.pdevice, target.target_ptr)
#while is_present(n.pdevice, target.target_ptr):
#    print('card present, waiting')
#    time.sleep(1)

