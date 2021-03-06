# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2019 grandizzy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from web3 import Web3
from pprint import pformat
from typing import List, Optional

from pymaker import Contract, Address, Transact, Wad
from pymaker.sign import eth_sign
from pymaker.util import http_response_summary
from pymaker.zrx import ZrxExchange, Order as ZrxOrder
from pymaker.token import ERC20Token

import logging
import datetime
import dateutil.parser
import requests
import json
import time


class Order:
    def __init__(self,
                 order_id: int,
                 pair: str,
                 is_sell: bool,
                 price: Wad,
                 amount: Wad):

        assert(isinstance(order_id, int))
        assert(isinstance(pair, str))
        assert(isinstance(is_sell, bool))
        assert(isinstance(price, Wad))
        assert(isinstance(amount, Wad))

        self.order_id = order_id
        self.pair = pair
        self.is_sell = is_sell
        self.price = price
        self.amount = amount

    @property
    def sell_to_buy_price(self) -> Wad:
        return self.price

    @property
    def buy_to_sell_price(self) -> Wad:
        return self.price

    @property
    def remaining_buy_amount(self) -> Wad:
        return self.amount*self.price if self.is_sell else self.amount

    @property
    def remaining_sell_amount(self) -> Wad:
        return self.amount if self.is_sell else self.amount*self.price

    def __repr__(self):
        return pformat(vars(self))

    @staticmethod
    def to_order(pair, order):
        is_sell = True if Wad.from_number(order['amount']) > Wad(0) else False
        amount = abs(Wad.from_number(order['amount']))
        return Order(order_id=order['id'],
                     is_sell=is_sell,
                     pair=pair,
                     price=amount/(amount * Wad.from_number(order['price'])),
                     amount=amount * Wad.from_number(order['price']))


class Trade:
    def __init__(self,
                 trade_id: id,
                 timestamp: int,
                 is_sell: bool,
                 pair: Optional[str],
                 price: Wad,
                 amount: Wad):
        assert(isinstance(trade_id, int))
        assert(isinstance(timestamp, int))
        assert(isinstance(is_sell, bool))
        assert(isinstance(pair, str) or (trade_id is None))
        assert(isinstance(price, Wad))
        assert(isinstance(amount, Wad))

        self.trade_id = trade_id
        self.timestamp = timestamp
        self.is_sell = is_sell
        self.pair = pair
        self.price = price
        self.amount = amount

    def __eq__(self, other):
        assert(isinstance(other, Trade))
        return self.trade_id == other.trade_id and \
               self.timestamp == other.timestamp and \
               self.is_sell == other.is_sell and \
               self.pair == other.pair and \
               self.price == other.price and \
               self.amount == other.amount

    def __hash__(self):
        return hash((self.trade_id,
                     self.timestamp,
                     self.is_sell,
                     self.pair,
                     self.price,
                     self.amount))

    def __repr__(self):
        return pformat(vars(self))

    @staticmethod
    def to_trade(trade):

        amount_orig = Wad.from_number(trade['amount_orig'])
        total = Wad.from_number(trade['price']) * amount_orig

        return Trade(trade_id=int(trade['id']),
                     timestamp=int(dateutil.parser.parse(trade['updated_at'] + 'Z').timestamp()),
                     is_sell=True if amount_orig > Wad(0) else False,
                     pair=trade['pair'],
                     price=amount_orig / total,
                     amount=abs(total))


class TEthfinexToken(ERC20Token):
    """A client for the `Trustless Ethfinex token wrappers.

    Attributes:
        web3: An instance of `Web` from `web3.py`.
        address: Ethereum address of the `TEthfinexToken` wrapper.
    """

    abi = Contract._load_abi(__name__, 'abi/TETHFINEX.abi')
    bin = Contract._load_bin(__name__, 'abi/TETHFINEX.bin')

    def __init__(self, web3, address, token: str):
        assert(isinstance(token, str))
        self.token = token

        super().__init__(web3, address)
        self._contract = self._get_contract(web3, self.abi, address)

    def deposit(self, amount: Wad, duration: int=500) -> Transact:
        """Locks `amount` of token in to `TEthfinexToken`.

        Args:
            amount: Amount of token to be locked to `TEthfinexToken`.
            duration: Period of time (in hours) for locking the amount
        Returns:
            A :py:class:`pymaker.Transact` instance, which can be used to trigger the transaction.
        """
        assert(isinstance(amount, Wad))
        assert(isinstance(duration, int))

        if self.token == "ETH":
            return Transact(self, self.web3, self.abi, self.address, self._contract, 'deposit',
                            [amount.value, duration], {'value': amount.value})
        else:
            return Transact(self, self.web3, self.abi, self.address, self._contract, 'deposit',
                            [amount.value, duration], {})

    def __repr__(self):
        return f"TEthfinexToken('{self.address}')"


