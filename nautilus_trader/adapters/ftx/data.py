# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2022 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

import asyncio
from typing import Any, Dict, Optional

import orjson
import pandas as pd

from nautilus_trader.adapters.ftx.common import FTX_VENUE
from nautilus_trader.adapters.ftx.http.client import FTXHttpClient
from nautilus_trader.adapters.ftx.http.error import FTXError
from nautilus_trader.adapters.ftx.providers import FTXInstrumentProvider
from nautilus_trader.adapters.ftx.websocket.client import FTXWebSocketClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.clock import LiveClock
from nautilus_trader.common.logging import Logger
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data.bar import BarType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.msgbus.bus import MessageBus


class FTXDataClient(LiveMarketDataClient):
    """
    Provides a data client for the FTX exchange.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client : FTXHttpClient
        The FTX HTTP client.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    logger : Logger
        The logger for the client.
    instrument_provider : FTXInstrumentProvider
        The instrument provider.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client: FTXHttpClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        logger: Logger,
        instrument_provider: FTXInstrumentProvider,
    ):
        super().__init__(
            loop=loop,
            client_id=ClientId(FTX_VENUE.value),
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            logger=logger,
        )

        self._http_client = client
        self._ws_client = FTXWebSocketClient(
            loop=loop,
            clock=clock,
            logger=logger,
            handler=self._handle_ws_message,
            key=client.api_key,
            secret=client.api_secret,
        )

    def connect(self):
        """
        Connect the client to FTX.
        """
        self._log.info("Connecting...")
        self._loop.create_task(self._connect())

    def disconnect(self):
        """
        Disconnect the client from FTX.
        """
        self._log.info("Disconnecting...")
        self._loop.create_task(self._disconnect())

    async def _connect(self):
        if not self._http_client.connected:
            await self._http_client.connect()
        try:
            await self._instrument_provider.load_all_or_wait_async()
        except FTXError as ex:
            self._log.exception(ex)
            return

        self._send_all_instruments_to_data_engine()

        await self._ws_client.connect(start=True)
        await self._ws_client.subscribe_markets()

        self._set_connected(True)
        self._log.info("Connected.")

    async def _disconnect(self):
        if self._ws_client.is_connected:
            await self._ws_client.disconnect()
            await self._ws_client.close()
        if self._http_client.connected:
            await self._http_client.disconnect()

        self._set_connected(False)
        self._log.info("Disconnected.")

    # -- SUBSCRIPTIONS -----------------------------------------------------------------------------

    def subscribe_instruments(self):
        """
        Subscribe to instrument data for the venue.

        """
        for instrument_id in list(self._instrument_provider.get_all().keys()):
            self._add_subscription_instrument(instrument_id)

    def subscribe_instrument(self, instrument_id: InstrumentId):
        """
        Subscribe to instrument data for the given instrument ID.

        Parameters
        ----------
        instrument_id : InstrumentId
            The instrument ID to subscribe to.

        """
        self._add_subscription_instrument(instrument_id)

    def subscribe_order_book_deltas(
        self,
        instrument_id: InstrumentId,
        book_type: BookType,
        depth: Optional[int] = None,
        kwargs: dict = None,
    ):
        self._loop.create_task(self._ws_client.subscribe_orderbook(instrument_id.symbol.value))
        self._add_subscription_order_book_deltas(instrument_id)

    def subscribe_order_book_snapshots(
        self,
        instrument_id: InstrumentId,
        book_type: BookType,
        depth: Optional[int] = None,
        kwargs: dict = None,
    ):
        self._loop.create_task(self._ws_client.subscribe_orderbook(instrument_id.symbol.value))
        self._add_subscription_order_book_snapshots(instrument_id)

    def subscribe_ticker(self, instrument_id: InstrumentId):
        self._loop.create_task(self._ws_client.subscribe_ticker(instrument_id.symbol.value))
        self._add_subscription_ticker(instrument_id)

    def subscribe_quote_ticks(self, instrument_id: InstrumentId):
        self._loop.create_task(self._ws_client.subscribe_ticker(instrument_id.symbol.value))
        self._add_subscription_quote_ticks(instrument_id)

    def subscribe_trade_ticks(self, instrument_id: InstrumentId):
        self._loop.create_task(self._ws_client.subscribe_trades(instrument_id.symbol.value))
        self._add_subscription_trade_ticks(instrument_id)

    def subscribe_bars(self, bar_type: BarType):
        self._log.error(
            f"Cannot subscribe to bars {bar_type} " f"(not supported by exchange).",
        )

    def subscribe_instrument_status_updates(self, instrument_id: InstrumentId):
        self._log.error(
            f"Cannot subscribe to instrument status updates for {instrument_id} "
            f"(not supported by exchange).",
        )

    def subscribe_instrument_close_prices(self, instrument_id: InstrumentId):
        self._log.error(
            f"Cannot subscribe to instrument close prices for {instrument_id} "
            f"(not supported by exchange).",
        )

    def unsubscribe_instruments(self):
        for instrument_id in list(self._instrument_provider.get_all().keys()):
            self._remove_subscription_instrument(instrument_id)

    def unsubscribe_instrument(self, instrument_id: InstrumentId):
        self._remove_subscription_instrument(instrument_id)

    def unsubscribe_order_book_deltas(self, instrument_id: InstrumentId):
        self._remove_subscription_order_book_deltas(instrument_id)
        if instrument_id not in self.subscribed_order_book_snapshots():
            # Only unsubscribe if there are also no subscriptions for the markets order book snapshots
            self._loop.create_task(
                self._ws_client.unsubscribe_orderbook(instrument_id.symbol.value)
            )

    def unsubscribe_order_book_snapshots(self, instrument_id: InstrumentId):
        self._remove_subscription_order_book_snapshots(instrument_id)
        if instrument_id not in self.subscribed_order_book_deltas():
            # Only unsubscribe if there are also no subscriptions for the markets order book deltas
            self._loop.create_task(
                self._ws_client.unsubscribe_orderbook(instrument_id.symbol.value)
            )

    def unsubscribe_ticker(self, instrument_id: InstrumentId):
        self._remove_subscription_ticker(instrument_id)
        if instrument_id not in self.subscribed_quote_ticks():
            # Only unsubscribe if there are also no subscriptions for the markets quote ticks
            self._loop.create_task(self._ws_client.unsubscribe_ticker(instrument_id.symbol.value))

    def unsubscribe_quote_ticks(self, instrument_id: InstrumentId):
        self._remove_subscription_quote_ticks(instrument_id)
        if instrument_id not in self.subscribed_tickers():
            # Only unsubscribe if there are also no subscriptions for the markets ticker
            self._loop.create_task(self._ws_client.unsubscribe_ticker(instrument_id.symbol.value))

    def unsubscribe_trade_ticks(self, instrument_id: InstrumentId):
        self._remove_subscription_trade_ticks(instrument_id)
        self._loop.create_task(self._ws_client.unsubscribe_trades(instrument_id.symbol.value))

    def unsubscribe_bars(self, bar_type: BarType):
        self._log.error(f"Cannot unsubscribe from bars {bar_type} (not supported by exchange).")

    def unsubscribe_instrument_status_updates(self, instrument_id: InstrumentId):
        self._log.error(
            "Cannot unsubscribe from instrument status updates " "(not supported by exchange).",
        )

    def unsubscribe_instrument_close_prices(self, instrument_id: InstrumentId):
        self._log.error(
            "Cannot unsubscribe from instrument close prices " "(not supported by exchange).",
        )

    # -- REQUESTS ----------------------------------------------------------------------------------

    def request_quote_ticks(
        self,
        instrument_id: InstrumentId,
        from_datetime: pd.Timestamp,
        to_datetime: pd.Timestamp,
        limit: int,
        correlation_id: UUID4,
    ):
        pass
        # TODO(Implement

    def request_trade_ticks(
        self,
        instrument_id: InstrumentId,
        from_datetime: pd.Timestamp,
        to_datetime: pd.Timestamp,
        limit: int,
        correlation_id: UUID4,
    ):
        pass
        # TODO(Implement

    def request_bars(
        self,
        bar_type: BarType,
        from_datetime: pd.Timestamp,
        to_datetime: pd.Timestamp,
        limit: int,
        correlation_id: UUID4,
    ):
        pass
        # TODO(Implement

    async def _subscribed_instruments_update(self, delay):
        await self._instrument_provider.load_all_async()

        self._send_all_instruments_to_data_engine()

        update = self.run_after_delay(delay, self._subscribed_instruments_update(delay))
        self._update_instruments_task = self._loop.create_task(update)

    def _send_all_instruments_to_data_engine(self):
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)

    def _handle_ws_message(self, raw: bytes):
        msg: Dict[str, Any] = orjson.loads(raw)
        channel: str = msg.get("channel")

        if channel:
            print(msg)  # TODO!
