import json
import re
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Union
import requests
from openai import OpenAI, APIConnectionError, APIError
import time
import logging
from circuit_breaker import circuit_manager
from risk_manager import DynamicRiskManager
from trading_config import TradingConfig

# Prompt 日志目录
PROMPT_LOG_DIR = Path(__file__).parent / 'logs' / 'prompts'
PROMPT_LOG_DIR.mkdir(parents=True, exist_ok=True)


def safe_float(value: Union[str, float, int, None], default: float = 0.0) -> float:
    """
    安全地将值转换为 float，处理包含 $、逗号等格式的字符串
    
    Args:
        value: 要转换的值，可能是 "$2.08", "2,000.50", 2.08, None 等
        default: 转换失败时的默认值
        
    Returns:
        转换后的浮点数
    """
    if value is None:
        return default
    
    if isinstance(value, (int, float)):
        return float(value)
    
    if isinstance(value, str):
        # 移除常见的格式字符：$, ¥, €, £, 逗号, 空格
        cleaned = re.sub(r'[$¥€£,\s]', '', value.strip())
        
        # 处理百分号
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class AITrader:
    # 类级别的交易开始时间（所有实例共享）
    _trading_start_time = None
    
    def __init__(self, provider_type: str, api_key: str, api_url: str, model_name: str, db=None, market_fetcher=None):
        self.provider_type = provider_type.lower()
        self.api_key = api_key
        self.api_url = api_url
        self.model_name = model_name
        self.logger = logging.getLogger(__name__)
        self.db = db  # 用于获取历史数据
        self.market_fetcher = market_fetcher  # 用于获取市场情绪数据
        
        # 记录交易开始时间（仅第一次初始化时设置）
        if AITrader._trading_start_time is None:
            AITrader._trading_start_time = datetime.now()
        
        # 从配置读取API参数
        self.max_retries = TradingConfig.API_MAX_RETRIES
        self.retry_delay = TradingConfig.API_RETRY_DELAY
        
        # 初始化熔断器（使用配置参数）
        self.circuit_breaker = circuit_manager.get_breaker(
            name=f"AI_{provider_type}_{model_name}",
            failure_threshold=TradingConfig.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            timeout=TradingConfig.CIRCUIT_BREAKER_TIMEOUT
        )
        
        # 初始化风险管理器（使用配置参数）
        self.risk_manager = DynamicRiskManager(
            base_risk_per_trade=TradingConfig.BASE_RISK_PER_TRADE,
            max_risk_per_trade=TradingConfig.MAX_RISK_PER_TRADE
        )
    
    def get_circuit_breaker_status(self) -> Dict:
        """获取熔断器状态"""
        return self.circuit_breaker.get_state()
    
    def reset_circuit_breaker(self):
        """手动重置熔断器"""
        self.circuit_breaker.reset()
        self.logger.info(f"熔断器已手动重置: {self.circuit_breaker.name}")
    
    def _save_prompt_log(self, system_prompt: str, user_prompt: str, response: str = None):
        """
        保存 Prompt 日志到本地文件用于分析
        
        将 prompt 按行分割保存，提高可读性
        
        Args:
            system_prompt: System Prompt 内容
            user_prompt: User Prompt 内容
            response: LLM 响应内容（可选）
        """
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            date_str = datetime.now().strftime('%Y-%m-%d')
            
            # 按日期创建子目录
            day_dir = PROMPT_LOG_DIR / date_str
            day_dir.mkdir(parents=True, exist_ok=True)
            
            # 文件名: {时间戳}_{provider}_{model}.json
            model_safe = self.model_name.replace('/', '_').replace(':', '_')
            filename = f"{timestamp}_{self.provider_type}_{model_safe}.json"
            filepath = day_dir / filename
            
            # 解析 response 中的 JSON（如果有）
            parsed_response = None
            if response:
                try:
                    resp_text = response
                    if '```json' in resp_text:
                        resp_text = resp_text.split('```json')[1].split('```')[0]
                    elif '```' in resp_text:
                        resp_text = resp_text.split('```')[1].split('```')[0]
                    
                    json_start = resp_text.find('{')
                    json_end = resp_text.rfind('}')
                    if json_start != -1 and json_end != -1:
                        resp_text = resp_text[json_start:json_end+1]
                        parsed_response = json.loads(resp_text)
                except:
                    parsed_response = None
            
            # 构建日志数据 - 将 prompt 按行分割为数组
            log_data = {
                "timestamp": datetime.now().isoformat(),
                "provider": self.provider_type,
                "model": self.model_name,
                "prompt_lengths": {
                    "system_chars": len(system_prompt) if system_prompt else 0,
                    "user_chars": len(user_prompt) if user_prompt else 0,
                    "response_chars": len(response) if response else 0
                },
                "system_prompt": system_prompt.split('\n') if system_prompt else [],
                "user_prompt": user_prompt.split('\n') if user_prompt else [],
                "response": parsed_response if parsed_response else response
            }
            
            # 写入 JSON 文件
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
            
            self.logger.debug(f"Prompt 日志已保存: {filepath}")
            
        except Exception as e:
            self.logger.warning(f"保存 Prompt 日志失败: {e}")
    
    def _get_performance_summary(self, portfolio: Dict) -> str:
        """获取历史表现摘要（用于LLM学习）"""
        if not self.db or 'model_id' not in portfolio:
            return "暂无历史数据"
        
        try:
            model_id = portfolio['model_id']
            trades = self.db.get_trades(model_id, limit=20)
            
            if not trades:
                return "这是你的首次交易决策，请谨慎分析市场。"
            
            # 计算近期表现（确保pnl是float）
            closed_trades = [t for t in trades if t.get('signal') == 'close_position']
            if closed_trades:
                # 确保所有pnl都是float
                for t in closed_trades:
                    t['pnl'] = float(t.get('pnl', 0) or 0)
                
                winning = sum(1 for t in closed_trades if t['pnl'] > 0)
                total_pnl = sum(t['pnl'] for t in closed_trades)
                win_rate = (winning / len(closed_trades)) * 100
                
                # 分析多空表现
                long_trades = [t for t in closed_trades if t.get('side') == 'long']
                short_trades = [t for t in closed_trades if t.get('side') == 'short']
                
                long_pnl = sum(t['pnl'] for t in long_trades) if long_trades else 0
                short_pnl = sum(t['pnl'] for t in short_trades) if short_trades else 0
                
                summary = f"""
近期交易表现：
- 总交易笔数: {len(closed_trades)}笔
- 胜率: {win_rate:.1f}% ({winning}胜/{len(closed_trades)-winning}负)
- 总盈亏: ${total_pnl:.2f}
- 做多表现: {len(long_trades)}笔, 盈亏${long_pnl:.2f}
- 做空表现: {len(short_trades)}笔, 盈亏${short_pnl:.2f}

经验总结:
"""
                # 添加最成功和最失败的交易
                if closed_trades:
                    best_trade = max(closed_trades, key=lambda x: float(x.get('pnl', 0) or 0))
                    worst_trade = min(closed_trades, key=lambda x: float(x.get('pnl', 0) or 0))
                    
                    best_pnl = float(best_trade.get('pnl', 0) or 0)
                    worst_pnl = float(worst_trade.get('pnl', 0) or 0)
                    summary += f"• 最佳交易: {best_trade.get('coin', '?')} {best_trade.get('side', '?')}, 盈利${best_pnl:.2f}\n"
                    summary += f"• 最差交易: {worst_trade.get('coin', '?')} {worst_trade.get('side', '?')}, 亏损${worst_pnl:.2f}\n"
                
                # 策略建议
                if win_rate < 40:
                    summary += "•胜率偏低，建议提高入场标准，减少交易频率\n"
                elif win_rate > 60:
                    summary += "•胜率良好，保持当前策略\n"
                
                if long_pnl < 0 and short_pnl < 0:
                    summary += "•多空都亏损，可能市场环境不适合，考虑观望\n"
                elif long_pnl > short_pnl * 2:
                    summary += "•做多表现更好，可适当倾向做多机会\n"
                elif short_pnl > long_pnl * 2:
                    summary += "•做空表现更好，可适当倾向做空机会\n"
                
                return summary
            else:
                return "有开仓记录但还没有平仓，暂无完整交易数据。"
                
        except Exception as e:
            self.logger.warning(f"Failed to get performance summary: {e}")
            return "无法获取历史数据"
    
    def _generate_trading_insights(self, portfolio: Dict) -> str:
        """
        生成交易学习总结 - 基于历史交易自动优化策略
        
        分析内容:
        1. 各币种表现排名
        2. 多空策略效果
        3. 杠杆使用效果
        4. 交易时间模式
        5. 止盈止损执行效果
        """
        if not TradingConfig.LEARNING_ENABLED:
            return ""
        
        if not self.db or 'model_id' not in portfolio:
            return ""
        
        try:
            model_id = portfolio['model_id']
            trades = self.db.get_trades(model_id, limit=TradingConfig.LEARNING_HISTORY_LIMIT)
            
            closed_trades = [t for t in trades if t.get('signal') == 'close_position']
            
            if len(closed_trades) < TradingConfig.LEARNING_MIN_TRADES:
                return ""
            
            # 确保数据类型正确
            for t in closed_trades:
                t['pnl'] = float(t.get('pnl', 0) or 0)
                t['price'] = float(t.get('price', 0) or 0)
                t['quantity'] = float(t.get('quantity', 0) or 0)
                t['leverage'] = int(t.get('leverage', 1) or 1)
            
            insights = []
            insights.append("\n# 🧠 交易学习总结（基于历史数据自动优化）\n")
            
            # 1. 总体统计
            total_trades = len(closed_trades)
            winning = sum(1 for t in closed_trades if t['pnl'] > 0)
            losing = total_trades - winning
            total_pnl = sum(t['pnl'] for t in closed_trades)
            win_rate = (winning / total_trades) * 100 if total_trades > 0 else 0
            
            avg_win = sum(t['pnl'] for t in closed_trades if t['pnl'] > 0) / winning if winning > 0 else 0
            avg_loss = sum(t['pnl'] for t in closed_trades if t['pnl'] < 0) / losing if losing > 0 else 0
            
            insights.append(f"**总体表现:** {total_trades}笔交易, 胜率{win_rate:.1f}%, 总盈亏${total_pnl:.2f}")
            if avg_win > 0 and avg_loss < 0:
                profit_factor = abs(avg_win * winning / (avg_loss * losing)) if losing > 0 else float('inf')
                insights.append(f"**盈亏比:** 平均盈利${avg_win:.2f} vs 平均亏损${abs(avg_loss):.2f}, 盈亏因子={profit_factor:.2f}")
            
            # 2. 各币种表现分析
            coin_performance = {}
            for t in closed_trades:
                coin = t.get('coin', 'UNKNOWN')
                if coin not in coin_performance:
                    coin_performance[coin] = {'trades': 0, 'wins': 0, 'pnl': 0}
                coin_performance[coin]['trades'] += 1
                coin_performance[coin]['pnl'] += t['pnl']
                if t['pnl'] > 0:
                    coin_performance[coin]['wins'] += 1
            
            # 排序找出最佳和最差币种
            sorted_coins = sorted(coin_performance.items(), key=lambda x: x[1]['pnl'], reverse=True)
            if sorted_coins:
                best_coin = sorted_coins[0]
                worst_coin = sorted_coins[-1]
                insights.append(f"\n**币种表现:**")
                insights.append(f"- 🟢 最佳: {best_coin[0]} (盈亏${best_coin[1]['pnl']:.2f}, {best_coin[1]['trades']}笔)")
                if worst_coin[1]['pnl'] < 0:
                    insights.append(f"- 🔴 最差: {worst_coin[0]} (盈亏${worst_coin[1]['pnl']:.2f}, {worst_coin[1]['trades']}笔)")
                    insights.append(f"- 💡 建议: 减少对{worst_coin[0]}的交易, 优先关注{best_coin[0]}")
            
            # 3. 多空表现对比
            long_trades = [t for t in closed_trades if t.get('side') == 'long']
            short_trades = [t for t in closed_trades if t.get('side') == 'short']
            
            if long_trades or short_trades:
                insights.append(f"\n**多空策略:**")
                if long_trades:
                    long_pnl = sum(t['pnl'] for t in long_trades)
                    long_wins = sum(1 for t in long_trades if t['pnl'] > 0)
                    long_rate = (long_wins / len(long_trades)) * 100
                    insights.append(f"- 做多: {len(long_trades)}笔, 胜率{long_rate:.1f}%, 盈亏${long_pnl:.2f}")
                if short_trades:
                    short_pnl = sum(t['pnl'] for t in short_trades)
                    short_wins = sum(1 for t in short_trades if t['pnl'] > 0)
                    short_rate = (short_wins / len(short_trades)) * 100
                    insights.append(f"- 做空: {len(short_trades)}笔, 胜率{short_rate:.1f}%, 盈亏${short_pnl:.2f}")
                
                # 策略建议
                if long_trades and short_trades:
                    long_pnl = sum(t['pnl'] for t in long_trades)
                    short_pnl = sum(t['pnl'] for t in short_trades)
                    if long_pnl > short_pnl * 1.5:
                        insights.append(f"- 💡 做多表现更好，优先寻找做多机会")
                    elif short_pnl > long_pnl * 1.5:
                        insights.append(f"- 💡 做空表现更好，优先寻找做空机会")
            
            # 4. 杠杆使用效果
            leverage_performance = {}
            for t in closed_trades:
                lev = t['leverage']
                if lev not in leverage_performance:
                    leverage_performance[lev] = {'trades': 0, 'wins': 0, 'pnl': 0}
                leverage_performance[lev]['trades'] += 1
                leverage_performance[lev]['pnl'] += t['pnl']
                if t['pnl'] > 0:
                    leverage_performance[lev]['wins'] += 1
            
            if len(leverage_performance) > 1:
                insights.append(f"\n**杠杆效果:**")
                for lev, perf in sorted(leverage_performance.items()):
                    if perf['trades'] >= 2:
                        lev_rate = (perf['wins'] / perf['trades']) * 100
                        insights.append(f"- {lev}x杠杆: {perf['trades']}笔, 胜率{lev_rate:.1f}%, 盈亏${perf['pnl']:.2f}")
                
                # 找出最佳杠杆
                best_lev = max(leverage_performance.items(), key=lambda x: x[1]['pnl'] if x[1]['trades'] >= 2 else -999999)
                if best_lev[1]['trades'] >= 2 and best_lev[1]['pnl'] > 0:
                    insights.append(f"- 💡 {best_lev[0]}x杠杆表现最好，高置信度时优先使用")
            
            # 5. 生成具体策略建议
            insights.append(f"\n**自动优化建议:**")
            
            if win_rate < 40:
                insights.append("- ⚠️ 胜率偏低(<40%), 建议提高入场置信度阈值，减少交易频率")
            elif win_rate > 60:
                insights.append("- ✅ 胜率良好(>60%), 可适当增加仓位")
            
            if avg_loss != 0 and abs(avg_win / avg_loss) < 1.5:
                insights.append("- ⚠️ 盈亏比偏低，建议扩大止盈目标或收紧止损")
            
            if total_pnl < 0:
                insights.append("- 🔴 总体亏损，建议减少交易频率，等待更好的机会")
            elif total_pnl > 0:
                insights.append("- 🟢 总体盈利，继续执行当前策略")
            
            return "\n".join(insights)
            
        except Exception as e:
            self.logger.warning(f"Failed to generate trading insights: {e}")
            return ""
    
    def _calculate_sharpe_ratio(self, portfolio: Dict, risk_free_rate: float = 0.02) -> float:
        """
        计算夏普比率 (Sharpe Ratio)
        
        Sharpe Ratio = (平均收益率 - 无风险利率) / 收益率标准差
        
        Args:
            portfolio: 投资组合信息
            risk_free_rate: 年化无风险利率 (默认2%)
            
        Returns:
            夏普比率，正常范围 -2 到 +3
        """
        if not self.db or 'model_id' not in portfolio:
            return 0.0
        
        try:
            model_id = portfolio['model_id']
            trades = self.db.get_trades(model_id, limit=100)
            
            # 只计算已平仓交易
            closed_trades = [t for t in trades if t.get('signal') == 'close_position']
            
            if len(closed_trades) < 5:  # 样本太少，不计算
                return 0.0
            
            # 计算每笔交易收益率
            returns = []
            for trade in closed_trades:
                pnl = float(trade.get('pnl', 0) or 0)
                price = float(trade.get('price', 1) or 1)
                quantity = float(trade.get('quantity', 1) or 1)
                
                # 计算投入金额（近似）
                invested = price * quantity
                if invested > 0:
                    ret = pnl / invested
                    returns.append(ret)
            
            if len(returns) < 2:
                return 0.0
            
            # 计算平均收益和标准差
            import statistics
            avg_return = statistics.mean(returns)
            std_return = statistics.stdev(returns)
            
            if std_return == 0:
                return 0.0
            
            # 将无风险利率转换为每笔交易（假设每笔交易持续约1天）
            daily_rf = risk_free_rate / 365
            
            # 计算夏普比率
            sharpe = (avg_return - daily_rf) / std_return
            
            # 限制范围 [-3, 3]
            return max(-3.0, min(3.0, round(sharpe, 2)))
            
        except Exception as e:
            self.logger.warning(f"Failed to calculate Sharpe ratio: {e}")
            return 0.0
    
    def _check_rule_based_exits(self, market_state: Dict, portfolio: Dict) -> Dict:
        """
        规则驱动的止盈止损检查
        
        规则:
        1. 亏损超过8% → 紧急止损
        2. 盈利超过10% → 止盈
        3. 盈利超过5% + RSI极端 → 部分止盈
        4. 趋势反转信号 → 平仓
        
        Returns:
            需要执行的平仓决策 {coin: decision}
        """
        exit_decisions = {}
        positions = portfolio.get('positions', [])
        
        for pos in positions:
            coin = pos.get('coin', '')
            current_price = float(market_state.get(coin, {}).get('price', 0) or 0)
            entry_price = float(pos.get('avg_price', 0) or 0)
            leverage = int(pos.get('leverage', 1) or 1)
            side = pos.get('side', 'long')
            
            if entry_price <= 0 or current_price <= 0:
                continue
            
            # 获取技术指标
            indicators = market_state.get(coin, {}).get('indicators', {})
            rsi14 = float(indicators.get('rsi_14', 50) or 50)
            macd = float(indicators.get('macd', 0) or 0)
            macd_signal = float(indicators.get('macd_signal', 0) or 0)
            
            # 计算盈亏百分比
            if side == 'long':
                pnl_pct = (current_price - entry_price) / entry_price * 100 * leverage
                trend_reversed = macd < macd_signal  # 死叉 = 趋势反转
                rsi_extreme = rsi14 > 75  # RSI超买
            else:
                pnl_pct = (entry_price - current_price) / entry_price * 100 * leverage
                trend_reversed = macd > macd_signal  # 金叉 = 趋势反转
                rsi_extreme = rsi14 < 25  # RSI超卖
            
            exit_reason = None
            confidence = 0.0
            
            # 规则1: 紧急止损 - 亏损超过8%
            if pnl_pct <= -8:
                exit_reason = f'紧急止损: {side}仓亏损{pnl_pct:.1f}%超过阈值'
                confidence = 0.95
                self.logger.warning(f"[STOP-LOSS] {coin} {side} 亏损{pnl_pct:.1f}%，触发止损")
            
            # 规则2: 止盈 - 盈利超过10%
            elif pnl_pct >= 10:
                exit_reason = f'止盈: {side}仓盈利{pnl_pct:.1f}%达到目标'
                confidence = 0.90
                self.logger.info(f"[TAKE-PROFIT] {coin} {side} 盈利{pnl_pct:.1f}%，触发止盈")
            
            # 规则3: 盈加RSI极端 - 盈利5%且RSI超买/超卖
            elif pnl_pct >= 5 and rsi_extreme:
                exit_reason = f'止盈: {side}仓盈利{pnl_pct:.1f}%且RSI={rsi14:.0f}极端'
                confidence = 0.85
                self.logger.info(f"[RSI-EXIT] {coin} {side} 盈利{pnl_pct:.1f}%+RSI极端，建议平仓")
            
            # 规则4: 趋势反转 + 小亏损 - 及时止损
            elif pnl_pct <= -3 and trend_reversed:
                exit_reason = f'止损: {side}仓亏损{pnl_pct:.1f}%且MACD趋势反转'
                confidence = 0.80
                self.logger.warning(f"[TREND-EXIT] {coin} {side} 亏损{pnl_pct:.1f}%+趋势反转，建议平仓")
            
            # 规则5: 盈利回吐 - 曾经盈利5%+但现在只有微利且趋势反转
            elif 0 < pnl_pct < 2 and trend_reversed:
                exit_reason = f'保护利润: {side}仓盈利回吐至{pnl_pct:.1f}%且趋势反转'
                confidence = 0.75
                self.logger.info(f"[PROTECT-PROFIT] {coin} {side} 盈利回吐+趋势反转，建议平仓")
            
            if exit_reason:
                exit_decisions[coin] = {
                    'signal': 'close_position',
                    'confidence': confidence,
                    'reasoning': exit_reason,
                    'rule_based': True,
                    'pnl_pct': pnl_pct
                }
        
        return exit_decisions
    
    def make_decision(self, market_state: Dict, portfolio: Dict,
                     account_info: Dict) -> Dict:
        # 首先检查规则驱动的止盈止损
        rule_exits = self._check_rule_based_exits(market_state, portfolio)
        if rule_exits:
            self.logger.info(f"[规则驱动] 发现{len(rule_exits)}个需要平仓的持仓")
            return rule_exits
        
        prompt = self._build_prompt(market_state, portfolio, account_info)
        
        # 记录提示词到日志
        self.logger.info("=" * 60)
        self.logger.info("[PROMPT] AI Trading Decision Request")
        self.logger.info("=" * 60)
        for line in prompt.split('\n'):
            self.logger.info(f"[PROMPT] {line}")
        self.logger.info("=" * 60)

        response = self._call_llm(prompt)
        
        # 记录AI响应
        self.logger.info("[AI-RESPONSE] %s", response[:500] if len(response) > 500 else response)

        decisions = self._parse_response(response)

        # Validate and filter decisions
        validated_decisions = self._validate_decisions(decisions, market_state, portfolio)

        return validated_decisions

    def _validate_decisions(self, decisions: Dict, market_state: Dict, portfolio: Dict) -> Dict:
        """Validate AI decisions with dynamic risk management, volume confirmation, and sentiment filtering"""
        if not decisions:
            return {}

        validated = {}
        scored_decisions = []  # 带评分的决策列表
        total_value = float(portfolio.get('total_value', 0) or 0)

        # Count existing positions by coin
        existing_positions = {pos['coin']: pos['side'] for pos in portfolio['positions']}
        
        # 限制同时开仓数量（使用配置）
        max_positions = TradingConfig.MAX_POSITIONS
        max_new_per_cycle = getattr(TradingConfig, 'MAX_NEW_POSITIONS_PER_CYCLE', 1)
        
        # 获取市场情绪
        fng_index = 50  # 默认中性
        if hasattr(self, 'market_fetcher') and self.market_fetcher:
            try:
                sentiment = self.market_fetcher.get_market_sentiment()
                fng_index = float(sentiment.get('fear_greed_index', 50) or 50)
            except:
                pass
        
        # 获取情绪调整策略
        sentiment_action, sentiment_penalty = TradingConfig.get_sentiment_adjustment(fng_index)

        for coin, decision in decisions.items():
            # Check coin validity
            if coin not in market_state:
                self.logger.warning(f"[WARN] Invalid coin {coin}, skipping")
                continue

            signal = decision.get('signal', '').lower()
            price = float(market_state[coin].get('price', 0) or 0)
            indicators = market_state[coin].get('indicators', {})

            # Validate signal type
            if signal not in ['buy_to_enter', 'sell_to_enter', 'close_position', 'hold']:
                self.logger.warning(f"[WARN] Invalid signal '{signal}' for {coin}, skipping")
                continue

            # Skip 'hold' signals
            if signal == 'hold':
                continue

            # Validate close_position
            if signal == 'close_position':
                if coin not in existing_positions:
                    self.logger.warning(f"[WARN] Cannot close {coin}, no position exists")
                    continue
                validated[coin] = decision
                continue

            # Validate entry signals with dynamic risk management
            confidence = safe_float(decision.get('confidence'), 0)
            volatility = safe_float(indicators.get('volatility_7d'), 50)
            atr = safe_float(indicators.get('atr_14'), 0)

            # 技术指标（确保都是float）
            trend_alignment = float(indicators.get('trend_alignment', 0.5) or 0.5)
            macd = float(indicators.get('macd', 0) or 0)
            macd_signal_line = float(indicators.get('macd_signal', 0) or 0)
            rsi = float(indicators.get('rsi_14', 50) or 50)
            current_volume = float(indicators.get('volume_24h', 0) or 0)
            avg_volume = float(indicators.get('average_volume_7d', 0) or 0)
            
            # ============================================================
            # 策略优化1: 动态置信度阈值
            # ============================================================
            dynamic_threshold = TradingConfig.get_dynamic_confidence_threshold(volatility)
            self.logger.info(f"[THRESHOLD] {coin}: 波动率={volatility:.1f}% → 动态阈值={dynamic_threshold:.2f}")
            
            # ============================================================
            # 策略优化2: 成交量确认调整
            # ============================================================
            volume_adjustment = TradingConfig.get_volume_adjustment(current_volume, avg_volume)
            adjusted_confidence = confidence + volume_adjustment
            
            if volume_adjustment < 0:
                self.logger.info(f"[VOLUME] {coin}: 缩量信号，置信度调整 {confidence:.2f} → {adjusted_confidence:.2f}")
            elif volume_adjustment > 0:
                self.logger.info(f"[VOLUME] {coin}: 放量信号，置信度调整 {confidence:.2f} → {adjusted_confidence:.2f}")
            
            # ============================================================
            # 策略优化3: 情绪过滤器
            # ============================================================
            if sentiment_action != 'normal':
                # 极端情绪时的信号过滤
                if sentiment_action == 'cautious_long' and signal == 'buy_to_enter':
                    # 极度恐慌时做多需要更高置信度
                    adjusted_confidence -= sentiment_penalty
                    self.logger.info(f"[SENTIMENT] 极度恐慌(FGI={fng_index:.0f})，做多置信度-{sentiment_penalty:.0%}")
                elif sentiment_action == 'prefer_short' and signal == 'buy_to_enter':
                    # 极度贪婪时不建议做多
                    adjusted_confidence -= sentiment_penalty * 1.5
                    self.logger.info(f"[SENTIMENT] 极度贪婪(FGI={fng_index:.0f})，做多置信度-{sentiment_penalty*1.5:.0%}")
                elif sentiment_action == 'prefer_short' and signal == 'sell_to_enter':
                    # 极度贪婪时做空加分
                    adjusted_confidence += sentiment_penalty * 0.5
                    self.logger.info(f"[SENTIMENT] 极度贪婪(FGI={fng_index:.0f})，做空置信度+{sentiment_penalty*0.5:.0%}")
            
            # ============================================================
            # 策略优化4: 动态RSI阈值
            # ============================================================
            is_uptrend = trend_alignment > 0.6
            rsi_overbought = TradingConfig.get_rsi_threshold(is_uptrend, 'overbought')
            rsi_oversold = TradingConfig.get_rsi_threshold(is_uptrend, 'oversold')
            
            # RSI极端值警告（但不阻止交易，只调整置信度）
            if signal == 'buy_to_enter' and rsi > rsi_overbought:
                rsi_penalty = min((rsi - rsi_overbought) / 100, 0.15)
                adjusted_confidence -= rsi_penalty
                self.logger.info(f"[RSI] {coin}: RSI={rsi:.1f}超买(阈值{rsi_overbought})，置信度-{rsi_penalty:.0%}")
            elif signal == 'sell_to_enter' and rsi < rsi_oversold:
                rsi_penalty = min((rsi_oversold - rsi) / 100, 0.15)
                adjusted_confidence -= rsi_penalty
                self.logger.info(f"[RSI] {coin}: RSI={rsi:.1f}超卖(阈值{rsi_oversold})，置信度-{rsi_penalty:.0%}")
            
            # ============================================================
            # 最终置信度检查
            # ============================================================
            if adjusted_confidence < dynamic_threshold:
                self.logger.warning(f"[SKIP] {coin}: 调整后置信度 {adjusted_confidence:.2f} < 动态阈值 {dynamic_threshold:.2f}")
                continue
            
            # 安全检查2：极端波动
            if volatility > TradingConfig.MAX_VOLATILITY_THRESHOLD:
                self.logger.warning(f"[SKIP] {coin}: 波动率 {volatility:.1f}% 过高")
                continue
            
            self.logger.info(f"[OK] {coin} {signal}: 原始置信度={confidence:.2f}, 调整后={adjusted_confidence:.2f}, 阈值={dynamic_threshold:.2f}")

            # Check for existing opposite position
            if coin in existing_positions:
                existing_side = existing_positions[coin]
                new_side = 'long' if signal == 'buy_to_enter' else 'short'
                if existing_side != new_side:
                    # 策略优化5: 允许平仓换方向（如果启用）
                    if TradingConfig.ALLOW_POSITION_SWAP:
                        self.logger.info(f"[SWAP] {coin}: 建议先平{existing_side}仓，再开{new_side}仓")
                        # 添加一个平仓决策
                        validated[coin] = {
                            'signal': 'close_position',
                            'reasoning': f'换仓: 平{existing_side}准备开{new_side}',
                            'swap_to': new_side,
                            'original_decision': decision
                        }
                        continue
                    else:
                        self.logger.warning(f"[WARN] {coin} already has {existing_side} position, cannot open {new_side}")
                        continue
                if len(existing_positions) >= max_positions:
                    self.logger.warning(f"[WARN] Maximum {max_positions} positions reached, cannot add to {coin}")
                    continue

            # 优先使用LLM返回的quantity，如果没有则使用风险管理计算
            side = 'long' if signal == 'buy_to_enter' else 'short'
            llm_quantity = safe_float(decision.get('quantity'), 0)
            llm_leverage = safe_float(decision.get('leverage'), TradingConfig.DEFAULT_LEVERAGE)
            
            if llm_quantity > 0:
                # LLM 提供了数量，使用它
                quantity = llm_quantity
                leverage = min(llm_leverage, TradingConfig.MAX_LEVERAGE)
                self.logger.info(f"[LLM] {coin}: 使用LLM建议数量 {quantity:.6f} (约${quantity*price:.2f})")
            else:
                # LLM 没有提供，使用风险管理计算
                quantity, leverage = self.risk_manager.calculate_position_size(
                    total_value, volatility, confidence, price
                )
                self.logger.info(f"[RISK] {coin}: 风险管理计算数量 {quantity:.6f} (约${quantity*price:.2f})")

            # 计算动态止损止盈（如果LLM没有提供则计算）
            llm_stop_loss = decision.get('stop_loss')
            llm_profit_target = decision.get('profit_target')

            if llm_stop_loss:
                stop_loss = safe_float(llm_stop_loss, 0)
                if stop_loss <= 0:  # 解析失败，使用风险管理计算
                    stop_loss = self.risk_manager.calculate_stop_loss(
                        price, side, volatility, atr
                    )
            else:
                stop_loss = self.risk_manager.calculate_stop_loss(
                    price, side, volatility, atr
                )
            
            if llm_profit_target:
                profit_target = safe_float(llm_profit_target, 0)
                if profit_target <= 0:  # 解析失败，使用风险管理计算
                    profit_target = self.risk_manager.calculate_profit_target(
                        price, stop_loss, side, risk_reward_ratio=TradingConfig.RISK_REWARD_RATIO
                    )
            else:
                profit_target = self.risk_manager.calculate_profit_target(
                    price, stop_loss, side, risk_reward_ratio=TradingConfig.RISK_REWARD_RATIO
                )

            # 验证交易规模：最低20美元，最高不超过账户40%
            trade_value = quantity * price
            min_trade_usd = TradingConfig.MIN_TRADE_VALUE_USD  # 20美元
            max_trade_value = total_value * TradingConfig.MAX_TRADE_VALUE_PCT
            
            if trade_value < min_trade_usd:
                # 低于最小值，调整到最小值
                quantity = min_trade_usd / price
                trade_value = min_trade_usd
                self.logger.info(f"[ADJUST] {coin}: 金额过小，调整到最小${min_trade_usd}")
            if trade_value > max_trade_value:
                # 超过最大值，限制到最大值
                quantity = max_trade_value / price
                trade_value = max_trade_value
                self.logger.info(f"[ADJUST] {coin}: 金额过大，限制到${max_trade_value:.2f}")

            # 更新决策
            decision['quantity'] = quantity
            decision['leverage'] = leverage
            decision['stop_loss'] = stop_loss
            decision['profit_target'] = profit_target

            # 计算交易质量评分（用于排序优先级）
            quality_score = self._calculate_trade_quality_score(
                adjusted_confidence, volatility, trend_alignment, macd, macd_signal_line, 
                rsi, signal, price, stop_loss, profit_target,
                volume_ratio=current_volume / avg_volume if avg_volume > 0 else 1.0
            )
            
            # 策略优化6: 质量分过滤
            if TradingConfig.QUALITY_SCORE_ENABLED and quality_score < TradingConfig.MIN_QUALITY_SCORE:
                self.logger.warning(f"[SKIP] {coin}: 质量分 {quality_score:.1f} < 最低要求 {TradingConfig.MIN_QUALITY_SCORE}")
                continue
            
            decision['quality_score'] = quality_score
            decision['adjusted_confidence'] = adjusted_confidence
            
            # LLM-First: 质量分只用于排序，不用于过滤
            # 如果有多个交易机会，优先执行得分高的
            scored_decisions.append({
                'coin': coin,
                'decision': decision,
                'score': quality_score,
                'signal': signal
            })
            
            # 记录LLM决策详情
            reasoning = decision.get('reasoning', 'N/A')[:60]
            self.logger.info(f"[LLM-DECISION] {coin} {signal}: conf={confidence:.2f}, score={quality_score:.1f}")
            self.logger.info(f"  → 理由: {reasoning}")

        # 精准狙击：只选择得分最高的交易
        # 按质量分降序排序
        scored_decisions.sort(key=lambda x: x['score'], reverse=True)
        
        # 统计需要开新仓的数量限制
        new_position_count = 0
        
        for item in scored_decisions:
            coin = item['coin']
            decision = item['decision']
            signal = item['signal']
            score = item['score']
            
            if signal in ['buy_to_enter', 'sell_to_enter']:
                # 检查是否超过每周期开仓限制
                if new_position_count >= max_new_per_cycle:
                    self.logger.warning(f"[LIMIT] {coin} 跳过：本周期已开仓{new_position_count}个 (限制{max_new_per_cycle})")
                    continue
                    
                # 检查是否超过总持仓限制
                if len(existing_positions) + new_position_count >= max_positions:
                    self.logger.warning(f"[LIMIT] {coin} 跳过：已达最大持仓数{max_positions}")
                    continue
                
                new_position_count += 1

            validated[coin] = decision
            self.logger.info(f"[FINAL] {coin} {signal}: 质量分={score:.1f} - 已选中")

        # 输出汇总
        entry_count = sum(1 for d in validated.values() if d.get('signal') in ['buy_to_enter', 'sell_to_enter'])
        close_count = sum(1 for d in validated.values() if d.get('signal') == 'close_position')
        self.logger.info(f"[SUMMARY] 本周期: 开仓{entry_count}, 平仓{close_count}, 总待执行{len(validated)}")

        return validated
    
    def _calculate_trade_quality_score(self, confidence: float, volatility: float, 
                                        trend_alignment: float, macd: float, 
                                        macd_signal: float, rsi: float, signal: str,
                                        price: float, stop_loss: float, 
                                        profit_target: float,
                                        volume_ratio: float = 1.0) -> float:
        """
        计算交易质量评分（满分100）
        包含成交量确认和动态权重
        """
        weights = TradingConfig.QUALITY_SCORE_WEIGHTS
        
        score = 0
        
        # 1. 置信度评分
        conf_score = min(confidence, 1.0) * weights.get('confidence', 35)
        score += conf_score
        
        # 2. 趋势一致性评分
        alignment_score = min(trend_alignment, 1.0) * weights.get('trend_alignment', 25)
        score += alignment_score
        
        # 3. 动量确认评分
        macd_diff = abs(macd - macd_signal)
        macd_strength = min(macd_diff / 0.01, 1.0) if macd_diff > 0 else 0
        momentum_score = macd_strength * weights.get('momentum', 15)
        score += momentum_score
        
        # 4. 波动率评分（低波动得高分）
        vol_weight = weights.get('volatility', 10)
        if volatility < 30:
            vol_score = vol_weight
        elif volatility < 50:
            vol_score = vol_weight * 0.8
        elif volatility < 70:
            vol_score = vol_weight * 0.5
        else:
            vol_score = vol_weight * 0.2
        score += vol_score
        
        # 5. 风险回报比评分
        rr_weight = weights.get('risk_reward', 10)
        if signal == 'buy_to_enter':
            risk = price - stop_loss
            reward = profit_target - price
        else:
            risk = stop_loss - price
            reward = price - profit_target
        
        if risk > 0:
            rr_ratio = reward / risk
            rr_score = min(rr_ratio / 3.0, 1.0) * rr_weight  # 3:1 得满分
        else:
            rr_score = 0
        score += rr_score
        
        # 6. 成交量确认评分
        vol_confirm_weight = weights.get('volume', 5)
        if volume_ratio > 1.5:
            # 放量：满分
            volume_score = vol_confirm_weight
        elif volume_ratio > 1.0:
            # 正常成交量
            volume_score = vol_confirm_weight * 0.7
        elif volume_ratio > 0.6:
            # 轻度缩量
            volume_score = vol_confirm_weight * 0.4
        else:
            # 严重缩量
            volume_score = 0
        score += volume_score
        
        return score
    
    def _get_system_prompt(self) -> str:
        """
        获取优化的 System Prompt
        基于专业交易代理的最佳实践设计
        """
        return f"""# 角色定义
你是自主加密货币交易代理，在 OKX 交易所执行永续合约交易。
使命：通过系统化、纪律化的交易最大化风险调整后收益。

# 交易环境
- 交易所: OKX 永续合约
- 币种: {', '.join(TradingConfig.TRADING_COINS)}
- 决策频率: 每 {TradingConfig.TRADING_CYCLE_SECONDS // 60} 分钟
- 杠杆范围: {TradingConfig.MIN_LEVERAGE}x - {TradingConfig.MAX_LEVERAGE}x
- 交易费: ~0.08%

# 操作空间（4种动作）
1. buy_to_enter: 开多（看涨）
2. sell_to_enter: 开空（看跌）  
3. hold: 维持现有持仓
4. close_position: 平仓退出

# 仓位计算公式
仓位金额 = 可用资金 × 杠杆 × 分配比例
仓位数量 = 仓位金额 / 当前价格

# 杠杆选择
- 低置信度(0.3-0.5): 1-2x
- 中置信度(0.5-0.7): 2-3x  
- 高置信度(0.7-0.9): 3-5x

# 风险管理（强制要求）
每笔交易必须指定：
- profit_target: 止盈价（盈亏比≥2:1）
- stop_loss: 止损价（限制单笔亏损≤账户3%）
- confidence: 置信度(0-1)

# 数据时间框架
- 日内数据: 3分钟间隔，约10个数据点（用于短线入场时机）
- 4小时数据: 约10个数据点（用于趋势判断和关键位置）
- ⚠️ 所有序列数据排序: 旧→新，最后一个值是最新数据

# 技术指标解读
- EMA: 价格>EMA=上涨趋势, 价格<EMA=下跌趋势
- MACD: 正值=看涨动量, 负值=看跌动量; 金叉=买入信号, 死叉=卖出信号
- RSI: >70超买(可能回调), <30超卖(可能反弹), 40-60中性
- ATR: 越高波动越大（需要更宽止损）

# 核心原则
1. 资金保护第一：保护本金比追逐收益更重要
2. 纪律高于情绪：严格执行止盈止损
3. 质量高于数量：少量高确信交易胜过大量低质量交易
4. 适应波动：根据市场条件调整仓位
5. 顺势而为：不要逆势操作

# 常见陷阱
⚠️ 过度交易：频繁交易会被手续费吃掉利润
⚠️ 报复交易：亏损后加仓想回本
⚠️ 过度杠杆：高杠杆放大亏损
⚠️ 忽视相关性：BTC通常领涨领跌

# 输出格式
返回纯JSON，格式如下：
{{
  "decisions": {{
    "币种": {{
      "signal": "buy_to_enter/sell_to_enter/hold/close_position",
      "confidence": 0.0-1.0,
      "quantity": 数量,
      "leverage": 杠杆倍数,
      "profit_target": 止盈价,
      "stop_loss": 止损价,
      "reasoning": "简短理由"
    }}
  }}
}}

重要：数值不要带$符号，直接输出数字。没有好机会就返回空decisions。"""

    def _build_prompt(self, market_state: Dict, portfolio: Dict,
                     account_info: Dict) -> str:
        """
        构建用户提示词 - 参考专业交易代理模板
        包含: 市场数据、技术指标、账户状态、持仓信息
        """
        # ============================================================
        # 1. 时间戳计算 - 建立时间感
        # ============================================================
        if AITrader._trading_start_time:
            elapsed = datetime.now() - AITrader._trading_start_time
            minutes_elapsed = int(elapsed.total_seconds() / 60)
            hours_elapsed = minutes_elapsed // 60
            mins_remainder = minutes_elapsed % 60
            if hours_elapsed > 0:
                time_str = f"{hours_elapsed}小时{mins_remainder}分钟"
            else:
                time_str = f"{minutes_elapsed}分钟"
        else:
            minutes_elapsed = 0
            time_str = "刚刚开始"
        
        # ============================================================
        # 2. 基础参数计算
        # ============================================================
        total_value = float(portfolio.get('total_value', 0) or 0)
        cash = float(portfolio.get('cash', 0) or 0)
        realized_pnl = float(portfolio.get('realized_pnl', 0) or 0)
        
        min_trade_pct = total_value * TradingConfig.PROMPT_MIN_TRADE_PCT
        min_trade_usd = TradingConfig.MIN_TRADE_VALUE_USD
        min_trade_value = max(min_trade_pct, min_trade_usd)
        max_trade_value = total_value * TradingConfig.MAX_TRADE_VALUE_PCT

        # 获取市场情绪
        market_sentiment = self.market_fetcher.get_market_sentiment() if hasattr(self, 'market_fetcher') and self.market_fetcher else {}
        fng_index = float(market_sentiment.get('fear_greed_index', 50) or 50)
        fng_label = market_sentiment.get('fear_greed_label', '中性') or '中性'
        market_trend = market_sentiment.get('market_trend', '震荡') or '震荡'
        btc_dominance = float(market_sentiment.get('btc_dominance', 50) or 50)
        
        # 计算收益率和夏普比率
        initial_capital = float(account_info.get('initial_capital', 10000) or 10000)
        return_pct = ((total_value - initial_capital) / initial_capital * 100) if initial_capital > 0 else 0
        sharpe_ratio = self._calculate_sharpe_ratio(portfolio) if hasattr(self, '_calculate_sharpe_ratio') else 0
        
        # ============================================================
        # 3. 风险敞口计算
        # ============================================================
        positions = portfolio.get('positions', [])
        total_long_exposure = 0
        total_short_exposure = 0
        total_unrealized_pnl = 0
        total_margin_used = 0  # 实际占用保证金
        
        # 优先使用 OKX 账户级别的冻结保证金（全仓模式下最准确）
        frozen_margin = float(portfolio.get('frozen_margin', 0) or 0)
        
        for pos in positions:
            coin = pos.get('coin', '')
            current_price = float(market_state.get(coin, {}).get('price', 0) or 0)
            quantity = float(pos.get('quantity', 0) or 0)
            entry_price = float(pos.get('avg_price', 0) or 0)
            leverage = int(pos.get('leverage', 1) or 1)
            side = pos.get('side', 'long')
            
            # 优先使用 OKX 返回的名义价值和保证金
            notional = float(pos.get('notional_usd', 0))
            if notional <= 0:
                # 备用计算：数量 × 价格
                notional = quantity * current_price
            
            # 如果没有账户级别冻结保证金，则累加每个持仓的保证金
            if frozen_margin <= 0:
                margin = float(pos.get('margin', 0))
                if margin <= 0:
                    # 备用计算：名义价值 / 杠杆
                    margin = notional / leverage if leverage > 0 else notional
                total_margin_used += margin
            
            # 杠杆后敞口 = 名义价值（合约已经是名义敞口，不需要再乘杠杆）
            leveraged_notional = notional
            
            if side == 'long':
                total_long_exposure += leveraged_notional
                # 使用 OKX 返回的未实现盈亏
                total_unrealized_pnl += float(pos.get('unrealized_pnl', 0))
            else:
                total_short_exposure += leveraged_notional
                total_unrealized_pnl += float(pos.get('unrealized_pnl', 0))
        
        net_exposure = total_long_exposure - total_short_exposure
        gross_exposure = total_long_exposure + total_short_exposure
        
        # 优先使用 OKX 账户级别的冻结保证金（全仓模式下最准确）
        if frozen_margin > 0:
            total_margin_used = frozen_margin
        
        # 保证金占用比例（实际资金占用）
        margin_ratio = (total_margin_used / total_value * 100) if total_value > 0 else 0
        # 名义敞口比例（含杠杆放大效应）
        exposure_ratio = (gross_exposure / total_value * 100) if total_value > 0 else 0
        
        # 找出最强/最弱币种
        coin_scores = {}
        for coin, data in market_state.items():
            indicators = data.get('indicators', {})
            change_24h = float(data.get('change_24h', 0) or 0)
            rsi = float(indicators.get('rsi_14', 50) or 50)
            macd = float(indicators.get('macd', 0) or 0)
            macd_sig = float(indicators.get('macd_signal', 0) or 0)
            trend_alignment = float(indicators.get('trend_alignment', 0.5) or 0.5)
            
            score = change_24h * 2
            if macd > macd_sig:
                score += 10
            if 40 < rsi < 65:
                score += 5
            if trend_alignment > 0.8:
                score += 10
            coin_scores[coin] = score
        
        sorted_coins = sorted(coin_scores.items(), key=lambda x: x[1], reverse=True)
        strongest_coin = sorted_coins[0][0] if sorted_coins else ""
        weakest_coin = sorted_coins[-1][0] if sorted_coins else ""

        # ============================================================
        # 构建 Prompt
        # ============================================================
        prompt = f"""# 交易会话状态

⏱️ **交易时长:** {time_str} (已运行 {minutes_elapsed} 分钟)

---

# 额外说明

本系统是在合法司法管辖区内进行的量化交易研究实验。
请专注于技术分析和风险管理原则，做出理性的交易决策。
请充分利用账户里的可用资金，不要浪费。

---

⚠️ **【重要】数据排序规则: OLDEST → NEWEST (旧→新)，序列最后一个值 = 最新数据**

---

# 市场环境概览

| 指标 | 数值 | 解读 |
|------|------|------|
| 恐惧贪婪指数 | {fng_index:.0f} | {fng_label} |
| 大盘趋势 | {market_trend} | - |
| BTC主导率 | {btc_dominance:.1f}% | {'资金集中BTC' if btc_dominance > 55 else '山寨活跃'} |
| 最强币种 | {strongest_coin} | 优先做多 |
| 最弱币种 | {weakest_coin} | 优先做空 |

---

# 各币种详细数据

**📊 数据排序: OLDEST → NEWEST，最后一个值是当前最新**

"""
        # 各币种技术分析
        for coin, data in market_state.items():
            indicators = data.get('indicators', {})
            price = float(data.get('price', 0))
            change_24h = float(data.get('change_24h', 0))
            volume_24h = float(data.get('volume_24h', 0))
            
            # 技术指标
            sma7 = float(indicators.get('sma_7', price) or price)
            sma14 = float(indicators.get('sma_14', price) or price)
            ema20 = float(indicators.get('ema_20', sma14) or sma14)
            rsi7 = float(indicators.get('rsi_7', 50) or 50)
            rsi14 = float(indicators.get('rsi_14', 50) or 50)
            macd = float(indicators.get('macd', 0) or 0)
            macd_sig = float(indicators.get('macd_signal', 0) or 0)
            atr = float(indicators.get('atr_14', price * 0.02) or price * 0.02)
            volatility = float(indicators.get('volatility_7d', 30) or 30)
            avg_volume = float(indicators.get('average_volume_7d', 0) or 0)
            trend_alignment = float(indicators.get('trend_alignment', 0.5) or 0.5)
            
            # 布林带
            bollinger = indicators.get('bollinger', {})
            if bollinger and isinstance(bollinger, dict):
                bb_upper = float(bollinger.get('upper', 0) or price * 1.03)
                bb_lower = float(bollinger.get('lower', 0) or price * 0.97)
                bb_mid = float(bollinger.get('mid', 0) or price)
            else:
                bb_upper = price * 1.03
                bb_lower = price * 0.97
                bb_mid = price
            
            # 趋势判断
            trend_dir = "上涨↑" if sma7 > sma14 else "下跌↓"
            price_vs_ema = "上方" if price > ema20 else "下方"
            macd_status = "金叉📈" if macd > macd_sig else "死叉📉"
            trend_sync = "一致✓" if trend_alignment >= 0.8 else "分歧✗"
            
            # RSI解读
            if rsi14 > 70:
                rsi_hint = "超买(回调风险)"
            elif rsi14 < 30:
                rsi_hint = "超卖(反弹机会)"
            elif rsi14 > 55:
                rsi_hint = "偏强"
            else:
                rsi_hint = "偏弱"
            
            # 成交量
            vol_status = ""
            if avg_volume > 0 and volume_24h > 0:
                vol_ratio = volume_24h / avg_volume
                if vol_ratio > 1.5:
                    vol_status = "放量🔥"
                elif vol_ratio < 0.5:
                    vol_status = "缩量"
                else:
                    vol_status = "正常"
            
            # 强弱标记
            strength_tag = " [🟢强势]" if coin == strongest_coin else (" [🔴弱势]" if coin == weakest_coin else "")
            
            # 获取日内数据（3分钟间隔）、4小时数据、合约数据
            intraday_data = {}
            h4_data = {}
            futures_data = {}
            if hasattr(self, 'market_fetcher') and self.market_fetcher:
                try:
                    intraday_data = self.market_fetcher.get_intraday_klines(coin, interval='3m', limit=TradingConfig.KLINE_INTRADAY_LIMIT)
                    self.logger.debug(f"[{coin}] 日内数据: {len(intraday_data.get('prices', []))} 个数据点")
                except Exception as e:
                    self.logger.warning(f"[{coin}] 获取日内数据失败: {e}")
                
                try:
                    h4_data = self.market_fetcher.get_4h_klines(coin, limit=TradingConfig.KLINE_H4_LIMIT)
                    self.logger.debug(f"[{coin}] 4小时数据: {len(h4_data.get('prices', []))} 个数据点")
                except Exception as e:
                    self.logger.warning(f"[{coin}] 获取4小时数据失败: {e}")
                
                # 获取合约数据（资金费率、持仓量）
                try:
                    futures_data = self.market_fetcher.get_futures_data(coin)
                except Exception as e:
                    self.logger.debug(f"[{coin}] 获取合约数据失败: {e}")
            
            # 解析合约数据 (OKX)
            funding_rate = futures_data.get('funding_rate', 0)
            next_funding_rate = futures_data.get('next_funding_rate', 0)
            open_interest = futures_data.get('open_interest', 0)
            open_interest_ccy = futures_data.get('open_interest_ccy', 0)
            oi_change = futures_data.get('oi_change_24h', 0)
            
            # 资金费率解读 (OKX 费率通常在 -0.375% ~ +0.375% 之间)
            if funding_rate > 0.1:
                funding_hint = "多头极度拥挤🔴"
            elif funding_rate > 0.05:
                funding_hint = "多头拥挤⚠️"
            elif funding_rate < -0.1:
                funding_hint = "空头极度拥挤🔴"
            elif funding_rate < -0.05:
                funding_hint = "空头拥挤⚠️"
            elif funding_rate > 0.01:
                funding_hint = "偏多"
            elif funding_rate < -0.01:
                funding_hint = "偏空"
            else:
                funding_hint = "中性"
            
            # 格式化持仓量显示
            if open_interest_ccy >= 1000000:
                oi_display = f"{open_interest_ccy/1000000:.2f}M"
            elif open_interest_ccy >= 1000:
                oi_display = f"{open_interest_ccy/1000:.2f}K"
            else:
                oi_display = f"{open_interest_ccy:.2f}"
            
            prompt += f"""## {coin}{strength_tag}

**当前快照:**
- 现价: {price:.4f} | 24h涨跌: {change_24h:+.2f}%
- EMA20: {ema20:.4f} | 价格在EMA{price_vs_ema}
- MACD: {macd:.6f} | 状态: {macd_status}
- RSI(7): {rsi7:.1f} | RSI(14): {rsi14:.1f} ({rsi_hint})

**波动与成交:**
- ATR(14): {atr:.4f} | 波动率: {volatility:.1f}%
- 成交量: {vol_status} | 趋势: {trend_dir} | 多周期{trend_sync}

**合约数据 (OKX):**
- 资金费率: {funding_rate:+.4f}% ({funding_hint}) | 预测下期: {next_funding_rate:+.4f}%
- 持仓量(OI): {oi_display} {coin}

**关键价位:**
- 阻力位(布林上轨): {bb_upper:.4f}
- 中轨: {bb_mid:.4f}
- 支撑位(布林下轨): {bb_lower:.4f}
- 建议止损(2ATR): {price - atr * 2:.4f}(多) / {price + atr * 2:.4f}(空)
"""
            # 添加日内数据（3分钟间隔，约10个数据点）- 数据排序: OLDEST→NEWEST
            if intraday_data and intraday_data.get('prices'):
                prompt += f"""
**日内数据 (3分钟间隔, OLDEST→NEWEST, {intraday_data.get('count', 0)}个点):**
- 价格: {intraday_data.get('prices', [])}
- EMA20: {intraday_data.get('ema20', [])}
- MACD: {intraday_data.get('macd', [])}
- RSI7: {intraday_data.get('rsi7', [])}
- RSI14: {intraday_data.get('rsi14', [])}
"""
            
            # 添加4小时数据 - 数据排序: OLDEST→NEWEST
            if h4_data and h4_data.get('prices'):
                h4_ema20_last = h4_data.get('ema20', [0])[-1] if h4_data.get('ema20') else 0
                h4_ema50_last = h4_data.get('ema50', [0])[-1] if h4_data.get('ema50') else 0
                prompt += f"""
**4小时数据 (OLDEST→NEWEST, {h4_data.get('count', 0)}个点):**
- EMA20(4h): {h4_ema20_last:.4f} | EMA50(4h): {h4_ema50_last:.4f}
- ATR3(4h): {h4_data.get('atr3', 0):.4f} | ATR14(4h): {h4_data.get('atr14', 0):.4f}
- 成交量: {h4_data.get('current_volume', 0):.0f} (平均: {h4_data.get('avg_volume', 0):.0f})
- MACD(4h): {h4_data.get('macd', [])}
- RSI14(4h): {h4_data.get('rsi14', [])}
"""
            
            prompt += """
---

"""

        # ============================================================
        # 账户信息
        # ============================================================
        num_positions = len(portfolio.get('positions', []))
        
        prompt += f"""# 账户信息与表现

**绩效指标:**
- 总收益率: {return_pct:+.2f}%
- 夏普比率: {sharpe_ratio:.2f}
- 已实现盈亏: {realized_pnl:+.2f} USD
- 未实现盈亏: {total_unrealized_pnl:+.2f} USD

**账户状态:**
- 可用资金: {cash:.2f} USD
- 账户总值: {total_value:.2f} USD
- 当前持仓数: {num_positions}/{TradingConfig.MAX_POSITIONS}

**风险敞口:**
- 保证金占用: {total_margin_used:.2f} USD ({margin_ratio:.1f}% 账户) {'✅正常' if margin_ratio < 80 else '⚠️较高' if margin_ratio < 100 else '🔴过高'}
- 多头名义敞口: {total_long_exposure:.2f} USD | 空头名义敞口: {total_short_exposure:.2f} USD
- 净敞口方向: {'多头' if net_exposure > 0 else '空头' if net_exposure < 0 else '中性'}
- 剩余可开仓: {cash:.2f} USD

"""
        # 持仓详情
        if portfolio.get('positions'):
            prompt += """**当前持仓详情:**

| 币种 | 方向 | 杠杆 | 入场价 | 现价 | 数量 | 保证金 | 未实现盈亏 | 爆仓价 | 建议 |
|------|------|------|--------|------|------|--------|------------|--------|------|
"""
            for pos in portfolio['positions']:
                coin = pos.get('coin', '')
                current_price = float(market_state.get(coin, {}).get('price', 0) or 0)
                entry_price = float(pos.get('avg_price', 0) or 0)
                quantity = float(pos.get('quantity', 0) or 0)
                leverage = int(pos.get('leverage', 1) or 1)
                side = pos.get('side', 'long')
                
                # 优先使用 OKX 返回的未实现盈亏
                pnl_usd = float(pos.get('unrealized_pnl', 0))
                pnl_ratio = float(pos.get('unrealized_pnl_ratio', 0))
                pnl_pct = pnl_ratio * 100  # 转为百分比
                
                # 如果 OKX 没有返回，则手动计算
                if pnl_usd == 0 and entry_price > 0 and current_price > 0:
                    if side == 'long':
                        pnl_pct = (current_price - entry_price) / entry_price * 100 * leverage
                        pnl_usd = (current_price - entry_price) * quantity * leverage
                    else:
                        pnl_pct = (entry_price - current_price) / entry_price * 100 * leverage
                        pnl_usd = (entry_price - current_price) * quantity * leverage
                
                # 爆仓价：优先使用 OKX 返回的值
                liq_price = float(pos.get('liq_price', 0) or 0)
                if liq_price <= 0 and entry_price > 0:
                    if side == 'long':
                        liq_price = entry_price * (1 - 0.9 / leverage)
                    else:
                        liq_price = entry_price * (1 + 0.9 / leverage)
                
                # 优先使用 OKX 返回的实际保证金
                margin = float(pos.get('margin', 0))
                if margin <= 0:
                    # 备用：从名义价值计算
                    notional_value = float(pos.get('notional_usd', 0)) or (quantity * current_price)
                    margin = notional_value / leverage if leverage > 0 else notional_value
                
                # 建议的止盈止损
                coin_atr = float(market_state.get(coin, {}).get('indicators', {}).get('atr_14', current_price * 0.02) or current_price * 0.02)
                if side == 'long':
                    suggested_tp = current_price + coin_atr * 3
                    suggested_sl = entry_price - coin_atr * 2
                else:
                    suggested_tp = current_price - coin_atr * 3
                    suggested_sl = entry_price + coin_atr * 2
                
                side_cn = "多" if side == 'long' else "空"
                
                # 操作建议
                if pnl_pct >= 8:
                    action = "🎯止盈"
                elif pnl_pct >= 5:
                    action = "部分止盈"
                elif pnl_pct <= -8:
                    action = "⚠️止损"
                elif pnl_pct <= -5:
                    action = "关注"
                else:
                    action = "持有"
                
                prompt += f"| {coin} | {side_cn} | {leverage}x | {entry_price:.2f} | {current_price:.2f} | {quantity:.4f} | ${margin:.2f} | {pnl_pct:+.1f}% (${pnl_usd:+.2f}) | {liq_price:.2f} | {action} |\n"
            
            prompt += "\n"
        else:
            prompt += "**当前持仓:** 空仓（可开新仓）\n\n"

        # ============================================================
        # 交易学习总结（基于历史交易自动优化策略）
        # ============================================================
        if TradingConfig.LEARNING_ENABLED and TradingConfig.LEARNING_INCLUDE_IN_PROMPT:
            trading_insights = self._generate_trading_insights(portfolio)
            if trading_insights:
                prompt += trading_insights
                prompt += "\n"

        # ============================================================
        # 决策要求
        # ============================================================
        prompt += f"""---

# 交易参数

| 参数 | 值 |
|------|-----|
| 单笔金额 | {min_trade_value:.0f} - {max_trade_value:.0f} USD |
| 杠杆范围 | {TradingConfig.MIN_LEVERAGE}x - {TradingConfig.MAX_LEVERAGE}x |
| 最低置信度 | {TradingConfig.MIN_CONFIDENCE_THRESHOLD} |
| 最大持仓数 | {TradingConfig.MAX_POSITIONS} |

---

请根据以上数据分析，输出JSON格式的交易决策。

**输出要求:**
1. 数值直接输出数字（如 95000.50），不要带 $ 符号
2. 每个决策必须包含: signal, confidence, quantity, leverage, profit_target, stop_loss, reasoning
3. 如果没有高质量机会，返回: {{"decisions": {{}}}}
"""

        return prompt
    
    def _call_llm(self, prompt: str) -> str:
        """Call LLM API with circuit breaker and retry logic"""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                # 使用熔断器保护API调用
                if self.provider_type in ['openai', 'azure_openai', 'deepseek']:
                    return self.circuit_breaker.call(self._call_openai_api, prompt)
                elif self.provider_type == 'anthropic':
                    return self.circuit_breaker.call(self._call_anthropic_api, prompt)
                elif self.provider_type == 'gemini':
                    return self.circuit_breaker.call(self._call_gemini_api, prompt)
                else:
                    return self.circuit_breaker.call(self._call_openai_api, prompt)
            except Exception as e:
                last_error = e
                error_type = type(e).__name__
                error_msg = str(e)
                
                # 详细记录错误信息
                self.logger.error(
                    f"API调用失败 [尝试 {attempt + 1}/{self.max_retries}]: "
                    f"类型={error_type}, 信息={error_msg}"
                )
                
                # 如果是熔断器打开的错误，不需要重试
                if "Circuit breaker" in error_msg and "is OPEN" in error_msg:
                    self.logger.warning(f"熔断器已打开，跳过重试")
                    raise
                
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (attempt + 1)
                    self.logger.info(f"等待 {wait_time}秒后重试...")
                    time.sleep(wait_time)
                else:
                    self.logger.error(
                        f"所有 {self.max_retries} 次API调用尝试都失败了。"
                        f"最后错误: {error_type}: {error_msg}"
                    )
                    raise
    
    def _call_openai_api(self, prompt: str) -> str:
        """Call OpenAI-compatible API"""
        try:
            base_url = self.api_url.rstrip('/')
            if not base_url.endswith('/v1'):
                if '/v1' in base_url:
                    base_url = base_url.split('/v1')[0] + '/v1'
                else:
                    base_url = base_url + '/v1'

            client = OpenAI(
                api_key=self.api_key,
                base_url=base_url
            )

            self.logger.info(f"Calling {self.provider_type} API with model {self.model_name}")
            
            system_prompt = self._get_system_prompt()
            
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0.7,
                max_tokens=2000
            )

            result = response.choices[0].message.content
            self.logger.info(f"API call successful, response length: {len(result)} chars")
            
            # 保存 Prompt 日志
            self._save_prompt_log(system_prompt, prompt, result)
            
            return result

        except APIConnectionError as e:
            error_msg = f"API connection failed: {str(e)}"
            self.logger.error(error_msg)
            raise Exception(error_msg)
        except APIError as e:
            error_msg = f"API error ({e.status_code}): {e.message}"
            self.logger.error(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            error_msg = f"OpenAI API call failed: {str(e)}"
            self.logger.error(error_msg)
            import traceback
            self.logger.debug(traceback.format_exc())
            raise Exception(error_msg)
    
    def _call_anthropic_api(self, prompt: str) -> str:
        """Call Anthropic Claude API"""
        try:
            base_url = self.api_url.rstrip('/')
            if not base_url.endswith('/v1'):
                base_url = base_url + '/v1'

            url = f"{base_url}/messages"
            headers = {
                'Content-Type': 'application/json',
                'x-api-key': self.api_key,
                'anthropic-version': '2023-06-01'
            }

            system_prompt = self._get_system_prompt()
            
            data = {
                "model": self.model_name,
                "max_tokens": 2000,
                "system": system_prompt,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }

            self.logger.info(f"Calling Anthropic API with model {self.model_name}")
            response = requests.post(url, headers=headers, json=data, timeout=60)
            response.raise_for_status()

            result = response.json()
            content = result['content'][0]['text']
            self.logger.info(f"API call successful, response length: {len(content)} chars")
            
            # 保存 Prompt 日志
            self._save_prompt_log(system_prompt, prompt, content)
            
            return content

        except Exception as e:
            error_msg = f"Anthropic API call failed: {str(e)}"
            self.logger.error(error_msg)
            import traceback
            self.logger.debug(traceback.format_exc())
            raise Exception(error_msg)
    
    def _call_gemini_api(self, prompt: str) -> str:
        """Call Google Gemini API"""
        try:
            base_url = self.api_url.rstrip('/')
            if not base_url.endswith('/v1'):
                base_url = base_url + '/v1'

            url = f"{base_url}/{self.model_name}:generateContent"
            headers = {
                'Content-Type': 'application/json'
            }
            params = {'key': self.api_key}

            system_prompt = self._get_system_prompt()
            
            data = {
                "contents": [
                    {
                        "parts": [
                            {
                                "text": f"{system_prompt}\n\n---\n\n{prompt}"
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 2000
                }
            }

            self.logger.info(f"Calling Gemini API with model {self.model_name}")
            response = requests.post(url, headers=headers, params=params, json=data, timeout=60)
            response.raise_for_status()

            result = response.json()
            content = result['candidates'][0]['content']['parts'][0]['text']
            self.logger.info(f"API call successful, response length: {len(content)} chars")
            
            # 保存 Prompt 日志
            self._save_prompt_log(system_prompt, prompt, content)
            
            return content

        except Exception as e:
            error_msg = f"Gemini API call failed: {str(e)}"
            self.logger.error(error_msg)
            import traceback
            self.logger.debug(traceback.format_exc())
            raise Exception(error_msg)
    
    def _parse_response(self, response: str) -> Dict:
        """Parse LLM response with CoT extraction and robust error handling"""
        if not response:
            print("[WARN] Empty response from LLM")
            return {}

        response = response.strip()

        # 提取思考过程（Chain-of-Thought）
        cot_trace = ""
        if "【思考过程】" in response or "【市场分析】" in response:
            # 提取思考部分
            parts = response.split("【JSON决策】")
            if len(parts) > 1:
                cot_trace = parts[0].strip()
                response = parts[1].strip()
                print(f"[INFO] Extracted CoT trace: {len(cot_trace)} chars")
                print(f"[CoT] {cot_trace[:200]}...")  # 显示思考过程摘要

        # Remove markdown code fences (multiple attempts)
        if '```json' in response:
            try:
                response = response.split('```json')[1].split('```')[0]
            except IndexError:
                print("[WARN] Malformed ```json fence, attempting fallback")
        elif '```' in response:
            try:
                response = response.split('```')[1].split('```')[0]
            except IndexError:
                print("[WARN] Malformed ``` fence, attempting fallback")

        # Remove common text patterns before/after JSON
        response = response.strip()

        # Find JSON object boundaries
        json_start = response.find('{')
        json_end = response.rfind('}')

        if json_start == -1 or json_end == -1:
            print(f"[ERROR] No JSON object found in response")
            print(f"[DATA] Response preview: {response[:200]}")
            return {}

        response = response[json_start:json_end+1]

        # Try to parse JSON
        try:
            full_response = json.loads(response)

            # Validate it's a dictionary
            if not isinstance(full_response, dict):
                print(f"[ERROR] Expected dict, got {type(full_response)}")
                return {}

            # 提取decisions部分（新格式）
            if 'decisions' in full_response:
                decisions = full_response['decisions']
                market_analysis = full_response.get('market_analysis', {})
                print(f"[INFO] Market Analysis: {market_analysis}")
                print(f"[INFO] Successfully parsed {len(decisions)} decisions")
                
                # 如果有CoT，添加到每个决策中
                if cot_trace:
                    for coin, decision in decisions.items():
                        if 'cot_trace' not in decision:
                            decision['cot_trace'] = cot_trace
                
                return decisions
            else:
                # 兼容旧格式（直接返回decisions）
                print(f"[INFO] Successfully parsed {len(full_response)} decisions (old format)")
                return full_response

        except json.JSONDecodeError as e:
            print(f"[ERROR] JSON parse failed: {e}")
            print(f"[DATA] Attempted to parse:\n{response[:500]}")

            # Attempt recovery: try to fix common JSON issues
            try:
                # Remove trailing commas
                import re
                response_fixed = re.sub(r',(\s*[}\]])', r'\1', response)
                full_response = json.loads(response_fixed)
                print("[INFO] Recovered from trailing comma error")
                
                # 同样处理新旧格式
                if 'decisions' in full_response:
                    return full_response['decisions']
                else:
                    return full_response
            except:
                print("[ERROR] Recovery failed, returning empty decisions")
                return {}