class TEthfinexApi():
    """Ethfinex Trustless API interface.

    Developed according to the following manual:
    <https://blog.ethfinex.com/ethfinex-trustless-developer-guide/>.
    """

    logger = logging.getLogger()

    def __init__(self, tethfinex: ZrxExchange, api_server: str, timeout: float):
        assert(isinstance(tethfinex, ZrxExchange))
        assert(isinstance(api_server, str))
        assert(isinstance(timeout, float))

        self.tethfinex = tethfinex
        self.api_server = api_server
        self.timeout = timeout

    def get_symbols(self):
        return self._http_get("/v1/symbols", {})

    def get_config(self):
        return self._http_post("/trustless/v1/r/get/conf", {})

    def get_orders(self, pair: str) -> List[Order]:
        assert(isinstance(pair, str))

        result = self._get_orders(f"/trustless/v1/r/orders/t{pair}")

        return list(map(lambda order : Order.to_order(pair, order), result))

    def place_order(self,
                    is_sell: bool,
                    pay_token: Address,
                    pay_amount: Wad,
                    buy_token: Address,
                    buy_amount: Wad,
                    fee_address: Address,
                    pair: str) -> Order:

        assert(isinstance(is_sell, bool))
        assert(isinstance(pay_token, Address))
        assert(isinstance(pay_amount, Wad))
        assert(isinstance(buy_token, Address))
        assert(isinstance(buy_amount, Wad))
        assert(isinstance(fee_address, Address))
        assert(isinstance(pair, str))

        expiration = int((datetime.datetime.today() + datetime.timedelta(hours=6)).strftime("%s"))
        order = ZrxOrder(exchange=self.tethfinex,
                         maker=Address(self.tethfinex.web3.eth.defaultAccount),
                         taker=fee_address,
                         maker_fee=Wad(0),
                         taker_fee=Wad(0),
                         pay_token=pay_token,
                         pay_amount=(pay_amount / buy_amount) * buy_amount if is_sell else pay_amount,
                         buy_token=buy_token,
                         buy_amount=buy_amount if is_sell else (buy_amount / pay_amount) * pay_amount,
                         salt=self.tethfinex.random_salt(),
                         fee_recipient=fee_address,
                         expiration=expiration,
                         exchange_contract_address=self.tethfinex.address,
                         ec_signature_r=None,
                         ec_signature_s=None,
                         ec_signature_v=None)

        signed_order = self.tethfinex.sign_order(order)
        data = {
            "type": 'EXCHANGE LIMIT',
            "symbol": f"t{pair}",
            "amount": str(buy_amount) if is_sell else str(f"-{pay_amount}"),
            "price": str(pay_amount / buy_amount) if is_sell else str(buy_amount / pay_amount),
            "meta": signed_order.to_json(),
            "protocol": '0x'
        }

        side = "SELL" if is_sell else "BUY"
        self.logger.info(f"Placing order ({side}, amount {data['amount']} of {pair},"
                         f" price {data['price']})...")

        result = self._http_post("/trustless/v1/w/on", data)

        self.logger.info(f"Placed order  #{result[0]}")

        return result[0]

    def cancel_order(self, order_id: int) -> bool:
        assert(isinstance(order_id, int))

        self.logger.info(f"Cancelling order #{order_id}...")

        data = {
            "orderId": str(order_id),
            "protocol": '0x',
            "signature": eth_sign(bytes(str(order_id), 'utf-8'), self.tethfinex.web3)
        }

        result = self._http_post("/trustless/v1/w/oc", data)
        success = result[0] == order_id

        if success:
            self.logger.info(f"Cancelled order #{order_id}")
        else:
            self.logger.info(f"Failed to cancel order #{order_id}")

        return success

    def get_trades(self, pair: str, page_number: int = 1) -> List[Trade]:
        assert(isinstance(pair, str))
        assert(isinstance(page_number, int))
        assert(page_number == 1)

        result = self._get_orders("/trustless/v1/r/orders/hist")

        executed_orders = filter(lambda order: 'EXECUTED' in order['status'], result)

        return list(map(lambda trade: Trade.to_trade(trade), executed_orders))

    def get_all_trades(self, pair: str, page_number: int = 1) -> List[Trade]:
        assert(isinstance(pair, str))
        assert(isinstance(page_number, int))
        assert(page_number == 1)

        result = self._http_get(f"/v1/trades/{pair}", {})

        return list(map(lambda item: Trade(trade_id=int(item['tid']),
                                           timestamp=int(item['timestamp']),
                                           is_sell=True if item['type'] == "sell" else False,
                                           pair=pair,
                                           price=Wad.from_number(item['price']),
                                           amount=Wad.from_number(item['amount'])), result))

    def _get_orders(self, endpoint: str):
        assert(isinstance(endpoint, str))

        nonce = str(time.time() + 3)

        body = {
            'protocol': '0x',
            'nonce': nonce,
            'signature': eth_sign(bytes(nonce, 'utf-8'), self.tethfinex.web3)
        }

        return self._http_post(f"{endpoint}", body)

    def _http_get(self, resource: str, body: dict):
        assert(isinstance(resource, str))
        assert(isinstance(body, dict))

        if not body:
            return self._result(requests.get(url=f"{self.api_server}{resource}",
                                             timeout=self.timeout))

        data = json.dumps(body, separators=(',', ':'))
        return self._result(requests.get(url=f"{self.api_server}{resource}",
                                         json=data,
                                         timeout=self.timeout))

    def _http_post(self, resource: str, body: dict):
        assert(isinstance(resource, str))

        if not body:
            return self._result(requests.post(url=f"{self.api_server}{resource}",
                                              timeout=self.timeout))

        return self._result(requests.post(url=f"{self.api_server}{resource}",
                                          json=body,
                                          timeout=self.timeout))

    @staticmethod
    def _result(result) -> dict:
        if not result.ok:
            raise Exception(f"Ethfinex API invalid HTTP response: {http_response_summary(result)}")

        try:
            data = result.json()
        except Exception:
            raise Exception(f"Ethfinex API invalid JSON response: {http_response_summary(result)}")

        return data
