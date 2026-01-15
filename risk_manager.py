"""
动态风险管理系统
Dynamic Risk Management System

使用 TradingConfig 中的配置参数
"""
import math
from typing import Dict, Tuple, Optional
from trading_config import TradingConfig


class DynamicRiskManager:
    """动态风险管理器 - 基于市场波动率和置信度调整仓位"""
    
    def __init__(self, 
                 base_risk_per_trade: float = None, 
                 max_risk_per_trade: float = None):
        """
        初始化风险管理器
        
        Args:
            base_risk_per_trade: 基础单笔风险比例（默认从配置读取）
            max_risk_per_trade: 最大单笔风险比例（默认从配置读取）
        """
        self.base_risk = base_risk_per_trade or TradingConfig.BASE_RISK_PER_TRADE
        self.max_risk = max_risk_per_trade or TradingConfig.MAX_RISK_PER_TRADE
        self.min_risk = TradingConfig.MIN_RISK_PER_TRADE
    
    def calculate_position_size(self, account_value: float, volatility: float, 
                               confidence: float, price: float) -> Tuple[float, int]:
        """
        计算最优仓位大小和杠杆
        
        Args:
            account_value: 账户总价值
            volatility: 7日年化波动率 (%)
            confidence: AI置信度 (0-1)
            price: 当前价格
            
        Returns:
            (quantity, leverage): 数量和杠杆倍数
        """
        # 1. 波动率调整因子（使用配置）
        volatility_factor = TradingConfig.get_volatility_factor(volatility)
        
        # 2. 置信度调整因子（使用配置）
        confidence_factor = confidence * TradingConfig.CONFIDENCE_MULTIPLIER
        confidence_factor = min(TradingConfig.CONFIDENCE_FACTOR_CAP, confidence_factor)
        
        # 3. 计算风险比例
        risk_per_trade = self.base_risk * volatility_factor * confidence_factor
        risk_per_trade = max(self.min_risk, min(self.max_risk, risk_per_trade))
        
        # 4. 计算仓位价值
        position_value = account_value * risk_per_trade
        
        # 5. 根据波动率和置信度决定杠杆（使用配置）
        leverage = TradingConfig.get_leverage(volatility, confidence)
        
        # 6. 计算数量
        quantity = position_value / price
        
        return quantity, leverage
    
    def calculate_stop_loss(self, entry_price: float, side: str,
                           volatility: float, atr: Optional[float] = None) -> float:
        """
        计算动态止损价格
        
        Args:
            entry_price: 入场价格
            side: 'long' 或 'short'
            volatility: 7日年化波动率 (%)
            atr: 平均真实波幅 (可选)
            
        Returns:
            止损价格
        """
        if atr and atr > 0:
            # 使用ATR（使用配置的倍数）
            stop_distance = atr * TradingConfig.STOP_LOSS_ATR_MULTIPLIER
        else:
            # 基于波动率的止损（使用配置）
            stop_pct = TradingConfig.get_stop_loss_pct(volatility)
            stop_distance = entry_price * stop_pct
        
        if side == 'long':
            stop_loss = entry_price - stop_distance
        else:
            stop_loss = entry_price + stop_distance
        
        return round(stop_loss, 2)
    
    def calculate_profit_target(self, entry_price: float, stop_loss: float,
                               side: str, risk_reward_ratio: float = None) -> float:
        """
        计算止盈目标价格
        
        Args:
            entry_price: 入场价格
            stop_loss: 止损价格
            side: 'long' 或 'short'
            risk_reward_ratio: 风险回报比（默认从配置读取）
            
        Returns:
            止盈价格
        """
        if risk_reward_ratio is None:
            risk_reward_ratio = TradingConfig.RISK_REWARD_RATIO
            
        risk = abs(entry_price - stop_loss)
        reward = risk * risk_reward_ratio
        
        if side == 'long':
            profit_target = entry_price + reward
        else:
            profit_target = entry_price - reward
        
        return round(profit_target, 2)
    
    def should_scale_out(self, entry_price: float, current_price: float,
                        profit_target: float, side: str) -> Tuple[bool, float]:
        """
        判断是否应该部分止盈
        
        Args:
            entry_price: 入场价格
            current_price: 当前价格
            profit_target: 止盈目标
            side: 'long' 或 'short'
            
        Returns:
            (should_scale, scale_percentage): 是否止盈和止盈比例
        """
        if side == 'long':
            profit_pct = (current_price - entry_price) / entry_price
            target_pct = (profit_target - entry_price) / entry_price
        else:
            profit_pct = (entry_price - current_price) / entry_price
            target_pct = (entry_price - profit_target) / entry_price
        
        progress = profit_pct / target_pct if target_pct > 0 else 0
        
        # 使用配置的部分止盈规则
        scale_pct = TradingConfig.get_scale_out_pct(progress)
        
        return (scale_pct > 0, scale_pct)


