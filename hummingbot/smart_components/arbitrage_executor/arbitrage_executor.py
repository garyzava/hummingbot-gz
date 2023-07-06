import asyncio
import logging
from decimal import Decimal
from functools import lru_cache
from typing import Union

from hummingbot.client.settings import AllConnectorSettings
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.core.event.events import BuyOrderCreatedEvent, MarketOrderFailureEvent, SellOrderCreatedEvent
from hummingbot.core.rate_oracle.rate_oracle import RateOracle
from hummingbot.logger import HummingbotLogger
from hummingbot.smart_components.arbitrage_executor.data_types import ArbitrageConfig, ArbitrageExecutorStatus
from hummingbot.smart_components.position_executor.data_types import TrackedOrder
from hummingbot.smart_components.smart_component_base import SmartComponentBase
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class ArbitrageExecutor(SmartComponentBase):
    _logger = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    @staticmethod
    @lru_cache(maxsize=10)
    def is_amm(exchange: str) -> bool:
        return exchange in sorted(
            AllConnectorSettings.get_gateway_amm_connector_names()
        )

    def __init__(self, strategy: ScriptStrategyBase, arbitrage_config: ArbitrageConfig, update_interval: float = 0.5):
        connectors = [arbitrage_config.buying_market.exchange, arbitrage_config.selling_market.exchange]
        self.buying_market = arbitrage_config.buying_market
        self.selling_market = arbitrage_config.selling_market
        self.min_profitability = arbitrage_config.min_profitability
        self.order_amount = arbitrage_config.order_amount
        self.max_retries = arbitrage_config.max_retries
        self.arbitrage_status = ArbitrageExecutorStatus.NOT_STARTED

        # Order tracking
        self._buy_order: TrackedOrder = TrackedOrder()
        self._sell_order: TrackedOrder = TrackedOrder()

        self._last_buy_price = None
        self._last_sell_price = None
        self._last_tx_cost = None
        self._cumulative_failures = 0
        super().__init__(strategy, list(connectors), update_interval)

    # def generate_all_opportunities(self):
    #     opportunities = []
    #     for pair1, pair2 in itertools.combinations(self.arbitrage_config.markets, 2):
    #         if self.validate_pair(pair1, pair2):
    #             opportunity = ArbitrageOpportunity(buying_market=pair1.exchange,
    #                                                selling_market=pair2.exchange)
    #             opportunities.append(opportunity)
    #     return opportunities
    #
    # @staticmethod
    # def validate_pair(pair1, pair2):
    #     base_asset1, quote_asset1 = pair1.trading_pair.split('/')
    #     base_asset2, quote_asset2 = pair2.trading_pair.split('/')
    #     return base_asset1 == base_asset2 and quote_asset1 == quote_asset2

    @property
    def net_pnl(self) -> Decimal:
        if self.arbitrage_status == ArbitrageExecutorStatus.COMPLETED:
            sell_quote_amount = self.sell_order.order.executed_amount * self.sell_order.order.average_price
            buy_quote_amount = self.buy_order.order.executed_amount * self.buy_order.order.average_price
            cum_fees = self.buy_order.order.cum_fee_quote + self.sell_order.order.cum_fee_quote
            return sell_quote_amount - buy_quote_amount - cum_fees
        else:
            return Decimal("0")

    @property
    def net_pnl_pct(self) -> Decimal:
        if self.arbitrage_status == ArbitrageExecutorStatus.COMPLETED:
            return self.net_pnl / self.buy_order.order.executed_amount
        else:
            return Decimal("0")

    @property
    def buy_order(self) -> TrackedOrder:
        return self._buy_order

    @buy_order.setter
    def buy_order(self, value: TrackedOrder):
        self._buy_order = value

    @property
    def sell_order(self) -> TrackedOrder:
        return self._sell_order

    @sell_order.setter
    def sell_order(self, value: TrackedOrder):
        self._sell_order = value

    async def get_resulting_price_for_amount(self, exchange: str, trading_pair: str, is_buy: bool, order_amount: Decimal):
        return await self.connectors[exchange].get_quote_price(trading_pair, is_buy, order_amount)

    async def control_task(self):
        if self.arbitrage_status == ArbitrageExecutorStatus.NOT_STARTED:
            try:
                trade_pnl_pct = await self.get_trade_pnl_pct()
                fee_pct = await self.get_tx_cost_pct()
                profitability = trade_pnl_pct - fee_pct
                if profitability > self.min_profitability:
                    await self.execute_arbitrage()
            except Exception as e:
                self.logger().error("Error calculating profitability", e)
        elif self.arbitrage_status == ArbitrageExecutorStatus.ACTIVE_ARBITRAGE:
            if self._cumulative_failures > self.max_retries:
                self.arbitrage_status = ArbitrageExecutorStatus.FAILED
                self.terminate_control_loop()
            else:
                self.check_order_status()

    def check_order_status(self):
        if self.buy_order.order.is_filled and self.sell_order.order.is_filled:
            self.arbitrage_status = ArbitrageExecutorStatus.COMPLETED
            self.terminate_control_loop()

    async def execute_arbitrage(self):
        self.arbitrage_status = ArbitrageExecutorStatus.ACTIVE_ARBITRAGE
        self.place_buy_arbitrage_order()
        self.place_sell_arbitrage_order()

    def place_buy_arbitrage_order(self):
        self.buy_order = self.place_order(
            connector_name=self.buying_market.exchange,
            trading_pair=self.buying_market.trading_pair,
            order_type=OrderType.MARKET,
            side=TradeType.BUY,
            amount=self.order_amount,
            price=self._last_buy_price,
        )

    def place_sell_arbitrage_order(self):
        self.sell_order = self.place_order(
            connector_name=self.selling_market.exchange,
            trading_pair=self.selling_market.trading_pair,
            order_type=OrderType.MARKET,
            side=TradeType.SELL,
            amount=self.order_amount,
            price=self._last_sell_price,
        )

    async def get_tx_cost_pct(self) -> Decimal:
        base, quote = split_hb_trading_pair(trading_pair=self.buying_market.trading_pair)
        buy_fee = await self.get_tx_cost_in_asset(
            exchange=self.buying_market.exchange,
            trading_pair=self.buying_market.trading_pair,
            is_buy=True,
            order_amount=self.order_amount,
            asset=base
        )
        sell_fee = await self.get_tx_cost_in_asset(
            exchange=self.selling_market.exchange,
            trading_pair=self.selling_market.trading_pair,
            is_buy=False,
            order_amount=self.order_amount,
            asset=base)
        self._last_tx_cost = buy_fee + sell_fee
        return self._last_tx_cost / self.order_amount

    async def get_buy_and_sell_prices(self):
        buy_price_task = asyncio.create_task(self.get_resulting_price_for_amount(
            exchange=self.buying_market.exchange,
            trading_pair=self.buying_market.trading_pair,
            is_buy=True,
            order_amount=self.order_amount))
        sell_price_task = asyncio.create_task(self.get_resulting_price_for_amount(
            exchange=self.selling_market.exchange,
            trading_pair=self.selling_market.trading_pair,
            is_buy=False,
            order_amount=self.order_amount))

        buy_price, sell_price = await asyncio.gather(buy_price_task, sell_price_task)
        return buy_price, sell_price

    async def get_trade_pnl_pct(self):
        self._last_buy_price, self._last_sell_price = await self.get_buy_and_sell_prices()
        return (self._last_sell_price - self._last_buy_price) / self._last_buy_price

    async def get_tx_cost_in_asset(self, exchange: str, trading_pair: str, is_buy: bool, order_amount: Decimal, asset: str):
        connector = self.connectors[exchange]
        price = await self.get_resulting_price_for_amount(exchange, trading_pair, is_buy, order_amount)
        if self.is_amm(exchange=exchange):
            gas_cost = connector.network_transaction_fee
            conversion_price = RateOracle.get_instance().get_pair_rate(f"{asset}-{gas_cost.token}")
            return gas_cost.amount / conversion_price
        else:
            fee = connector.get_fee(
                base_currency=asset,
                quote_currency=asset,
                order_type=OrderType.MARKET,
                order_side=TradeType.BUY if is_buy else TradeType.SELL,
                amount=order_amount,
                price=price,
                is_maker=False
            )
            return fee.fee_amount_in_token(
                trading_pair=trading_pair,
                price=price,
                order_amount=order_amount,
                token=asset,
                exchange=connector,
            )

    def process_order_created_event(self, _, market, event: Union[BuyOrderCreatedEvent, SellOrderCreatedEvent]):
        if self.buy_order.order_id == event.order_id:
            self.buy_order.order = self.get_in_flight_order(self.buying_market.exchange, event.order_id)
            self.logger().info("Open Order Created")
        elif self.sell_order.order_id == event.order_id:
            self.logger().info("Close Order Created")
            self.sell_order.order = self.get_in_flight_order(self.selling_market.exchange, event.order_id)

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        if self.buy_order.order_id == event.order_id:
            self.place_buy_arbitrage_order()
            self._cumulative_failures += 1
        elif self.sell_order.order_id == event.order_id:
            self.place_sell_arbitrage_order()
            self._cumulative_failures += 1

    def to_format_status(self):
        lines = []
        trade_pnl_pct = (self._last_sell_price - self._last_buy_price) / self._last_buy_price
        tx_cost_pct = self._last_tx_cost / self.order_amount
        base, quote = split_hb_trading_pair(trading_pair=self.buying_market.trading_pair)
        lines.extend(f"""
Buy:
    Exchange: {self.buying_market.exchange} | Trading Pair: {self.buying_market.trading_pair} | Price: {self._last_buy_price}
Sell
    Exchange: {self.selling_market.exchange} | Trading Pair: {self.selling_market.trading_pair} | Price: {self._last_sell_price}
Order Amount: {self.order_amount}
Real-time Profit analysis:
    Trade PnL (%): {trade_pnl_pct} | TX Cost (%): {tx_cost_pct}
    Net PnL (%): {trade_pnl_pct - tx_cost_pct}
Arbitrage Status: {self.arbitrage_status}""")
        if self.arbitrage_status == ArbitrageExecutorStatus.COMPLETED:
            lines.extend(f"""
Total Profit (%): {self.net_pnl_pct} | Total Profit ({quote}): {self.net_pnl}
""")
        return "\n".join(lines)
