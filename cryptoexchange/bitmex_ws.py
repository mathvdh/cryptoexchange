#!/usr/bin/env python3
import sys
import websocket
import threading
import traceback
from time import sleep
import json
import string
import logging
import urllib.parse
import math
import time
import hmac
import hashlib

def generate_nonce():
    return int(round(time.time() * 1000))

# Generates an API signature.
# A signature is HMAC_SHA256(secret, verb + path + nonce + data), hex encoded.
# Verb must be uppercased, url is relative, nonce must be an increasing 64-bit integer
# and the data, if present, must be JSON without whitespace between keys.
#
# For example, in psuedocode (and in real code below):
#
# verb=POST
# url=/api/v1/order
# nonce=1416993995705
# data={"symbol":"XBTZ14","quantity":1,"price":395.01}
# signature = HEX(HMAC_SHA256(secret, 'POST/api/v1/order1416993995705{"symbol":"XBTZ14","quantity":1,"price":395.01}'))
def generate_signature(secret, verb, url, nonce, data):
    """Generate a request signature compatible with BitMEX."""
    # Parse the url so we can remove the base and extract just the path.
    parsedURL = urllib.parse.urlparse(url)
    path = parsedURL.path
    if parsedURL.query:
        path = path + '?' + parsedURL.query

    # print "Computing HMAC: %s" % verb + path + str(nonce) + data
    message = bytes(verb + path + str(nonce) + data, 'utf-8')

    signature = hmac.new(secret.encode('utf-8'),
                         message,
                         digestmod=hashlib.sha256).hexdigest()
    return signature