class PerformanceAnalyzer:
    """交易性能分析器"""
    
    def __init__(self, db):
        self.db = db
    
    def calculate_sharpe_ratio(self, model_id: int, days: int = None) -> float:
        """计算夏普比率"""
        if days is None:
            days = TradingConfig.SHARPE_RATIO_DAYS
            
        history = self.db.get_account_value_history(model_id, limit=days * 10)
        
        if len(history) < 2:
            return 0.0
        
        returns = []
        for i in range(len(history) - 1):
            current_value = history[i]['total_value']
            prev_value = history[i + 1]['total_value']
            
            if prev_value > 0:
                daily_return = (current_value - prev_value) / prev_value
                returns.append(daily_return)
        
        if not returns:
            return 0.0
        
        avg_return = sum(returns) / len(returns)
        
        if len(returns) < 2:
            return 0.0
        
        variance = sum((r - avg_return) ** 2 for r in returns) / (len(returns) - 1)
        std_return = math.sqrt(variance)
        
        if std_return == 0:
            return 0.0
        
        sharpe = (avg_return / std_return) * math.sqrt(365)
        
        return round(sharpe, 2)
    
    def calculate_max_drawdown(self, model_id: int) -> float:
        """计算最大回撤"""
        history = self.db.get_account_value_history(
            model_id, 
            limit=TradingConfig.MAX_DRAWDOWN_HISTORY
        )
        
        if len(history) < 2:
            return 0.0
        
        history.reverse()
        
        values = [h['total_value'] for h in history]
        peak = values[0]
        max_dd = 0.0
        
        for value in values:
            if value > peak:
                peak = value
            
            drawdown = (peak - value) / peak if peak > 0 else 0
            
            if drawdown > max_dd:
                max_dd = drawdown
        
        return round(max_dd, 4)
    
    def calculate_win_rate(self, model_id: int) -> float:
        """计算胜率"""
        trades = self.db.get_trades(
            model_id, 
            limit=TradingConfig.WIN_RATE_TRADE_LIMIT
        )
        
        closed_trades = [t for t in trades if t['signal'] == 'close_position']
        
        if not closed_trades:
            return 0.0
        
        winning_trades = sum(1 for t in closed_trades if t['pnl'] > 0)
        
        return round(winning_trades / len(closed_trades), 4)
    
    def calculate_profit_factor(self, model_id: int) -> float:
        """计算盈利因子"""
        trades = self.db.get_trades(
            model_id, 
            limit=TradingConfig.WIN_RATE_TRADE_LIMIT
        )
        
        closed_trades = [t for t in trades if t['signal'] == 'close_position']
        
        if not closed_trades:
            return 0.0
        
        total_profit = sum(t['pnl'] for t in closed_trades if t['pnl'] > 0)
        total_loss = abs(sum(t['pnl'] for t in closed_trades if t['pnl'] < 0))
        
        if total_loss == 0:
            return float('inf') if total_profit > 0 else 0.0
        
        return round(total_profit / total_loss, 2)
    
    def calculate_long_short_performance(self, model_id: int) -> Dict:
        """分析做多和做空的表现"""
        trades = self.db.get_trades(
            model_id, 
            limit=TradingConfig.WIN_RATE_TRADE_LIMIT
        )
        
        closed_trades = [t for t in trades if t['signal'] == 'close_position']
        
        long_trades = [t for t in closed_trades if t['side'] == 'long']
        short_trades = [t for t in closed_trades if t['side'] == 'short']
        
        def calc_stats(trades_list):
            if not trades_list:
                return {'count': 0, 'win_rate': 0, 'avg_pnl': 0, 'total_pnl': 0}
            
            winning = sum(1 for t in trades_list if t['pnl'] > 0)
            total_pnl = sum(t['pnl'] for t in trades_list)
            
            return {
                'count': len(trades_list),
                'win_rate': round(winning / len(trades_list), 4),
                'avg_pnl': round(total_pnl / len(trades_list), 2),
                'total_pnl': round(total_pnl, 2)
            }
        
        return {
            'long': calc_stats(long_trades),
            'short': calc_stats(short_trades),
            'total_trades': len(closed_trades)
        }
    
    def get_performance_metrics(self, model_id: int) -> Dict:
        """获取综合性能指标"""
        long_short_perf = self.calculate_long_short_performance(model_id)
        
        return {
            'sharpe_ratio': self.calculate_sharpe_ratio(model_id),
            'max_drawdown': self.calculate_max_drawdown(model_id),
            'win_rate': self.calculate_win_rate(model_id),
            'profit_factor': self.calculate_profit_factor(model_id),
            'long_performance': long_short_perf['long'],
            'short_performance': long_short_perf['short']
        }
