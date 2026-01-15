"""
真实交易引擎
Real Trading Engine

对接 OKX 交易所进行真实交易
"""
import time
import re
from datetime import datetime
from typing import Dict, List
import json
import logging
from trading_config import TradingConfig
from okx_exchange import OKXExchange, get_okx_exchange


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


class RealTradingEngine:
    """
    真实交易引擎
    
    与模拟交易引擎 TradingEngine 接口兼容，可无缝切换
    """
    
    def __init__(self, model_id: int, db, market_fetcher, ai_trader):
        """
        初始化真实交易引擎
        
        Args:
            model_id: 模型 ID
            db: 数据库实例
            market_fetcher: 市场数据获取器
            ai_trader: AI 交易员
        """
        self.model_id = model_id
        self.db = db
        self.market_fetcher = market_fetcher
        self.ai_trader = ai_trader
        self.coins = TradingConfig.TRADING_COINS
        
        # OKX 交易所适配器
        self.exchange = get_okx_exchange()
        
        # 冷却期机制
        self.last_trade_time = {}
        self.cooldown_period = TradingConfig.COOLDOWN_PERIOD_SECONDS
        
        # 连接状态
        self._connection_ok = False
        self._last_connection_check = 0
        self._connection_check_interval = 300  # 5分钟检查一次连接
        
        # 验证连接（非阻塞，失败不影响启动）
        try:
            self._connection_ok = self.exchange.test_connection()
            if not self._connection_ok:
                logger.warning("OKX 交易所连接失败，将在交易时重试。请检查网络或 API 配置")
        except Exception as e:
            logger.warning(f"OKX 连接测试异常: {e}，将在交易时重试")
        
        logger.info(f"[RealTradingEngine] 真实交易引擎初始化完成 (Model {model_id}, 连接状态: {'正常' if self._connection_ok else '待重试'})")
    
    def execute_trading_cycle(self) -> Dict:
        """
        执行交易周期
        
        Returns:
            交易结果
        """
        try:
            # 0. 定期检查连接状态
            current_time = time.time()
            if current_time - self._last_connection_check > self._connection_check_interval:
                self._last_connection_check = current_time
                try:
                    self._connection_ok = self.exchange.test_connection(try_backup=True)
                    if self._connection_ok:
                        logger.info("[RealTradingEngine] OKX 连接正常")
                except Exception as e:
                    logger.warning(f"[RealTradingEngine] 连接检查失败: {e}")
                    self._connection_ok = False
            
            # 1. 获取市场状态（结合本地数据和 OKX 实时数据）
            market_state = self._get_market_state()
            
            # 2. 获取账户和持仓信息
            portfolio = self._get_portfolio(market_state)
            
            # 记录账户价值
            self.db.record_account_value(
                self.model_id,
                portfolio['total_value'],
                portfolio['cash'],
                portfolio['positions_value']
            )
            
            # ★ 2.5 实时止盈检查（在AI决策之前执行）
            take_profit_results = self._check_and_take_profit(portfolio, market_state)
            if take_profit_results:
                # 止盈后重新获取持仓
                portfolio = self._get_portfolio(market_state)
            
            # 3. 构建账户信息
            account_info = self._build_account_info(portfolio)
            
            # 检查模型是否已被删除
            if account_info is None:
                return {
                    'success': False,
                    'error': f'模型 {self.model_id} 已被删除'
                }
            
            # 4. AI 决策
            decisions = self.ai_trader.make_decision(
                market_state, portfolio, account_info
            )
            
            # 5. 记录对话
            self.db.add_conversation(
                self.model_id,
                user_prompt=f"Market State: {len(market_state)} coins, Portfolio: {len(portfolio['positions'])} positions",
                ai_response=json.dumps(decisions, ensure_ascii=False)
            )
            
            # 6. 执行交易
            execution_results = self._execute_decisions(decisions, market_state, portfolio)
            
            # 7. 更新账户价值
            updated_portfolio = self._get_portfolio(market_state)
            if execution_results and any(r.get('success') for r in execution_results):
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
            logger.error(f"[RealTradingEngine] 交易周期失败 (Model {self.model_id}): {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {
                'success': False,
                'error': str(e)
            }
    
    def _get_market_state(self) -> Dict:
        """获取市场状态"""
        market_state = {}
        
        # 从 OKX 获取实时行情
        okx_tickers = self.exchange.get_tickers(self.coins)
        
        # 从本地获取技术指标
        local_prices = self.market_fetcher.get_current_prices(self.coins)
        
        for coin in self.coins:
            if coin in okx_tickers:
                okx_data = okx_tickers[coin]
                local_data = local_prices.get(coin, {})
                
                # 合并数据
                market_state[coin] = {
                    'price': okx_data['price'],
                    'change_24h': ((okx_data['price'] - okx_data.get('change_24h', okx_data['price'])) / okx_data.get('change_24h', okx_data['price']) * 100) if okx_data.get('change_24h') else local_data.get('change_24h', 0),
                    'volume_24h': okx_data['volume_24h'],
                    'high_24h': okx_data['high_24h'],
                    'low_24h': okx_data['low_24h'],
                    'bid': okx_data['bid'],
                    'ask': okx_data['ask'],
                }
                
                # 添加本地技术指标
                indicators = self.market_fetcher.calculate_technical_indicators(coin)
                market_state[coin]['indicators'] = indicators
                if indicators:
                    for field in ['volatility_7d', 'sentiment_score', 'news_signal']:
                        if indicators.get(field) is not None:
                            market_state[coin][field] = indicators.get(field)
            
            elif coin in local_prices:
                # OKX 没有数据时使用本地数据
                market_state[coin] = local_prices[coin].copy()
                indicators = self.market_fetcher.calculate_technical_indicators(coin)
                market_state[coin]['indicators'] = indicators
        
        return market_state
    
    def _get_portfolio(self, market_state: Dict) -> Dict:
        """获取投资组合"""
        # 获取 OKX 账户余额
        balance = self.exchange.get_account_balance()
        
        # 获取 OKX 持仓
        okx_positions = self.exchange.get_positions()
        
        # 转换为标准格式
        positions = []
        positions_value = 0
        
        for pos in okx_positions:
            coin = pos['coin']
            current_price = market_state.get(coin, {}).get('price', pos['avg_price'])
            
            position_data = {
                'coin': coin,
                'side': pos['side'],
                'quantity': pos['quantity'],
                'contract_size': pos.get('contract_size', 0),
                'ct_val': pos.get('ct_val', 1),
                'avg_price': pos['avg_price'],
                'leverage': pos['leverage'],
                'current_price': current_price,
                'unrealized_pnl': pos['unrealized_pnl'],
                'unrealized_pnl_ratio': pos.get('unrealized_pnl_ratio', 0),
                'margin': pos.get('margin', 0),  # OKX 返回的实际保证金
                'notional_usd': pos.get('notional_usd', 0),  # OKX 返回的名义价值
                'liq_price': pos.get('liq_price'),
            }
            positions.append(position_data)
            
            # 使用 OKX 返回的保证金，如果没有则计算
            margin = pos.get('margin', 0)
            if margin <= 0:
                notional = pos.get('notional_usd', 0) or (pos['quantity'] * current_price)
                margin = notional / pos['leverage'] if pos['leverage'] > 0 else notional
            positions_value += margin
        
        # 计算总价值
        total_equity = balance.get('total_equity', 0) if balance.get('success') else 0
        available_balance = balance.get('available_balance', 0) if balance.get('success') else 0
        
        # 获取 OKX 返回的实际冻结保证金（全仓模式下最准确）
        frozen_margin = balance.get('frozen_margin', 0) if balance.get('success') else 0
        
        # 如果没有真实余额数据，使用数据库记录
        if total_equity == 0:
            model = self.db.get_model(self.model_id)
            if model:
                total_equity = model.get('initial_capital', 10000)
            else:
                total_equity = 10000  # 默认值
            available_balance = total_equity - positions_value
        
        return {
            'model_id': self.model_id,
            'total_value': total_equity,
            'cash': available_balance,
            'positions_value': positions_value,
            'positions': positions,
            'realized_pnl': 0,  # OKX 不直接提供
            'unrealized_pnl': sum(pos['unrealized_pnl'] for pos in positions),
            'frozen_margin': frozen_margin,  # OKX 账户级别的冻结保证金
        }
    
    def _build_account_info(self, portfolio: Dict) -> Dict:
        """构建账户信息"""
        model = self.db.get_model(self.model_id)
        
        # 检查模型是否存在
        if model is None:
            logger.warning(f"[RealTradingEngine] 模型 {self.model_id} 不存在或已删除")
            return None
        
        initial_capital = model.get('initial_capital', 10000)
        total_value = portfolio['total_value']
        total_return = ((total_value - initial_capital) / initial_capital) * 100 if initial_capital > 0 else 0
        
        return {
            'current_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_return': total_return,
            'initial_capital': initial_capital,
            'is_real_trading': True  # 标记为真实交易
        }
    
    def _check_and_take_profit(self, portfolio: Dict, market_state: Dict) -> List[Dict]:
        """
        实时止盈检查
        
        在每个交易周期开始时检查所有持仓，如果盈利达到阈值则自动平仓
        
        Returns:
            止盈执行结果列表
        """
        if not getattr(TradingConfig, 'ENABLE_AUTO_TAKE_PROFIT', True):
            return []
        
        results = []
        positions = portfolio.get('positions', [])
        
        if not positions:
            return []
        
        # 获取止盈规则
        take_profit_rules = getattr(TradingConfig, 'AUTO_TAKE_PROFIT_RULES', [
            (0.15, 1.0, "盈利15%全平"),
            (0.10, 0.50, "盈利10%平半仓"),
            (0.07, 0.30, "盈利7%平30%"),
        ])
        
        # 快速止盈阈值
        quick_profit_enabled = getattr(TradingConfig, 'ENABLE_QUICK_PROFIT_EXIT', True)
        quick_profit_threshold = getattr(TradingConfig, 'QUICK_PROFIT_THRESHOLD_PCT', 0.08)
        
        for pos in positions:
            coin = pos['coin']
            side = pos['side']
            quantity = pos['quantity']
            avg_price = pos['avg_price']
            current_price = pos.get('current_price', market_state.get(coin, {}).get('price', avg_price))
            
            # 计算盈利百分比
            if side == 'long':
                profit_pct = (current_price - avg_price) / avg_price
            else:  # short
                profit_pct = (avg_price - current_price) / avg_price
            
            # 跳过亏损仓位
            if profit_pct <= 0:
                continue
            
            logger.info(f"[PROFIT-CHECK] {coin} {side}: 盈利 {profit_pct*100:.2f}%")
            
            # 检查止盈规则（从高到低检查）
            close_pct = 0
            close_reason = ""
            
            # 快速止盈检查
            if quick_profit_enabled and profit_pct >= quick_profit_threshold:
                close_pct = 1.0
                close_reason = f"快速盈利{profit_pct*100:.1f}%，立即全平"
            else:
                # 阶梯止盈检查
                for threshold_pct, pct_to_close, desc in take_profit_rules:
                    if profit_pct >= threshold_pct:
                        close_pct = pct_to_close
                        close_reason = desc
                        break
            
            if close_pct > 0:
                logger.info(f"[TAKE-PROFIT] {coin}: {close_reason} (平仓{close_pct*100:.0f}%)")
                
                # 计算平仓数量
                close_quantity = int(quantity * close_pct)
                if close_quantity < 1:
                    close_quantity = 1  # 至少平1张
                
                # 执行平仓
                if close_pct >= 1.0:
                    # 全平
                    result = self.exchange.close_position(coin, side)
                else:
                    # 部分平仓 - 使用 close_long 或 close_short
                    close_order_side = 'close_long' if side == 'long' else 'close_short'
                    result = self.exchange.place_order(
                        coin=coin,
                        side=close_order_side,
                        quantity=close_quantity
                    )
                
                if result.get('success'):
                    # 记录交易
                    realized_pnl = profit_pct * close_quantity * avg_price
                    self._record_trade(
                        coin, 'close_position', current_price, close_quantity,
                        pos.get('leverage', 1), realized_pnl, f"自动止盈: {close_reason}"
                    )
                    logger.info(f"[TAKE-PROFIT] {coin}: 止盈成功! 盈利 ${realized_pnl:.2f}")
                    results.append({
                        'coin': coin,
                        'success': True,
                        'action': 'take_profit',
                        'profit_pct': profit_pct,
                        'realized_pnl': realized_pnl,
                        'reason': close_reason
                    })
                else:
                    logger.error(f"[TAKE-PROFIT] {coin}: 止盈失败 - {result.get('error')}")
                    results.append({
                        'coin': coin,
                        'success': False,
                        'error': result.get('error')
                    })
        
        return results
    
    def _execute_decisions(self, decisions: Dict, market_state: Dict, 
                          portfolio: Dict) -> List[Dict]:
        """执行交易决策"""
        results = []
        current_time = datetime.now().timestamp()
        
        for coin, decision in decisions.items():
            if coin not in self.coins:
                continue
            
            signal = decision.get('signal', '').lower()
            
            # 检查冷却期
            if signal in ['buy_to_enter', 'sell_to_enter']:
                last_trade = self.last_trade_time.get(coin, 0)
                time_since_last_trade = current_time - last_trade
                
                if time_since_last_trade < self.cooldown_period:
                    remaining = int(self.cooldown_period - time_since_last_trade)
                    logger.info(f"[COOLDOWN] {coin} 冷却中, 剩余 {remaining}s")
                    results.append({
                        'coin': coin,
                        'signal': signal,
                        'success': False,
                        'message': f'冷却中 ({remaining}s 后可交易)'
                    })
                    continue
            
            try:
                if signal == 'buy_to_enter':
                    result = self._execute_open_long(coin, decision, market_state)
                    if result.get('success'):
                        self.last_trade_time[coin] = current_time
                elif signal == 'sell_to_enter':
                    result = self._execute_open_short(coin, decision, market_state)
                    if result.get('success'):
                        self.last_trade_time[coin] = current_time
                elif signal == 'close_position':
                    result = self._execute_close(coin, decision, portfolio)
                elif signal == 'hold':
                    result = {'coin': coin, 'signal': 'hold', 'success': True, 'message': '持有'}
                else:
                    result = {'coin': coin, 'success': False, 'error': f'未知信号: {signal}'}
                
                results.append(result)
                
                # 记录交易到数据库
                if result.get('success') and signal != 'hold':
                    self._record_trade(coin, decision, result, market_state)
                
            except Exception as e:
                logger.error(f"[{coin}] 交易执行失败: {e}")
                results.append({'coin': coin, 'success': False, 'error': str(e)})
        
        return results
    
    def _execute_open_long(self, coin: str, decision: Dict, market_state: Dict) -> Dict:
        """执行开多"""
        price = safe_float(market_state[coin].get('price'), 0)
        quantity = safe_float(decision.get('quantity'), 0)
        leverage = int(safe_float(decision.get('leverage'), TradingConfig.DEFAULT_LEVERAGE))
        
        # 获取可用余额
        balance = self.exchange.get_account_balance()
        if not balance.get('success', False):
            error_msg = f"获取余额失败: {balance.get('error', '未知错误')}"
            logger.error(f"[{coin}] {error_msg}")
            return {'coin': coin, 'signal': 'buy_to_enter', 'success': False, 'error': error_msg, 'message': f"开多失败: {error_msg}"}
        
        available = float(balance.get('available_balance', 0) or 0)
        
        # 获取合约信息
        instrument = self.exchange.get_instrument(coin)
        min_sz = instrument['min_sz'] if instrument else 0.01
        lot_sz = instrument['lot_sz'] if instrument else 0.01
        
        # 计算单张合约价值和保证金
        contract_value = self.exchange.get_contract_value(coin, price)
        margin_per_contract = contract_value / leverage
        
        # 根据余额计算最大可开张数（保留10%缓冲，支持小数）
        usable_balance = available * 0.9
        max_contracts = usable_balance / margin_per_contract if margin_per_contract > 0 else 0
        # 按 lot_sz 精度向下取整
        max_contracts = int(max_contracts / lot_sz) * lot_sz if lot_sz > 0 else max_contracts
        
        # AI请求的张数
        usdt_amount = quantity * price
        requested_contracts = self.exchange.calculate_contract_size(coin, usdt_amount, price)
        
        # 取较小值：不超过余额允许的张数
        contracts = min(requested_contracts, max_contracts)
        
        logger.info(f"[{coin}] 开多: 可用${available:.2f}, 单张保证金${margin_per_contract:.2f}, 最大{max_contracts:.2f}张, 请求{requested_contracts:.2f}张, 实际{contracts:.2f}张")
        
        if contracts < min_sz:
            min_margin = contract_value * min_sz / leverage
            error_msg = f'余额不足: 可用${available:.2f}, 开{min_sz}张需${min_margin:.2f}'
            logger.warning(f"[{coin}] {error_msg}")
            return {
                'coin': coin, 
                'signal': 'buy_to_enter',
                'success': False, 
                'error': error_msg,
                'message': f"开多失败: {error_msg}"
            }
        
        # 检查最小下单金额（使用配置）
        trade_value = contracts * contract_value
        min_trade_usd = TradingConfig.MIN_TRADE_VALUE_USD  # 配置为20美元
        if trade_value < min_trade_usd:
            # 调整到最小金额
            min_contracts = min_trade_usd / contract_value
            min_contracts = max(min_sz, int(min_contracts / lot_sz + 1) * lot_sz)  # 向上取整到lot_sz
            if min_contracts * contract_value / leverage > available * 0.9:
                error_msg = f'最小下单${min_trade_usd}, 需{min_contracts:.2f}张, 保证金${min_contracts*contract_value/leverage:.2f}, 余额不足'
                logger.warning(f"[{coin}] {error_msg}")
                return {
                    'coin': coin, 
                    'signal': 'buy_to_enter',
                    'success': False, 
                    'error': error_msg,
                    'message': f"开多失败: {error_msg}"
                }
            contracts = min_contracts
            logger.info(f"[{coin}] 调整到最小下单: {contracts:.2f}张 (价值${contracts*contract_value:.2f})")
        
        # 下单（不带止损止盈，后续单独设置）
        result = self.exchange.place_order(
            coin=coin,
            side='buy_to_enter',
            quantity=contracts,
            leverage=leverage
        )
        
        result['coin'] = coin
        result['signal'] = 'buy_to_enter'
        result['contracts'] = contracts
        result['price'] = price
        result['leverage'] = leverage
        
        if result['success']:
            result['message'] = f"开多 {contracts}张 @ ${price:.2f}, {leverage}x"
            logger.info(f"[{coin}] 开多成功: {contracts}张 @ ${price:.2f}, {leverage}x")
            
            # 设置止损止盈（策略订单）
            stop_loss = decision.get('stop_loss')
            take_profit = decision.get('profit_target')
            if stop_loss or take_profit:
                self.exchange.set_stop_loss_take_profit(
                    coin, 'long', stop_loss, take_profit
                )
        else:
            result['message'] = f"开多失败: {result.get('error', '未知错误')}"
        
        return result
    
    def _execute_open_short(self, coin: str, decision: Dict, market_state: Dict) -> Dict:
        """执行开空"""
        price = safe_float(market_state[coin].get('price'), 0)
        quantity = safe_float(decision.get('quantity'), 0)
        leverage = int(safe_float(decision.get('leverage'), TradingConfig.DEFAULT_LEVERAGE))
        
        # 获取可用余额
        balance = self.exchange.get_account_balance()
        if not balance.get('success', False):
            error_msg = f"获取余额失败: {balance.get('error', '未知错误')}"
            logger.error(f"[{coin}] {error_msg}")
            return {'coin': coin, 'signal': 'sell_to_enter', 'success': False, 'error': error_msg, 'message': f"开空失败: {error_msg}"}
        
        available = float(balance.get('available_balance', 0) or 0)
        
        # 获取合约信息
        instrument = self.exchange.get_instrument(coin)
        min_sz = instrument['min_sz'] if instrument else 0.01
        lot_sz = instrument['lot_sz'] if instrument else 0.01
        
        # 计算单张合约价值和保证金
        contract_value = self.exchange.get_contract_value(coin, price)
        margin_per_contract = contract_value / leverage
        
        # 根据余额计算最大可开张数（保留10%缓冲，支持小数）
        usable_balance = available * 0.9
        max_contracts = usable_balance / margin_per_contract if margin_per_contract > 0 else 0
        # 按 lot_sz 精度向下取整
        max_contracts = int(max_contracts / lot_sz) * lot_sz if lot_sz > 0 else max_contracts
        
        # AI请求的张数
        usdt_amount = quantity * price
        requested_contracts = self.exchange.calculate_contract_size(coin, usdt_amount, price)
        
        # 取较小值：不超过余额允许的张数
        contracts = min(requested_contracts, max_contracts)
        
        logger.info(f"[{coin}] 开空: 可用${available:.2f}, 单张保证金${margin_per_contract:.2f}, 最大{max_contracts:.2f}张, 请求{requested_contracts:.2f}张, 实际{contracts:.2f}张")
        
        if contracts < min_sz:
            min_margin = contract_value * min_sz / leverage
            error_msg = f'余额不足: 可用${available:.2f}, 开{min_sz}张需${min_margin:.2f}'
            logger.warning(f"[{coin}] {error_msg}")
            return {
                'coin': coin, 
                'signal': 'sell_to_enter',
                'success': False, 
                'error': error_msg,
                'message': f"开空失败: {error_msg}"
            }
        
        # 检查最小下单金额（使用配置）
        trade_value = contracts * contract_value
        min_trade_usd = TradingConfig.MIN_TRADE_VALUE_USD  # 配置为20美元
        if trade_value < min_trade_usd:
            # 调整到最小金额
            min_contracts = min_trade_usd / contract_value
            min_contracts = max(min_sz, int(min_contracts / lot_sz + 1) * lot_sz)  # 向上取整到lot_sz
            if min_contracts * contract_value / leverage > available * 0.9:
                error_msg = f'最小下单${min_trade_usd}, 需{min_contracts:.2f}张, 保证金${min_contracts*contract_value/leverage:.2f}, 余额不足'
                logger.warning(f"[{coin}] {error_msg}")
                return {
                    'coin': coin, 
                    'signal': 'sell_to_enter',
                    'success': False, 
                    'error': error_msg,
                    'message': f"开空失败: {error_msg}"
                }
            contracts = min_contracts
            logger.info(f"[{coin}] 调整到最小下单: {contracts:.2f}张 (价值${contracts*contract_value:.2f})")
        
        # 下单（不带止损止盈，后续单独设置）
        result = self.exchange.place_order(
            coin=coin,
            side='sell_to_enter',
            quantity=contracts,
            leverage=leverage
        )
        
        result['coin'] = coin
        result['signal'] = 'sell_to_enter'
        result['contracts'] = contracts
        result['price'] = price
        result['leverage'] = leverage
        
        if result['success']:
            result['message'] = f"开空 {contracts}张 @ ${price:.2f}, {leverage}x"
            logger.info(f"[{coin}] 开空成功: {contracts}张 @ ${price:.2f}, {leverage}x")
            
            # 设置止损止盈（策略订单）
            stop_loss = decision.get('stop_loss')
            take_profit = decision.get('profit_target')
            if stop_loss or take_profit:
                self.exchange.set_stop_loss_take_profit(
                    coin, 'short', stop_loss, take_profit
                )
        else:
            result['message'] = f"开空失败: {result.get('error', '未知错误')}"
        
        return result
    
    def _execute_close(self, coin: str, decision: Dict, portfolio: Dict) -> Dict:
        """执行平仓"""
        # 找到持仓
        position = None
        for pos in portfolio['positions']:
            if pos['coin'] == coin:
                position = pos
                break
        
        if not position:
            return {'coin': coin, 'success': False, 'error': '未找到持仓'}
        
        # 平仓
        result = self.exchange.close_position(coin, position['side'])
        
        result['coin'] = coin
        result['signal'] = 'close_position'
        result['quantity'] = position['quantity']
        result['pnl'] = position['pnl']
        
        if result['success']:
            result['message'] = f"平仓 {position['side']} {position['quantity']}张, 盈亏 ${position['pnl']:.2f}"
            logger.info(f"[{coin}] 平仓成功: {position['side']}, 盈亏 ${position['pnl']:.2f}")
        else:
            result['message'] = f"平仓失败: {result.get('error', '未知错误')}"
        
        return result
    
    def _record_trade(self, coin: str, decision: Dict, result: Dict, market_state: Dict):
        """记录交易到数据库"""
        signal = decision.get('signal', '').lower()
        price = market_state[coin]['price']
        quantity = result.get('contracts', decision.get('quantity', 0))
        leverage = result.get('leverage', decision.get('leverage', 1))
        side = 'long' if signal == 'buy_to_enter' else ('short' if signal == 'sell_to_enter' else 'long')
        pnl = result.get('pnl', 0)
        fee = quantity * price * TradingConfig.TRADE_FEE_RATE
        
        self.db.add_trade(
            self.model_id, coin, signal, quantity,
            price, leverage, side, pnl=pnl, fee=fee
        )
    
    def close_all_positions(self) -> List[Dict]:
        """一键平仓所有持仓"""
        return self.exchange.close_all_positions()


def create_trading_engine(model_id: int, db, market_fetcher, ai_trader):
    """
    创建交易引擎（根据配置选择真实或模拟）
    
    Args:
        model_id: 模型 ID
        db: 数据库实例
        market_fetcher: 市场数据获取器
        ai_trader: AI 交易员
        
    Returns:
        交易引擎实例
    """
    if TradingConfig.ENABLE_REAL_TRADING:
        logger.info(f"[Model {model_id}] 使用真实交易引擎 (OKX)")
        return RealTradingEngine(model_id, db, market_fetcher, ai_trader)
    else:
        from trading_engine import TradingEngine
        logger.info(f"[Model {model_id}] 使用模拟交易引擎")
        return TradingEngine(model_id, db, market_fetcher, ai_trader)

