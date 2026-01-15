from datetime import datetime
from typing import Dict
import json
import logging
import re
from trading_config import TradingConfig


def safe_float(value, default: float = 0.0) -> float:
    """安全地将值转换为 float，处理包含 $、逗号等格式的字符串"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r'[$¥€£,\s]', '', value.strip())
        if cleaned.endswith('%'):
            cleaned = cleaned[:-1]
            try:
                return float(cleaned) / 100
            except ValueError:
                return default
        try:
            return float(cleaned) if cleaned else default
        except ValueError:
            return default
    return default

logger = logging.getLogger(__name__)

class TradingEngine:
    def __init__(self, model_id: int, db, market_fetcher, ai_trader, trade_fee_rate: float = None):
        self.model_id = model_id
        self.db = db
        self.market_fetcher = market_fetcher
        self.ai_trader = ai_trader
        
        # 从配置读取参数
        self.coins = TradingConfig.TRADING_COINS
        self.trade_fee_rate = trade_fee_rate or TradingConfig.TRADE_FEE_RATE
        self.max_slippage = TradingConfig.MAX_SLIPPAGE
        self.cash_buffer_ratio = TradingConfig.CASH_BUFFER_RATIO
        
        # 冷却期机制
        self.last_trade_time = {}  # {coin: timestamp}
        self.cooldown_period = TradingConfig.COOLDOWN_PERIOD_SECONDS
    
    def execute_trading_cycle(self) -> Dict:
        try:
            market_state = self._get_market_state()
            
            current_prices = {coin: market_state[coin]['price'] for coin in market_state}
            
            portfolio = self.db.get_portfolio(self.model_id, current_prices)
            
            # 在周期开始时记录账户价值快照（确保有完整记录）
            self.db.record_account_value(
                self.model_id,
                portfolio['total_value'],
                portfolio['cash'],
                portfolio['positions_value']
            )
            
            account_info = self._build_account_info(portfolio)
            
            decisions = self.ai_trader.make_decision(
                market_state, portfolio, account_info
            )
            
            # 提取CoT trace（如果有的话）
            cot_trace = ''
            if decisions:
                # 从第一个决策中提取CoT（如果存在）
                first_decision = next(iter(decisions.values()), {})
                cot_trace = first_decision.get('cot_trace', '')
            
            self.db.add_conversation(
                self.model_id,
                user_prompt=self._format_prompt(market_state, portfolio, account_info),
                ai_response=json.dumps(decisions, ensure_ascii=False),
                cot_trace=cot_trace
            )
            
            execution_results = self._execute_decisions(decisions, market_state, portfolio)
            
            # 检查是否需要部分止盈
            self._check_scale_out_opportunities(market_state, portfolio)
            
            # 检查持仓时间管理（移动止损、强制平仓等）
            self._check_position_time_management(market_state, portfolio)
            
            # 在周期结束后再次记录（如果有交易发生）
            updated_portfolio = self.db.get_portfolio(self.model_id, current_prices)
            if execution_results and any(r.get('signal') != 'hold' for r in execution_results):
                self.db.record_account_value(
                    self.model_id,
                    updated_portfolio['total_value'],
                    updated_portfolio['cash'],
                    updated_portfolio['positions_value']
                )
            
            return {
                'success': True,
                'decisions': decisions,
                'executions': execution_results,
                'portfolio': updated_portfolio
            }
            
        except Exception as e:
            print(f"[ERROR] Trading cycle failed (Model {self.model_id}): {e}")
            import traceback
            print(traceback.format_exc())
            return {
                'success': False,
                'error': str(e)
            }
    
    def _get_market_state(self) -> Dict:
        market_state = {}
        prices = self.market_fetcher.get_current_prices(self.coins)
        
        for coin in self.coins:
            if coin in prices:
                market_state[coin] = prices[coin].copy()
                indicators = self.market_fetcher.calculate_technical_indicators(coin)
                market_state[coin]['indicators'] = indicators
                if indicators:
                    for field in ['volatility_7d', 'sentiment_score', 'news_signal', 'average_volume_7d']:
                        if indicators.get(field) is not None:
                            market_state[coin][field] = indicators.get(field)
        
        return market_state
    
    def _build_account_info(self, portfolio: Dict) -> Dict:
        model = self.db.get_model(self.model_id)
        initial_capital = model['initial_capital']
        total_value = portfolio['total_value']
        total_return = ((total_value - initial_capital) / initial_capital) * 100
        
        return {
            'current_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_return': total_return,
            'initial_capital': initial_capital
        }
    
    def _format_prompt(self, market_state: Dict, portfolio: Dict, 
                      account_info: Dict) -> str:
        return f"Market State: {len(market_state)} coins, Portfolio: {len(portfolio['positions'])} positions"
    
    def _execute_decisions(self, decisions: Dict, market_state: Dict, 
                          portfolio: Dict) -> list:
        results = []
        current_time = datetime.now().timestamp()
        
        for coin, decision in decisions.items():
            if coin not in self.coins:
                continue
            
            signal = decision.get('signal', '').lower()
            
            # 检查冷却期（仅对开仓信号检查）
            if signal in ['buy_to_enter', 'sell_to_enter']:
                last_trade = self.last_trade_time.get(coin, 0)
                time_since_last_trade = current_time - last_trade
                
                if time_since_last_trade < self.cooldown_period:
                    remaining = int(self.cooldown_period - time_since_last_trade)
                    logger.info(f"[COOLDOWN] {coin} in cooldown period, {remaining}s remaining")
                    results.append({
                        'coin': coin,
                        'signal': signal,
                        'message': f'Skipped: cooldown period ({remaining}s remaining)'
                    })
                    continue
            
            try:
                if signal == 'buy_to_enter':
                    result = self._execute_buy(coin, decision, market_state, portfolio)
                    if 'error' not in result:
                        self.last_trade_time[coin] = current_time
                elif signal == 'sell_to_enter':
                    result = self._execute_sell(coin, decision, market_state, portfolio)
                    if 'error' not in result:
                        self.last_trade_time[coin] = current_time
                elif signal == 'close_position':
                    result = self._execute_close(coin, decision, market_state, portfolio)
                elif signal == 'hold':
                    result = {'coin': coin, 'signal': 'hold', 'message': 'Hold position'}
                else:
                    result = {'coin': coin, 'error': f'Unknown signal: {signal}'}
                
                results.append(result)
                
            except Exception as e:
                results.append({'coin': coin, 'error': str(e)})
        
        return results
    
    def _check_scale_out_opportunities(self, market_state: Dict, portfolio: Dict):
        """检查并执行部分止盈机会"""
        for pos in portfolio['positions']:
            coin = pos['coin']
            if coin not in market_state:
                continue
            
            current_price = market_state[coin]['price']
            entry_price = pos['avg_price']
            side = pos['side']
            
            # 获取止盈目标（使用配置的默认止盈百分比）
            profit_pct = TradingConfig.DEFAULT_PROFIT_TARGET_PCT
            profit_target = entry_price * (1 + profit_pct) if side == 'long' else entry_price * (1 - profit_pct)
            
            should_scale, scale_pct = self.ai_trader.risk_manager.should_scale_out(
                entry_price, current_price, profit_target, side
            )
            
            if should_scale and scale_pct > 0:
                # 部分平仓
                close_quantity = pos['quantity'] * scale_pct
                
                if side == 'long':
                    pnl = (current_price - entry_price) * close_quantity
                else:
                    pnl = (entry_price - current_price) * close_quantity
                
                trade_fee = close_quantity * current_price * self.trade_fee_rate
                net_pnl = pnl - trade_fee
                
                # 更新持仓（减少数量）
                new_quantity = pos['quantity'] - close_quantity
                if new_quantity > 0.0001:  # 保留剩余持仓
                    self.db.update_position(
                        self.model_id, coin, -close_quantity, current_price,
                        pos['leverage'], side
                    )
                else:  # 完全平仓
                    self.db.close_position(self.model_id, coin, side)
                
                # 记录部分平仓交易
                self.db.add_trade(
                    self.model_id, coin, 'partial_close', close_quantity,
                    current_price, pos['leverage'], side, pnl=net_pnl, fee=trade_fee
                )
                
                logger.info(f"[{coin}] Partial close {scale_pct*100:.0f}%: "
                          f"qty={close_quantity:.4f}, price=${current_price:.2f}, "
                          f"net_pnl=${net_pnl:.2f}")
    
    def _check_position_time_management(self, market_state: Dict, portfolio: Dict):
        """
        持仓时间管理：
        1. 长时间持仓后启用移动止损
        2. 超过最大持仓时间强制平仓
        3. 横盘超时收紧止损或平仓
        """
        if not TradingConfig.POSITION_TIME_ENABLED:
            return
        
        current_time = datetime.now()
        
        for pos in portfolio['positions']:
            coin = pos['coin']
            if coin not in market_state:
                continue
            
            current_price = market_state[coin]['price']
            entry_price = pos['avg_price']
            side = pos['side']
            quantity = pos['quantity']
            leverage = pos.get('leverage', 1)
            
            # 获取持仓时间（从数据库updated_at字段）
            updated_at = pos.get('updated_at')
            if not updated_at:
                continue
            
            try:
                if isinstance(updated_at, str):
                    position_time = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                else:
                    position_time = updated_at
                
                holding_hours = (current_time - position_time.replace(tzinfo=None)).total_seconds() / 3600
            except Exception as e:
                logger.warning(f"[{coin}] Cannot parse position time: {e}")
                continue
            
            # 计算当前盈亏百分比
            if side == 'long':
                pnl_pct = (current_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - current_price) / entry_price
            
            action_taken = False
            
            # 1. 检查是否超过最大持仓时间
            if holding_hours > TradingConfig.MAX_HOLDING_HOURS:
                logger.warning(f"[{coin}] Position held for {holding_hours:.1f}h > {TradingConfig.MAX_HOLDING_HOURS}h, force closing")
                self._force_close_position(coin, pos, current_price, "max_holding_time")
                action_taken = True
                continue
            
            # 2. 检查横盘超时
            if abs(pnl_pct) < TradingConfig.SIDEWAYS_THRESHOLD_PCT and holding_hours > TradingConfig.SIDEWAYS_HOURS:
                logger.info(f"[{coin}] Sideways for {holding_hours:.1f}h (pnl={pnl_pct:.2%})")
                
                if TradingConfig.SIDEWAYS_ACTION == 'close':
                    logger.warning(f"[{coin}] Closing due to sideways timeout")
                    self._force_close_position(coin, pos, current_price, "sideways_timeout")
                    action_taken = True
                elif TradingConfig.SIDEWAYS_ACTION == 'tighten_stop':
                    # 收紧止损（这里只记录日志，实际止损由交易所管理）
                    new_stop_distance = TradingConfig.TRAILING_DISTANCE_PCT * 0.5  # 止损距离减半
                    logger.info(f"[{coin}] Tightening stop loss due to sideways, new distance: {new_stop_distance:.2%}")
            
            # 3. 长时间持仓后启用移动止损（只对盈利仓位）
            if not action_taken and holding_hours > TradingConfig.TRAILING_TRIGGER_HOURS and pnl_pct > 0:
                # 计算移动止损价
                trailing_distance = TradingConfig.TRAILING_DISTANCE_PCT
                if side == 'long':
                    trailing_stop = current_price * (1 - trailing_distance)
                    if trailing_stop > entry_price:
                        logger.info(f"[{coin}] Trailing stop activated: {trailing_stop:.4f} (entry: {entry_price:.4f})")
                else:
                    trailing_stop = current_price * (1 + trailing_distance)
                    if trailing_stop < entry_price:
                        logger.info(f"[{coin}] Trailing stop activated: {trailing_stop:.4f} (entry: {entry_price:.4f})")
    
    def _force_close_position(self, coin: str, pos: Dict, current_price: float, reason: str):
        """强制平仓"""
        try:
            entry_price = pos['avg_price']
            quantity = pos['quantity']
            side = pos['side']
            leverage = pos.get('leverage', 1)
            
            if side == 'long':
                pnl = (current_price - entry_price) * quantity * leverage
            else:
                pnl = (entry_price - current_price) * quantity * leverage
            
            trade_fee = quantity * current_price * self.trade_fee_rate
            net_pnl = pnl - trade_fee
            
            # 平仓
            self.db.close_position(self.model_id, coin, side)
            
            # 更新现金
            position_value = quantity * current_price
            self.db.update_cash(self.model_id, position_value + net_pnl)
            
            # 记录交易
            self.db.add_trade(
                self.model_id, coin, 'close_position', quantity,
                current_price, leverage, side, pnl=net_pnl, fee=trade_fee
            )
            
            logger.info(f"[{coin}] Force closed ({reason}): qty={quantity:.4f}, price=${current_price:.2f}, net_pnl=${net_pnl:.2f}")
            
        except Exception as e:
            logger.error(f"[{coin}] Force close failed: {e}")
    
    def _check_slippage(self, coin: str, expected_price: float) -> tuple[bool, float]:
        """检查滑点是否在可接受范围内"""
        try:
            current_prices = self.market_fetcher.get_current_prices([coin])
            if coin not in current_prices:
                return False, 0.0
            
            current_price = current_prices[coin]['price']
            slippage = abs(current_price - expected_price) / expected_price
            
            if slippage > self.max_slippage:
                logger.warning(f"[{coin}] Slippage {slippage:.2%} exceeds limit {self.max_slippage:.2%}")
                return False, current_price
            
            return True, current_price
        except Exception as e:
            logger.error(f"[{coin}] Slippage check failed: {e}")
            return False, expected_price
    
    def _execute_buy(self, coin: str, decision: Dict, market_state: Dict,
                    portfolio: Dict) -> Dict:
        quantity = safe_float(decision.get('quantity'), 0)
        leverage = int(safe_float(decision.get('leverage'), 1))
        expected_price = market_state[coin]['price']
        
        if quantity <= 0:
            return {'coin': coin, 'error': 'Invalid quantity'}
        
        # 滑点保护
        slippage_ok, actual_price = self._check_slippage(coin, expected_price)
        if not slippage_ok:
            return {'coin': coin, 'error': f'Slippage too high, expected ${expected_price:.2f}, got ${actual_price:.2f}'}
        
        price = actual_price
        
        # 计算交易额和交易费
        trade_amount = quantity * price
        trade_fee = trade_amount * self.trade_fee_rate
        required_margin = (quantity * price) / leverage
        
        # 总需资金 = 保证金 + 交易费
        total_required = required_margin + trade_fee
        
        # 资金检查（使用配置的缓冲比例）
        if total_required > portfolio['cash'] * self.cash_buffer_ratio:
            return {'coin': coin, 'error': 'Insufficient cash (including fees)'}
        
        # 更新持仓
        self.db.update_position(
            self.model_id, coin, quantity, price, leverage, 'long'
        )
        
        # 记录交易（包含交易费）
        self.db.add_trade(
            self.model_id, coin, 'buy_to_enter', quantity, 
            price, leverage, 'long', pnl=0, fee=trade_fee  # 新增fee参数
        )
        
        return {
            'coin': coin,
            'signal': 'buy_to_enter',
            'quantity': quantity,
            'price': price,
            'leverage': leverage,
            'fee': trade_fee,  # 返回费用信息
            'message': f'Long {quantity:.4f} {coin} @ ${price:.2f} (Fee: ${trade_fee:.2f})'
        }
    
    def _execute_sell(self, coin: str, decision: Dict, market_state: Dict,
                 portfolio: Dict) -> Dict:
        quantity = safe_float(decision.get('quantity'), 0)
        leverage = int(safe_float(decision.get('leverage'), 1))
        expected_price = market_state[coin]['price']
        
        if quantity <= 0:
            return {'coin': coin, 'error': 'Invalid quantity'}
        
        # 滑点保护
        slippage_ok, actual_price = self._check_slippage(coin, expected_price)
        if not slippage_ok:
            return {'coin': coin, 'error': f'Slippage too high, expected ${expected_price:.2f}, got ${actual_price:.2f}'}
        
        price = actual_price
        
        # 计算交易额和交易费
        trade_amount = quantity * price
        trade_fee = trade_amount * self.trade_fee_rate
        required_margin = (quantity * price) / leverage
        
        # 总需资金 = 保证金 + 交易费
        total_required = required_margin + trade_fee
        
        # 资金检查（使用配置的缓冲比例）
        if total_required > portfolio['cash'] * self.cash_buffer_ratio:
            return {'coin': coin, 'error': 'Insufficient cash (including fees)'}
        
        # 更新持仓
        self.db.update_position(
            self.model_id, coin, quantity, price, leverage, 'short'
        )
        
        # 记录交易（包含交易费）
        self.db.add_trade(
            self.model_id, coin, 'sell_to_enter', quantity, 
            price, leverage, 'short', pnl=0, fee=trade_fee  # 新增fee参数
        )
        
        return {
            'coin': coin,
            'signal': 'sell_to_enter',
            'quantity': quantity,
            'price': price,
            'leverage': leverage,
            'fee': trade_fee,
            'message': f'Short {quantity:.4f} {coin} @ ${price:.2f} (Fee: ${trade_fee:.2f})'
        }
    
    def _execute_close(self, coin: str, decision: Dict, market_state: Dict,
                    portfolio: Dict) -> Dict:
        position = None
        for pos in portfolio['positions']:
            if pos['coin'] == coin:
                position = pos
                break
        
        if not position:
            return {'coin': coin, 'error': 'Position not found'}
        
        expected_price = market_state[coin]['price']
        
        # 滑点保护
        slippage_ok, actual_price = self._check_slippage(coin, expected_price)
        if not slippage_ok:
            logger.warning(f"[{coin}] Closing with slippage, expected ${expected_price:.2f}, got ${actual_price:.2f}")
        
        current_price = actual_price
        entry_price = position['avg_price']
        quantity = position['quantity']
        side = position['side']
        
        # 计算平仓利润（未扣费）
        if side == 'long':
            gross_pnl = (current_price - entry_price) * quantity
        else:  # short
            gross_pnl = (entry_price - current_price) * quantity
        
        # 计算平仓交易费（按平仓时的交易额）
        trade_amount = quantity * current_price
        trade_fee = trade_amount * self.trade_fee_rate
        net_pnl = gross_pnl - trade_fee  # 净利润 = 毛利润 - 交易费
        
        # 关闭持仓
        self.db.close_position(self.model_id, coin, side)
        
        # 记录平仓交易（包含费用和净利润）
        self.db.add_trade(
            self.model_id, coin, 'close_position', quantity,
            current_price, position['leverage'], side, pnl=net_pnl, fee=trade_fee  # 新增fee参数
        )
        
        return {
            'coin': coin,
            'signal': 'close_position',
            'quantity': quantity,
            'price': current_price,
            'pnl': net_pnl,
            'fee': trade_fee,
            'message': f'Close {coin}, Gross P&L: ${gross_pnl:.2f}, Fee: ${trade_fee:.2f}, Net P&L: ${net_pnl:.2f}'
        }
