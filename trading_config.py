"""
交易系统配置加载器
Trading System Configuration Loader

从 config.yaml 读取配置，支持环境变量覆盖
"""
import os
import yaml
from pathlib import Path


def _load_yaml_config() -> dict:
    """加载 YAML 配置文件"""
    config_path = Path(__file__).parent / 'config.yaml'
    
    if not config_path.exists():
        print(f"[WARN] config.yaml not found, using default values")
        return {}
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[ERROR] Failed to load config.yaml: {e}")
        return {}


# 加载配置
_config = _load_yaml_config()


def _get(section: str, key: str, default=None, env_var: str = None):
    """获取配置值，支持环境变量覆盖"""
    # 优先使用环境变量
    if env_var and os.getenv(env_var):
        value = os.getenv(env_var)
        # 尝试转换类型
        if isinstance(default, bool):
            return value.lower() in ('true', '1', 'yes')
        elif isinstance(default, int):
            return int(value)
        elif isinstance(default, float):
            return float(value)
        return value
    
    # 从 YAML 配置读取
    section_config = _config.get(section, {})
    if isinstance(section_config, dict):
        return section_config.get(key, default)
    return default


class TradingConfig:
    """交易系统配置 - 从 config.yaml 加载"""
    
    # ============================================================
    # OKX 交易所配置
    # ============================================================
    ENABLE_REAL_TRADING = _get('okx', 'enable_real_trading', True)
    OKX_API_KEY = os.getenv('OKX_API_KEY', _get('okx', 'api_key', ''))
    OKX_SECRET_KEY = os.getenv('OKX_SECRET_KEY', _get('okx', 'secret_key', ''))
    OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE', _get('okx', 'passphrase', ''))
    
    OKX_API_URL = os.getenv('OKX_API_URL', _get('okx', 'api_url', 'https://www.okx.com'))
    OKX_API_URL_BACKUP = os.getenv('OKX_API_URL_BACKUP', _get('okx', 'api_url_backup', 'https://aws.okx.com'))
    OKX_USE_BACKUP_URL = os.getenv('OKX_USE_BACKUP_URL', str(_get('okx', 'use_backup_url', False))).lower() == 'true'
    OKX_AUTO_SWITCH_URL = _get('okx', 'auto_switch_url', True)
    
    # SSL配置 - 网络问题时可设置为False禁用SSL验证（紧急模式）
    OKX_SSL_VERIFY = os.getenv('OKX_SSL_VERIFY', str(_get('okx', 'ssl_verify', True))).lower() != 'false'
    
    OKX_DEMO_TRADING = _get('okx', 'demo_trading', False)
    OKX_MARGIN_MODE = _get('okx', 'margin_mode', 'isolated')
    OKX_INST_TYPE = _get('okx', 'inst_type', 'SWAP')
    OKX_INST_SUFFIX = _get('okx', 'inst_suffix', '-USDT-SWAP')
    
    # ============================================================
    # 核心交易参数
    # ============================================================
    TRADING_CYCLE_SECONDS = _get('trading', 'cycle_seconds', 900)
    COOLDOWN_PERIOD_SECONDS = _get('trading', 'cooldown_seconds', 2700)
    TRADING_COINS = _get('trading', 'coins', ['BTC', 'ETH', 'BNB', 'XRP', 'DOGE'])
    
    # ============================================================
    # AI 决策配置
    # ============================================================
    MIN_CONFIDENCE_THRESHOLD = _get('ai', 'min_confidence', 0.80)
    MAX_POSITIONS = _get('ai', 'max_positions', 2)
    MAX_NEW_POSITIONS_PER_CYCLE = _get('ai', 'max_new_positions_per_cycle', 1)
    
    # ============================================================
    # K线数据配置
    # ============================================================
    _kline = _config.get('trading', {}).get('kline', {})
    KLINE_INTRADAY_LIMIT = _kline.get('intraday_limit', 15) if isinstance(_kline, dict) else 15
    KLINE_H4_LIMIT = _kline.get('h4_limit', 12) if isinstance(_kline, dict) else 12
    KLINE_DAILY_LIMIT = _kline.get('daily_limit', 10) if isinstance(_kline, dict) else 10
    
    # ============================================================
    # 交易学习配置
    # ============================================================
    _learning = _config.get('learning', {})
    LEARNING_ENABLED = _learning.get('enabled', True) if isinstance(_learning, dict) else True
    LEARNING_HISTORY_LIMIT = _learning.get('history_trades_limit', 50) if isinstance(_learning, dict) else 50
    LEARNING_UPDATE_FREQUENCY = _learning.get('update_frequency', 5) if isinstance(_learning, dict) else 5
    LEARNING_INCLUDE_IN_PROMPT = _learning.get('include_in_prompt', True) if isinstance(_learning, dict) else True
    LEARNING_MIN_TRADES = _learning.get('min_trades_for_summary', 10) if isinstance(_learning, dict) else 10
    
    # ============================================================
    # 杠杆配置
    # ============================================================
    DEFAULT_LEVERAGE = _get('leverage', 'default', 3)
    MAX_LEVERAGE = _get('leverage', 'max', 5)
    MIN_LEVERAGE = _get('leverage', 'min', 1)
    
    # 杠杆规则：[最大波动率, 最低置信度, 杠杆]
    _leverage_rules = _get('leverage', 'rules', [[30, 0.75, 5], [50, 0.70, 4], [80, 0.65, 3], [999, 0.0, 2]])
    LEVERAGE_RULES = [(r[0], r[1], r[2]) for r in _leverage_rules]
    
    # ============================================================
    # 风险控制
    # ============================================================
    BASE_RISK_PER_TRADE = _get('risk', 'base_risk_per_trade', 0.08)
    MAX_RISK_PER_TRADE = _get('risk', 'max_risk_per_trade', 0.15)
    MIN_RISK_PER_TRADE = _get('risk', 'min_risk_per_trade', 0.05)
    
    MIN_TRADE_VALUE_USD = _get('risk', 'min_trade_value_usd', 20)
    MAX_TRADE_VALUE_PCT = _get('risk', 'max_trade_value_pct', 0.40)
    MAX_VOLATILITY_THRESHOLD = _get('risk', 'max_volatility_threshold', 80)
    RISK_REWARD_RATIO = _get('risk', 'risk_reward_ratio', 2.0)
    
    # 波动率因子：[最大波动率, 因子]
    _volatility_factors = _get('risk', 'volatility_factors', [[30, 1.2], [50, 1.0], [80, 0.8], [999, 0.6]])
    VOLATILITY_FACTORS = [(v[0], v[1]) for v in _volatility_factors]
    
    # ============================================================
    # 止盈止损
    # ============================================================
    ENABLE_AUTO_TAKE_PROFIT = _get('take_profit', 'enabled', True)
    QUICK_PROFIT_THRESHOLD_PCT = _get('take_profit', 'quick_profit_threshold', 0.10)
    ENABLE_QUICK_PROFIT_EXIT = _get('take_profit', 'enable_quick_exit', True)
    
    # 阶梯止盈规则
    _tp_rules = _get('take_profit', 'rules', [[0.08, 1.0, "盈利8%全平"], [0.05, 0.50, "盈利5%平半仓"], [0.03, 0.30, "盈利3%平30%"]])
    AUTO_TAKE_PROFIT_RULES = [(r[0], r[1], r[2]) for r in _tp_rules]
    
    # 部分止盈规则
    _scale_out = _get('take_profit', 'scale_out_rules', [[1.0, 0.50], [0.8, 0.30], [0.6, 0.20]])
    SCALE_OUT_RULES = [(r[0], r[1]) for r in _scale_out]
    
    DEFAULT_STOP_LOSS_PCT = _get('stop_loss', 'default_pct', 0.08)
    MAX_STOP_LOSS_PCT = _get('stop_loss', 'max_pct', 0.12)
    STOP_LOSS_ATR_MULTIPLIER = _get('stop_loss', 'atr_multiplier', 2.5)
    
    # 止损规则：[最大波动率, 止损比例]
    _sl_rules = _get('stop_loss', 'rules', [[30, 0.04], [50, 0.05], [80, 0.06], [999, 0.08]])
    STOP_LOSS_PCT_RULES = [(r[0], r[1]) for r in _sl_rules]
    
    # ============================================================
    # 安全保护
    # ============================================================
    MAX_DAILY_LOSS_PCT = _get('safety', 'max_daily_loss_pct', 0.10)
    MAX_TOTAL_LOSS_PCT = _get('safety', 'max_total_loss_pct', 0.15)
    MAX_DAILY_TRADES = _get('safety', 'max_daily_trades', 50)
    EMERGENCY_STOP = _get('safety', 'emergency_stop', False)
    
    # ============================================================
    # API 配置
    # ============================================================
    API_MAX_RETRIES = _get('api', 'max_retries', 3)
    API_TIMEOUT = _get('api', 'timeout', 90)
    API_RETRY_DELAY = _get('api', 'retry_delay', 2)
    
    OKX_CONNECT_TIMEOUT = _get('api', 'okx_connect_timeout', 20)
    OKX_READ_TIMEOUT = _get('api', 'okx_read_timeout', 45)
    OKX_API_TIMEOUT = API_TIMEOUT
    OKX_MAX_RETRIES = _get('api', 'okx_max_retries', 5)
    OKX_RETRY_DELAY = _get('api', 'okx_retry_delay', 3)
    OKX_SSL_RETRY_RECREATE = _get('api', 'okx_ssl_retry_recreate', 2)
    
    _cb = _get('api', 'circuit_breaker', {})
    CIRCUIT_BREAKER_FAILURE_THRESHOLD = _cb.get('failure_threshold', 8) if isinstance(_cb, dict) else 8
    CIRCUIT_BREAKER_TIMEOUT = _cb.get('timeout', 30) if isinstance(_cb, dict) else 30
    
    # ============================================================
    # 费用配置
    # ============================================================
    TRADE_FEE_RATE = _get('fees', 'trade_fee_rate', 0.0008)
    MAX_SLIPPAGE = _get('fees', 'max_slippage', 0.003)
    CASH_BUFFER_RATIO = _get('fees', 'cash_buffer_ratio', 1.02)
    
    # ============================================================
    # 其他配置
    # ============================================================
    ENABLE_SHORT_SELLING = _get('misc', 'enable_short_selling', True)
    BALANCED_LONG_SHORT = _get('misc', 'balanced_long_short', True)
    DEFAULT_PROFIT_TARGET_PCT = _get('misc', 'default_profit_target_pct', 0.10)
    CONFIDENCE_MULTIPLIER = _get('misc', 'confidence_multiplier', 1.2)
    CONFIDENCE_FACTOR_CAP = _get('misc', 'confidence_factor_cap', 1.5)
    MIN_TREND_ALIGNMENT = _get('misc', 'min_trend_alignment', 0.5)
    SHARPE_RATIO_DAYS = _get('misc', 'sharpe_ratio_days', 30)
    MAX_DRAWDOWN_HISTORY = _get('misc', 'max_drawdown_history', 1000)
    WIN_RATE_TRADE_LIMIT = _get('misc', 'win_rate_trade_limit', 1000)
    
    # RSI 阈值
    RSI_OVERSOLD_THRESHOLD = _get('rsi', 'oversold', 40)
    RSI_OVERBOUGHT_THRESHOLD = _get('rsi', 'overbought', 60)
    RSI_STRONG_OVERSOLD = _get('rsi', 'strong_oversold', 35)
    RSI_STRONG_OVERBOUGHT = _get('rsi', 'strong_overbought', 70)
    
    # RSI趋势模式
    _rsi_trend = _config.get('rsi', {}).get('trend_mode', {})
    RSI_TREND_MODE_ENABLED = _rsi_trend.get('enabled', True) if isinstance(_rsi_trend, dict) else True
    RSI_OVERBOUGHT_IN_UPTREND = _rsi_trend.get('overbought_in_uptrend', 80) if isinstance(_rsi_trend, dict) else 80
    RSI_OVERSOLD_IN_DOWNTREND = _rsi_trend.get('oversold_in_downtrend', 25) if isinstance(_rsi_trend, dict) else 25
    
    # ============================================================
    # 动态置信度配置
    # ============================================================
    _dynamic_conf = _config.get('ai', {}).get('dynamic_confidence', {})
    DYNAMIC_CONFIDENCE_ENABLED = _dynamic_conf.get('enabled', True) if isinstance(_dynamic_conf, dict) else True
    CONFIDENCE_LOW_VOLATILITY = _dynamic_conf.get('low_volatility_threshold', 0.70) if isinstance(_dynamic_conf, dict) else 0.70  # 低波动时降低阈值
    CONFIDENCE_NORMAL = _dynamic_conf.get('normal_threshold', 0.75) if isinstance(_dynamic_conf, dict) else 0.75
    CONFIDENCE_HIGH_VOLATILITY = _dynamic_conf.get('high_volatility_threshold', 0.80) if isinstance(_dynamic_conf, dict) else 0.80  # 高波动时提高阈值
    VOLATILITY_BOUNDARY = _dynamic_conf.get('volatility_boundary', 50) if isinstance(_dynamic_conf, dict) else 50
    ALLOW_POSITION_SWAP = _get('ai', 'allow_position_swap', True)
    
    # ============================================================
    # 成交量确认配置
    # ============================================================
    _volume = _config.get('volume', {})
    VOLUME_CONFIRM_ENABLED = _volume.get('enabled', True) if isinstance(_volume, dict) else True
    VOLUME_SHRINK_PENALTY = _volume.get('shrink_penalty', 0.05) if isinstance(_volume, dict) else 0.05  # 降低缩量惩罚，避免过度过滤
    VOLUME_BREAKOUT_BONUS = _volume.get('breakout_bonus', 0.08) if isinstance(_volume, dict) else 0.08  # 同步调整放量奖励
    VOLUME_SHRINK_THRESHOLD = _volume.get('shrink_threshold', 0.6) if isinstance(_volume, dict) else 0.6
    VOLUME_BREAKOUT_THRESHOLD = _volume.get('breakout_threshold', 1.5) if isinstance(_volume, dict) else 1.5
    
    # ============================================================
    # 市场情绪过滤器配置
    # ============================================================
    _sentiment = _config.get('sentiment', {})
    SENTIMENT_FILTER_ENABLED = _sentiment.get('enabled', True) if isinstance(_sentiment, dict) else True
    EXTREME_FEAR_THRESHOLD = _sentiment.get('extreme_fear_threshold', 25) if isinstance(_sentiment, dict) else 25
    EXTREME_FEAR_ACTION = _sentiment.get('extreme_fear_action', 'hold') if isinstance(_sentiment, dict) else 'hold'
    EXTREME_GREED_THRESHOLD = _sentiment.get('extreme_greed_threshold', 70) if isinstance(_sentiment, dict) else 70
    EXTREME_GREED_ACTION = _sentiment.get('extreme_greed_action', 'prefer_short') if isinstance(_sentiment, dict) else 'prefer_short'
    EXTREME_CONFIDENCE_PENALTY = _sentiment.get('extreme_confidence_penalty', 0.05) if isinstance(_sentiment, dict) else 0.05
    
    # ============================================================
    # 做空激励配置
    # ============================================================
    _short_selling = _config.get('short_selling', {})
    SHORT_SELLING_ENABLED = _short_selling.get('enabled', True) if isinstance(_short_selling, dict) else True
    SHORT_CONFIDENCE_BOOST = _short_selling.get('confidence_boost', 0.05) if isinstance(_short_selling, dict) else 0.05
    SHORT_DOWNTREND_BOOST = _short_selling.get('downtrend_boost', 0.08) if isinstance(_short_selling, dict) else 0.08
    SHORT_RISK_REWARD_RATIO = _short_selling.get('risk_reward_ratio', 1.8) if isinstance(_short_selling, dict) else 1.8
    
    # ============================================================
    # K线形态识别配置
    # ============================================================
    _pattern = _config.get('pattern_recognition', {})
    PATTERN_RECOGNITION_ENABLED = _pattern.get('enabled', True) if isinstance(_pattern, dict) else True
    PATTERN_TIMEFRAME = _pattern.get('timeframe', '15m') if isinstance(_pattern, dict) else '15m'
    PATTERN_CANDLE_COUNT = _pattern.get('candle_count', 5) if isinstance(_pattern, dict) else 5
    PATTERN_MAX_ADJUSTMENT = _pattern.get('max_confidence_adjustment', 0.15) if isinstance(_pattern, dict) else 0.15
    PATTERN_MIN_STRENGTH = _pattern.get('min_pattern_strength', 0.3) if isinstance(_pattern, dict) else 0.3
    
    # ============================================================
    # 持仓时间管理配置
    # ============================================================
    _pos_time = _config.get('position_time', {})
    POSITION_TIME_ENABLED = _pos_time.get('enabled', True) if isinstance(_pos_time, dict) else True
    TRAILING_TRIGGER_HOURS = _pos_time.get('trailing_trigger_hours', 8) if isinstance(_pos_time, dict) else 8
    TRAILING_DISTANCE_PCT = _pos_time.get('trailing_distance_pct', 0.02) if isinstance(_pos_time, dict) else 0.02
    MAX_HOLDING_HOURS = _pos_time.get('max_holding_hours', 48) if isinstance(_pos_time, dict) else 48
    SIDEWAYS_THRESHOLD_PCT = _pos_time.get('sideways_threshold_pct', 0.02) if isinstance(_pos_time, dict) else 0.02
    SIDEWAYS_HOURS = _pos_time.get('sideways_hours', 12) if isinstance(_pos_time, dict) else 12
    SIDEWAYS_ACTION = _pos_time.get('sideways_action', 'tighten_stop') if isinstance(_pos_time, dict) else 'tighten_stop'
    
    # ============================================================
    # 交易质量评分配置
    # ============================================================
    _quality = _config.get('quality_score', {})
    QUALITY_SCORE_ENABLED = _quality.get('enabled', True) if isinstance(_quality, dict) else True
    MIN_QUALITY_SCORE = _quality.get('min_score', 55) if isinstance(_quality, dict) else 55
    _quality_weights = _quality.get('weights', {}) if isinstance(_quality, dict) else {}
    QUALITY_SCORE_WEIGHTS = {
        'confidence': _quality_weights.get('confidence', 35),
        'trend_alignment': _quality_weights.get('trend_alignment', 25),
        'momentum': _quality_weights.get('momentum', 15),
        'volatility': _quality_weights.get('volatility', 10),
        'risk_reward': _quality_weights.get('risk_reward', 10),
        'volume': _quality_weights.get('volume', 5)
    }
    
    # ============================================================
    # 兼容性参数（保留旧代码兼容）
    # ============================================================
    MIN_TRADE_VALUE_PCT = 0.08
    PROMPT_MIN_TRADE_PCT = 0.20  # 单笔建议最小比例提高到20%（充分利用资金）
    MIN_TRADE_QUALITY_SCORE = 0
    QUALITY_SCORE_WEIGHTS = {'confidence': 40, 'trend_alignment': 20, 'momentum': 15, 'volatility': 15, 'risk_reward': 10}
    MAX_DAILY_OPEN_POSITIONS = 20
    REAL_TRADING_CONSERVATIVE = False
    REQUIRE_ORDER_CONFIRMATION = False
    ENABLE_TRAILING_STOP = False
    TRAILING_STOP_TRIGGER_PCT = 0.04
    TRAILING_STOP_DISTANCE_PCT = 0.02
    REAL_MAX_LEVERAGE = MAX_LEVERAGE
    REAL_BASE_RISK_PER_TRADE = BASE_RISK_PER_TRADE
    REAL_MAX_RISK_PER_TRADE = MAX_RISK_PER_TRADE
    REAL_MIN_CONFIDENCE_THRESHOLD = 0.70
    REAL_MAX_POSITIONS = MAX_POSITIONS
    
    # ============================================================
    # 工具方法
    # ============================================================
    @classmethod
    def get_trading_cycle_minutes(cls) -> int:
        return cls.TRADING_CYCLE_SECONDS // 60
    
    @classmethod
    def get_cooldown_period_minutes(cls) -> int:
        return cls.COOLDOWN_PERIOD_SECONDS // 60
    
    @classmethod
    def get_volatility_factor(cls, volatility: float) -> float:
        for max_vol, factor in cls.VOLATILITY_FACTORS:
            if volatility < max_vol:
                return factor
        return 0.6
    
    @classmethod
    def get_leverage(cls, volatility: float, confidence: float) -> int:
        for max_vol, min_conf, leverage in cls.LEVERAGE_RULES:
            if volatility < max_vol and confidence > min_conf:
                return min(leverage, cls.MAX_LEVERAGE)
        return cls.MIN_LEVERAGE
    
    @classmethod
    def get_stop_loss_pct(cls, volatility: float) -> float:
        for max_vol, stop_pct in cls.STOP_LOSS_PCT_RULES:
            if volatility < max_vol:
                return stop_pct
        return cls.MAX_STOP_LOSS_PCT
    
    @classmethod
    def get_scale_out_pct(cls, progress: float) -> float:
        for threshold, close_pct in cls.SCALE_OUT_RULES:
            if progress >= threshold:
                return close_pct
        return 0.0
    
    @classmethod
    def get_dynamic_confidence_threshold(cls, volatility: float) -> float:
        """根据波动率动态调整置信度阈值
        
        逻辑：高波动率需要更高的置信度（更谨慎），低波动率可以降低阈值
        """
        if not cls.DYNAMIC_CONFIDENCE_ENABLED:
            return cls.MIN_CONFIDENCE_THRESHOLD
        
        if volatility < 30:
            # 低波动率：市场稳定，可以降低置信度要求
            return cls.CONFIDENCE_LOW_VOLATILITY  # 0.70
        elif volatility < cls.VOLATILITY_BOUNDARY:
            # 正常波动率
            return cls.CONFIDENCE_NORMAL  # 0.75
        else:
            # 高波动率：需要更高置信度
            return cls.CONFIDENCE_HIGH_VOLATILITY  # 0.80
    
    @classmethod
    def get_rsi_threshold(cls, is_uptrend: bool, threshold_type: str) -> float:
        """
        根据趋势动态调整RSI阈值
        
        Args:
            is_uptrend: 是否上升趋势
            threshold_type: 'overbought' 或 'oversold'
        """
        if not cls.RSI_TREND_MODE_ENABLED:
            if threshold_type == 'overbought':
                return cls.RSI_OVERBOUGHT_THRESHOLD
            else:
                return cls.RSI_OVERSOLD_THRESHOLD
        
        if threshold_type == 'overbought':
            return cls.RSI_OVERBOUGHT_IN_UPTREND if is_uptrend else cls.RSI_OVERBOUGHT_THRESHOLD
        else:
            return cls.RSI_OVERSOLD_IN_DOWNTREND if not is_uptrend else cls.RSI_OVERSOLD_THRESHOLD
    
    @classmethod
    def get_volume_adjustment(cls, current_volume: float, avg_volume: float) -> float:
        """
        根据成交量返回置信度调整值
        
        Returns:
            正值表示加分，负值表示减分
        """
        if not cls.VOLUME_CONFIRM_ENABLED or avg_volume <= 0:
            return 0.0
        
        ratio = current_volume / avg_volume
        
        if ratio < cls.VOLUME_SHRINK_THRESHOLD:
            return -cls.VOLUME_SHRINK_PENALTY  # 缩量惩罚
        elif ratio > cls.VOLUME_BREAKOUT_THRESHOLD:
            return cls.VOLUME_BREAKOUT_BONUS  # 放量加分
        else:
            return 0.0
    
    @classmethod
    def get_sentiment_adjustment(cls, fng_index: float) -> tuple:
        """
        根据恐惧贪婪指数返回调整策略
        
        Returns:
            (action: str, confidence_penalty: float)
        """
        if not cls.SENTIMENT_FILTER_ENABLED:
            return ('normal', 0.0)
        
        if fng_index < cls.EXTREME_FEAR_THRESHOLD:
            return (cls.EXTREME_FEAR_ACTION, cls.EXTREME_CONFIDENCE_PENALTY)
        elif fng_index > cls.EXTREME_GREED_THRESHOLD:
            return (cls.EXTREME_GREED_ACTION, cls.EXTREME_CONFIDENCE_PENALTY)
        else:
            return ('normal', 0.0)
    
    @classmethod
    def get_effective_max_leverage(cls) -> int:
        return cls.MAX_LEVERAGE
    
    @classmethod
    def get_effective_risk_per_trade(cls) -> tuple:
        return cls.BASE_RISK_PER_TRADE, cls.MAX_RISK_PER_TRADE
    
    @classmethod
    def get_effective_confidence_threshold(cls) -> float:
        return cls.MIN_CONFIDENCE_THRESHOLD
    
    @classmethod
    def get_effective_max_positions(cls) -> int:
        return cls.MAX_POSITIONS
    
    @classmethod
    def is_trading_allowed(cls) -> tuple:
        if cls.EMERGENCY_STOP:
            return False, "紧急停止"
        return True, "正常"
    
    @classmethod
    def reload(cls):
        """重新加载配置文件"""
        global _config
        _config = _load_yaml_config()
        print("[CONFIG] Configuration reloaded from config.yaml")
    
    @classmethod
    def summary(cls) -> str:
        return f"""
╔══════════════════════════════════════════════════════════╗
║              AITradeGame 量化交易系统                    ║
╠══════════════════════════════════════════════════════════╣
║ 【交易参数】                                             ║
║   交易币种: {', '.join(cls.TRADING_COINS):<40}║
║   交易周期: {cls.get_trading_cycle_minutes()}分钟                                     ║
║   冷却期: {cls.get_cooldown_period_minutes()}分钟                                       ║
║   最大持仓: {cls.MAX_POSITIONS}个                                       ║
║   置信度阈值: {cls.MIN_CONFIDENCE_THRESHOLD:.0%}                                    ║
╠══════════════════════════════════════════════════════════╣
║ 【风险控制】                                             ║
║   杠杆范围: {cls.MIN_LEVERAGE}-{cls.MAX_LEVERAGE}x                                      ║
║   默认止损: {cls.DEFAULT_STOP_LOSS_PCT:.0%}                                      ║
║   日亏损上限: {cls.MAX_DAILY_LOSS_PCT:.0%}                                    ║
╠══════════════════════════════════════════════════════════╣
║ 【止盈规则】                                             ║
║   3%盈利 → 平30%                                        ║
║   5%盈利 → 平50%                                        ║
║   8%盈利 → 全平                                         ║
╠══════════════════════════════════════════════════════════╣
║ 【系统状态】                                             ║
║   真实交易: {'是' if cls.ENABLE_REAL_TRADING else '否':<41}║
║   紧急停止: {'是' if cls.EMERGENCY_STOP else '否':<41}║
║   API URL: {cls.OKX_API_URL:<42}║
╚══════════════════════════════════════════════════════════╝
"""


if __name__ == '__main__':
    print(TradingConfig.summary())