# Naive implementation of connecting to BitMEX websocket for streaming realtime data.
# The Marketmaker still interacts with this as if it were a REST Endpoint, but now it can get
# much more realtime data without polling the hell out of the API.
#
# The Websocket offers a bunch of data as raw properties right on the object.
# On connect, it synchronously asks for a push of all this data then returns.
# Right after, the MM can start using its data. It will be updated in realtime, so the MM can
# poll really often if it wants.
class BitMEXWebsocket():

    def __init__(self, endpoint="", symbol="XBU24H", API_KEY=None, API_SECRET=None, LOGIN=None, PASSWORD=None):
        '''Connect to the websocket and initialize data stores.'''
        self.logger = logging.getLogger('root')
        self.logger.debug("Initializing WebSocket.")
        self.endpoint = endpoint
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.login = LOGIN
        self.password = PASSWORD
        self.data = {}
        self.keys = {}

        # We can subscribe right in the connection querystring, so let's build that.
        # Subscribe to all pertinent endpoints
        wsURL = self.__get_url(symbol)
        self.logger.info("Connecting to %s" % wsURL)
        self.__connect(wsURL, symbol)
        self.logger.info('Connected to WS.')

        # Connected. Push symbols
        self.__push_account()
        self.__push_symbol(symbol)
        self.logger.info('Got all market data. Starting.')

    def exit(self):
        self.exited = True
        self.ws.close()

    def get_instrument(self):
        # Turn the 'tickSize' into 'tickLog' for use in rounding
        instrument = self.data['instrument'][0]
        instrument['tickLog'] = int(math.fabs(math.log10(instrument['tickSize'])))
        return instrument

    def get_ticker(self):
        '''Return a ticker object. Generated from quote and trade.'''
        lastQuote = self.data['quote'][-1]
        lastTrade = self.data['trade'][-1]
        ticker = {
            "last": lastTrade['price'],
            "buy": lastQuote['bidPrice'],
            "sell": lastQuote['askPrice'],
            "mid": (float(lastQuote['bidPrice'] or 0) + float(lastQuote['askPrice'] or 0)) / 2
        }

        # The instrument has a tickSize. Use it to round values.
        instrument = self.data['instrument'][0]
        return {k: round(float(v or 0), instrument['tickLog']) for k, v in list(ticker.items())}

    def funds(self):
        return self.data['margin'][0]

    def market_depth(self):
        return self.data['orderBook25']

    def open_orders(self, clOrdIDPrefix):
        orders = self.data['order']
        # Filter to only open orders (leavesQty > 0) and those that we actually placed
        return [o for o in orders if str(o['clOrdID']).startswith(clOrdIDPrefix) and o['leavesQty'] > 0]

    def recent_trades(self):
        return self.data['trade']

    def __connect(self, wsURL, symbol):
        '''Connect to the websocket in a thread.'''
        self.logger.debug("Starting thread")

        self.ws = websocket.WebSocketApp(wsURL,
                                         on_message=self.__on_message,
                                         on_close=self.__on_close,
                                         on_open=self.__on_open,
                                         on_error=self.__on_error,
                                         # We can login using email/pass or API key
                                         header=self.__get_auth())

        self.wst = threading.Thread(target=lambda: self.ws.run_forever())
        self.wst.daemon = True
        self.wst.start()
        self.logger.debug("Started thread")

        # Wait for connect before continuing
        conn_timeout = 5
        while not self.ws.sock or not self.ws.sock.connected and conn_timeout:
            sleep(1)
            conn_timeout -= 1
        if not conn_timeout:
            self.logger.error("Couldn't connect to WS! Exiting.")
            self.exit()
            sys.exit(1)

    def __get_auth(self):
        '''Return auth headers. Will use API Keys if present in settings.'''
        if self.api_key == None and self.login == None:
            self.logger.error("No authentication provided! Unable to connect.")
            sys.exit(1)

        if self.api_key == None:
            self.logger.info("Authenticating with email/password.")
            return [
                "email: " + self.login,
                "password: " + self.password
            ]
        else:
            self.logger.info("Authenticating with API Key.")
            # To auth to the WS using an API key, we generate a signature of a nonce and
            # the WS API endpoint.
            nonce = generate_nonce()
            return [
                "api-nonce: " + str(nonce),
                "api-signature: " + generate_signature(self.api_secret, 'GET', '/realtime', nonce, ''),
                "api-key:" + self.api_key
            ]

    def __get_url(self, symbol):
        subscriptions = [sub + ':' + symbol for sub in ["order", "execution", "position", "quote", "trade"]]
        subscriptions += ["margin"]
        urlParts = list(urllib.parse.urlparse(self.endpoint))
        urlParts[0] = urlParts[0].replace('http', 'ws')
        urlParts[2] = "/realtime?subscribe=" + ",".join(subscriptions)
        return urllib.parse.urlunparse(urlParts)

    def __push_account(self):
        '''Ask the websocket for an account push. Gets margin, positions, and open orders'''
        self.__send_command("getAccount")
        # Wait for the keys to show up from the ws
        while not {'margin', 'position', 'order'} <= set(self.data):
            sleep(0.1)

    def __push_symbol(self, symbol):
        '''Ask the websocket for a symbol push. Gets instrument, orderBook, quote, and trade'''
        self.__send_command("getSymbol", symbol)
        while not {'instrument', 'trade', 'orderBook25'} <= set(self.data):
            sleep(0.1)

    def __send_command(self, command, args=[]):
        '''Send a raw command.'''
        self.ws.send(json.dumps({"op": command, "args": args}))

    def __on_message(self, ws, message):
        '''Handler for parsing WS messages.'''
        message = json.loads(message)
        self.logger.debug(json.dumps(message))

        table = message['table'] if 'table' in message else None
        action = message['action'] if 'action' in message else None
        try:
            if 'subscribe' in message:
                self.logger.debug("Subscribed to %s." % message['subscribe'])
            elif action:

                if table not in self.data:
                    self.data[table] = []

                # There are four possible actions from the WS:
                # 'partial' - full table image
                # 'insert'  - new row
                # 'update'  - update row
                # 'delete'  - delete row
                if action == 'partial':
                    self.logger.debug("%s: partial" % table)
                    self.data[table] += message['data']
                    # Keys are communicated on partials to let you know how to uniquely identify
                    # an item. We use it for updates.
                    self.keys[table] = message['keys']
                elif action == 'insert':
                    self.logger.debug('%s: inserting %s' % (table, message['data']))
                    self.data[table] += message['data']
                elif action == 'update':
                    self.logger.debug('%s: updating %s' % (table, message['data']))
                    # Locate the item in the collection and update it.
                    for updateData in message['data']:
                        item = findItemByKeys(self.keys[table], self.data[table], updateData)
                        if not item:
                            return  # No item found to update. Could happen before push
                        item.update(updateData)
                        # Remove cancelled / filled orders
                        if table == 'order' and item['leavesQty'] <= 0:
                            self.data[table].remove(item)
                elif action == 'delete':
                    self.logger.debug('%s: deleting %s' % (table, message['data']))
                    # Locate the item in the collection and remove it.
                    for deleteData in message['data']:
                        item = findItemByKeys(self.keys[table], self.data[table], deleteData)
                        self.data[table].remove(item)
                else:
                    raise Exception("Unknown action: %s" % action)
        except:
            self.logger.error(traceback.format_exc())

    def __on_error(self, ws, error):
        if not self.exited:
            self.logger.error("Error : %s" % error)
            sys.exit(1)

    def __on_open(self, ws):
        self.logger.debug("Websocket Opened.")

    def __on_close(self, ws):
        self.logger.info('Websocket Closed')
        sys.exit(1)

def findItemByKeys(keys, table, matchData):
    for item in table:
        matched = True
        for key in keys:
            if item[key] != matchData[key]:
                matched = False
        if matched:
            return item

if __name__ == "__main__":
    # create console handler and set level to debug
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    # create formatter
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    # add formatter to ch
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    ws = BitMEXWebsocket("https://testnet.bitmex.com/api/v1",
                         API_KEY="FOO",
                         API_SECRET="BAR")
    while(ws.ws.sock.connected):
        sleep(1)
