"""
OKX 交易所适配器
OKX Exchange Adapter

实现与 OKX 交易所的 API 对接，支持真实交易
文档: https://www.okx.com/docs-v5/zh/
"""
import hmac
import base64
import hashlib
import json
import time
import logging
import threading
import ssl
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from trading_config import TradingConfig

logger = logging.getLogger(__name__)

# 缓存锁，用于线程安全
_cache_lock = threading.Lock()


class TLSAdapter(HTTPAdapter):
    """
    自定义TLS适配器，解决SSL/TLS握手问题
    - 支持自定义SSL上下文
    - 更好的连接池管理
    - 支持禁用SSL验证（紧急模式）
    """
    
    def __init__(self, ssl_verify: bool = True, **kwargs):
        self.ssl_verify = ssl_verify
        super().__init__(**kwargs)
    
    def init_poolmanager(self, *args, **kwargs):
        try:
            # 创建SSL上下文
            ctx = ssl.create_default_context()
            
            if not self.ssl_verify:
                # 紧急模式：禁用SSL验证（仅用于网络问题调试）
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                logger.warning("SSL验证已禁用（紧急模式）")
            else:
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
            
            # 设置更宽松的TLS选项
            ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
            # 支持TLS 1.2和1.3
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            
            kwargs['ssl_context'] = ctx
        except Exception as e:
            logger.debug(f"使用默认SSL上下文: {e}")
        return super().init_poolmanager(*args, **kwargs)


class OKXExchange:
    """OKX 交易所适配器 - 基于 OKX V5 API
    
    文档: https://www.okx.com/docs-v5/zh/
    
    主要功能:
    - 账户余额查询 (GET /api/v5/account/balance)
    - 持仓信息查询 (GET /api/v5/account/positions)
    - 下单/撤单 (POST /api/v5/trade/order)
    - K线行情数据 (GET /api/v5/market/candles)
    - 止损止盈策略订单 (POST /api/v5/trade/order-algo)
    """
    
    # API 版本
    API_VERSION = 'v5'
    
    # 限速配置 (根据官方文档)
    RATE_LIMITS = {
        'account_balance': '10/2s',
        'account_positions': '10/2s',
        'trade_order': '60/2s',
        'trade_order_algo': '20/2s',
        'market_candles': '40/2s',
    }
    
    # OKX 常见错误码 (用于智能重试)
    ERROR_CODES = {
        # 系统错误（可重试）
        '50000': '系统繁忙',
        '50001': '系统维护中',
        '50004': '接口请求超时',
        '50011': '用户请求频率过高',
        '50013': '系统繁忙，请稍后重试',
        '50026': '系统错误',
        # 签名错误（不可重试，需检查配置）
        '50111': 'API Key无效',
        '50113': 'timestamp无效',
        '50114': '签名无效',
        # 资金/账户错误
        '51000': '保证金不足',
        '51001': '可用余额不足',
        '51008': '持仓不存在',
        '51020': '订单数量超出限制',
        '51127': '账户模式不支持该操作',
        # 订单错误
        '51400': '撤单失败，订单不存在',
        '51401': '撤单失败，订单已完成',
        '51402': '撤单失败，订单已撤销',
        '51403': '撤单失败，订单已被系统撤销',
    }
    
    # 可重试的错误码
    RETRYABLE_ERROR_CODES = {'50000', '50001', '50004', '50011', '50013', '50026'}
    
    def __init__(self, 
                 api_key: str = None, 
                 secret_key: str = None, 
                 passphrase: str = None,
                 demo_trading: bool = None):
        """
        初始化 OKX 交易所适配器
        
        Args:
            api_key: API Key（可选，默认从配置读取）
            secret_key: Secret Key（可选，默认从配置读取）
            passphrase: Passphrase（可选，默认从配置读取）
            demo_trading: 是否使用模拟盘（可选，默认从配置读取）
        """
        self.api_key = api_key or TradingConfig.OKX_API_KEY
        self.secret_key = secret_key or TradingConfig.OKX_SECRET_KEY
        self.passphrase = passphrase or TradingConfig.OKX_PASSPHRASE
        self.demo_trading = demo_trading if demo_trading is not None else TradingConfig.OKX_DEMO_TRADING
        
        # URL 配置 - 支持多端点切换
        # OKX 提供多个区域API端点
        self.api_endpoints = [
            'https://www.okx.com',      # 主站
            'https://aws.okx.com',       # AWS区域
            'https://okx.com',           # 简短域名
        ]
        self.primary_url = TradingConfig.OKX_API_URL
        self.backup_url = getattr(TradingConfig, 'OKX_API_URL_BACKUP', 'https://aws.okx.com')
        self.use_backup = getattr(TradingConfig, 'OKX_USE_BACKUP_URL', False)
        self.auto_switch = getattr(TradingConfig, 'OKX_AUTO_SWITCH_URL', True)
        self.base_url = self.backup_url if self.use_backup else self.primary_url
        self._current_endpoint_index = 0
        
        # SSL配置 - 紧急模式可禁用SSL验证
        self.ssl_verify = getattr(TradingConfig, 'OKX_SSL_VERIFY', True)
        
        # 超时配置
        self.connect_timeout = getattr(TradingConfig, 'OKX_CONNECT_TIMEOUT', 20)
        self.read_timeout = getattr(TradingConfig, 'OKX_READ_TIMEOUT', 45)
        self.max_retries = getattr(TradingConfig, 'OKX_MAX_RETRIES', 5)
        self.retry_delay = getattr(TradingConfig, 'OKX_RETRY_DELAY', 3)
        
        # 交易时效性配置 (expTime，毫秒)
        self.order_expire_ms = 30000  # 订单30秒内有效
        
        # 缓存配置 - 用于优雅降级
        self._balance_cache = None
        self._balance_cache_time = 0
        self._positions_cache = []
        self._positions_cache_time = 0
        self._cache_ttl = 60  # 缓存有效期60秒
        
        # 连接状态追踪
        self._consecutive_failures = 0
        self._last_success_time = 0
        
        # 验证配置
        if not all([self.api_key, self.secret_key, self.passphrase]):
            logger.warning("OKX API 配置不完整，请检查 trading_config.py 或环境变量")
        
        # 创建 Session 并配置连接池
        self.session = self._create_session()
        
        # 模拟盘标志
        if self.demo_trading:
            self.session.headers['x-simulated-trading'] = '1'
        
        logger.info(f"OKX 交易所适配器初始化完成 (模拟盘: {self.demo_trading}, URL: {self.base_url})")
    
    def _create_session(self) -> requests.Session:
        """创建配置好的Session"""
        session = requests.Session()
        
        # 配置带自定义TLS的适配器（传入SSL验证配置）
        tls_adapter = TLSAdapter(
            ssl_verify=self.ssl_verify,
            pool_connections=50,
            pool_maxsize=100,
            max_retries=Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=['GET', 'POST']
            )
        )
        
        session.mount('https://', tls_adapter)
        session.mount('http://', HTTPAdapter(
            pool_connections=50,
            pool_maxsize=100
        ))
        
        session.headers.update({
            'Content-Type': 'application/json',
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'User-Agent': 'AITradeGame/1.0',
        })
        
        # 如果禁用SSL验证，也需要设置session的verify属性
        if not self.ssl_verify:
            session.verify = False
        
        return session
    
    def _recreate_session(self, disable_ssl_verify: bool = False):
        """重建Session连接，用于SSL错误恢复
        
        Args:
            disable_ssl_verify: 是否禁用SSL验证（紧急模式）
        """
        try:
            self.session.close()
        except:
            pass
        
        # 如果指定禁用SSL验证，更新配置
        if disable_ssl_verify and self.ssl_verify:
            logger.warning("启用紧急模式：禁用SSL证书验证")
            self.ssl_verify = False
        
        self.session = self._create_session()
        if self.demo_trading:
            self.session.headers['x-simulated-trading'] = '1'
        logger.info(f"已重建API Session连接 (SSL验证: {self.ssl_verify})")
    
    def _switch_to_next_endpoint(self) -> bool:
        """切换到下一个API端点
        
        Returns:
            是否还有更多端点可尝试
        """
        self._current_endpoint_index += 1
        if self._current_endpoint_index < len(self.api_endpoints):
            self.base_url = self.api_endpoints[self._current_endpoint_index]
            logger.info(f"切换到API端点: {self.base_url}")
            return True
        else:
            # 重置索引，下次从头开始
            self._current_endpoint_index = 0
            self.base_url = self.api_endpoints[0]
            return False
    
    def _get_timestamp(self) -> str:
        """获取 ISO 格式时间戳
        
        OKX API 要求时间戳格式: 2020-12-08T09:08:57.715Z
        """
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    
    def _get_expire_time(self, offset_ms: int = None) -> str:
        """获取订单有效截止时间（Unix毫秒时间戳）
        
        用于交易时效性控制，防止网络延迟导致的过期订单执行
        文档: https://www.okx.com/docs-v5/zh/#trading-time-validity
        
        Args:
            offset_ms: 有效期偏移量（毫秒），默认使用 order_expire_ms
            
        Returns:
            Unix毫秒时间戳字符串
        """
        offset = offset_ms or self.order_expire_ms
        expire_ts = int((time.time() * 1000) + offset)
        return str(expire_ts)
    
    def _sign(self, timestamp: str, method: str, request_path: str, body: str = '') -> str:
        """
        生成 API 签名
        
        签名算法 (来自官方文档):
        1. 将 timestamp + method + requestPath + body 拼接成字符串
        2. 使用 HMAC SHA256 加密，密钥为 SecretKey
        3. 对结果进行 Base64 编码
        
        注意:
        - GET 请求的参数算作 requestPath，不算 body
        - POST 请求的 body 为 JSON 字符串
        
        Args:
            timestamp: ISO 时间戳 (OK-ACCESS-TIMESTAMP)
            method: HTTP 方法 (GET/POST)，必须大写
            request_path: 请求路径，如 /api/v5/account/balance?ccy=BTC
            body: 请求体（JSON 字符串），GET 请求为空
            
        Returns:
            Base64 编码的签名字符串
        """
        # 拼接待签名字符串
        prehash_string = timestamp + method.upper() + request_path + body
        
        # HMAC SHA256 签名
        mac = hmac.new(
            self.secret_key.encode('utf-8'),
            prehash_string.encode('utf-8'),
            hashlib.sha256
        )
        
        # Base64 编码
        return base64.b64encode(mac.digest()).decode('utf-8')
    
    def _switch_url(self):
        """切换到备用 URL"""
        if self.base_url == self.primary_url:
            self.base_url = self.backup_url
            logger.info(f"OKX API 切换到备用 URL: {self.backup_url}")
        else:
            self.base_url = self.primary_url
            logger.info(f"OKX API 切换回主 URL: {self.primary_url}")
    
    def _request(self, method: str, endpoint: str, params: dict = None, data: dict = None, 
                 use_cache: bool = False, cache_key: str = None, extra_headers: dict = None) -> dict:
        """
        发送 API 请求（带自动重试和 URL 切换）
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            params: URL 参数
            data: POST 请求体
            use_cache: 是否使用缓存（超时时降级）
            cache_key: 缓存键名
            extra_headers: 额外的请求头（如 expTime 用于交易时效性）
            
        Returns:
            API 响应
        """
        timeout = (self.connect_timeout, self.read_timeout)
        last_error = None
        tried_urls = set()
        ssl_error_count = 0
        
        # 最多尝试 max_retries 次，包括主备 URL 切换
        for attempt in range(self.max_retries):
            url = self.base_url + endpoint
            timestamp = self._get_timestamp()
            
            # 构建请求路径
            request_path = endpoint
            if params:
                query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
                request_path = f"{endpoint}?{query_string}"
            
            # 构建请求体
            body = ''
            if data:
                body = json.dumps(data)
            
            # 生成签名
            sign = self._sign(timestamp, method.upper(), request_path, body)
            
            headers = {
                'OK-ACCESS-SIGN': sign,
                'OK-ACCESS-TIMESTAMP': timestamp,
            }
            
            # 添加额外请求头（如 expTime）
            if extra_headers:
                headers.update(extra_headers)
            
            try:
                if method.upper() == 'GET':
                    response = self.session.get(
                        url, 
                        params=params, 
                        headers=headers, 
                        timeout=timeout
                    )
                else:
                    response = self.session.post(
                        url, 
                        json=data, 
                        headers=headers, 
                        timeout=timeout
                    )
                
                # 检查HTTP状态码
                response.raise_for_status()
                
                result = response.json()
                
                if result.get('code') != '0':
                    error_code = result.get('code', '')
                    error_msg = result.get('msg', 'Unknown error')
                    
                    # 获取错误码的中文描述（如果有）
                    error_desc = self.ERROR_CODES.get(error_code, '')
                    if error_desc:
                        error_msg = f"{error_msg} ({error_desc})"
                    
                    logger.error(f"OKX API 错误: {error_msg} (code: {error_code})")
                    
                    # 检查是否可重试的错误码
                    if error_code in self.RETRYABLE_ERROR_CODES and attempt < self.max_retries - 1:
                        wait_time = self.retry_delay * (attempt + 1)
                        logger.info(f"可重试错误码 {error_code}，等待 {wait_time}秒后重试...")
                        time.sleep(wait_time)
                        continue
                    
                    return {'success': False, 'error': error_msg, 'code': error_code, 'data': []}
                
                # 成功 - 重置失败计数
                self._consecutive_failures = 0
                self._last_success_time = time.time()
                
                return {'success': True, 'data': result.get('data', []), 'code': '0'}
            
            except requests.exceptions.SSLError as e:
                ssl_error_count += 1
                last_error = f'SSLError: {str(e)}'
                self._consecutive_failures += 1
                
                logger.warning(f"OKX API SSLError (尝试 {attempt + 1}/{self.max_retries}), URL: {self.base_url}, 端点: {endpoint}")
                
                # 记录已尝试的 URL
                tried_urls.add(self.base_url)
                
                # 策略1: 尝试切换到下一个端点
                if self.auto_switch and self._switch_to_next_endpoint():
                    logger.info(f"SSL错误，切换到下一个端点: {self.base_url}")
                
                # 策略2: SSL错误次数过多时，重建Session
                if ssl_error_count >= 2:
                    # 如果已经尝试了所有端点还是失败，考虑禁用SSL验证
                    if len(tried_urls) >= len(self.api_endpoints) and self.ssl_verify:
                        logger.warning("所有端点SSL都失败，尝试禁用SSL验证（紧急模式）")
                        self._recreate_session(disable_ssl_verify=True)
                    else:
                        self._recreate_session()
                    logger.info("SSL错误次数过多，已重建Session")
                
                # 等待后重试
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (attempt + 1)
                    logger.info(f"等待 {wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, 
                    requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                error_type = type(e).__name__
                last_error = f'{error_type}: {str(e)}'
                self._consecutive_failures += 1
                
                logger.warning(f"OKX API {error_type} (尝试 {attempt + 1}/{self.max_retries}), URL: {self.base_url}, 端点: {endpoint}")
                
                # 记录已尝试的 URL
                tried_urls.add(self.base_url)
                
                # 尝试切换到下一个端点
                if self.auto_switch:
                    self._switch_to_next_endpoint()
                    logger.info(f"连接错误，切换到端点: {self.base_url}")
                
                # 等待后重试
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (attempt + 1)
                    logger.info(f"等待 {wait_time}秒后重试...")
                    time.sleep(wait_time)
                    continue
                    
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                logger.error(f"OKX API 请求失败: {e}")
                break
            except json.JSONDecodeError as e:
                last_error = 'JSON decode error'
                logger.error(f"OKX API 响应解析失败: {e}")
                break
        
        # 所有重试都失败了
        logger.error(f"OKX API 请求最终失败: {last_error}, 连续失败次数: {self._consecutive_failures}")
        return {'success': False, 'error': last_error or 'Request failed', 'data': [], 'from_cache': False}
    
    # ============================================================
    # 账户相关 API
    # ============================================================
    
    def get_account_balance(self, use_cache_on_fail: bool = True) -> Dict:
        """
        获取账户余额（带重试机制和缓存降级）
        
        Args:
            use_cache_on_fail: 失败时是否使用缓存数据
            
        Returns:
            账户余额信息
        """
        result = self._request('GET', '/api/v5/account/balance')
        
        # 请求失败时，尝试使用缓存
        if not result.get('success'):
            if use_cache_on_fail and self._balance_cache:
                cache_age = time.time() - self._balance_cache_time
                logger.warning(f"OKX 余额查询失败，使用 {cache_age:.0f}秒前的缓存数据")
                cached_result = self._balance_cache.copy()
                cached_result['from_cache'] = True
                cached_result['cache_age'] = cache_age
                return cached_result
            return {'success': False, 'error': result.get('error', '获取余额失败'), 'total_equity': 0, 'available_balance': 0}
        
        data = result['data']
        if not data:
            return {'success': True, 'total_equity': 0, 'available_balance': 0, 'details': []}
        
        account = data[0]
        details = account.get('details', [])
        
        # 计算总可用余额（所有币种的 availBal 相加，或取 USDT 的可用余额）
        available_balance = 0
        usdt_available = 0
        for detail in details:
            ccy = detail.get('ccy', '')
            avail = float(detail.get('availBal', 0) or 0)
            avail_eq = float(detail.get('availEq', 0) or 0)  # 可用权益（美元计价）
            
            if ccy == 'USDT':
                usdt_available = avail
            
            # 累加所有币种的可用权益
            if avail_eq > 0:
                available_balance += avail_eq
        
        # 如果没有 availEq，使用 USDT 可用余额
        if available_balance == 0:
            available_balance = usdt_available
        
        # 总权益
        total_equity = float(account.get('totalEq', 0) or 0)
        
        # 如果总权益为0，尝试从 details 计算
        if total_equity == 0:
            for detail in details:
                eq = float(detail.get('eq', 0) or 0)  # 币种权益
                total_equity += eq
        
        # 计算实际冻结的保证金（从 frozenBal 获取）
        # 在全仓模式下，frozenBal 是实际被冻结用于保证金的金额
        frozen_margin = 0
        for detail in details:
            ccy = detail.get('ccy', '')
            frozen = float(detail.get('frozenBal', 0) or 0)
            if ccy == 'USDT' and frozen > 0:
                frozen_margin = frozen
                break
        
        # 如果 USDT 没有冻结金额，使用 totalEq - availBal 计算
        if frozen_margin == 0:
            frozen_margin = max(0, total_equity - available_balance)
        
        balance_data = {
            'success': True,
            'total_equity': total_equity,
            'available_balance': available_balance,
            'frozen_margin': frozen_margin,  # 新增：实际冻结的保证金
            'details': details,
            'from_cache': False
        }
        
        # 更新缓存
        with _cache_lock:
            self._balance_cache = balance_data.copy()
            self._balance_cache_time = time.time()
        
        logger.info(f"OKX 账户余额: 总权益=${total_equity:.2f}, 可用=${available_balance:.2f}, 冻结保证金=${frozen_margin:.2f}")
        
        return balance_data
    
    def get_positions(self, inst_type: str = None, use_cache_on_fail: bool = True) -> List[Dict]:
        """
        获取持仓信息（带重试机制和缓存降级）
        
        Args:
            inst_type: 产品类型 (SPOT/SWAP/FUTURES)
            use_cache_on_fail: 失败时是否使用缓存数据
            
        Returns:
            持仓列表
        """
        params = {}
        if inst_type:
            params['instType'] = inst_type
        else:
            params['instType'] = TradingConfig.OKX_INST_TYPE
        
        result = self._request('GET', '/api/v5/account/positions', params=params)
        
        # 请求失败时，尝试使用缓存
        if not result.get('success'):
            if use_cache_on_fail and self._positions_cache:
                cache_age = time.time() - self._positions_cache_time
                logger.warning(f"OKX 持仓查询失败，使用 {cache_age:.0f}秒前的缓存数据 ({len(self._positions_cache)} 个持仓)")
                return self._positions_cache.copy()
            logger.warning(f"获取持仓失败: {result.get('error', '未知错误')}")
            return []
        
        positions = []
        for pos in result['data']:
            if float(pos.get('pos', 0)) != 0:  # 只返回有持仓的
                # pos 是合约张数，ctVal 是每张合约面值（币数）
                # 实际持仓数量 = 张数 × 面值
                contract_size = float(pos.get('pos', 0))
                ct_val = float(pos.get('ctVal', 1))  # 合约面值，如 BTC 是 0.01
                actual_quantity = abs(contract_size * ct_val)
                
                # OKX 返回的 notionalUsd 是名义价值（美元）
                # margin 是实际占用的保证金
                notional_usd = float(pos.get('notionalUsd', 0) or 0)
                # 尝试多个可能的保证金字段
                margin_from_okx = float(pos.get('margin', 0) or 0) or float(pos.get('imr', 0) or 0) or float(pos.get('mmr', 0) or 0)
                
                # 如果 OKX 没返回保证金，从名义价值计算
                leverage = int(float(pos.get('lever', 1) or 1))
                if margin_from_okx <= 0 and notional_usd > 0:
                    margin_from_okx = notional_usd / leverage if leverage > 0 else notional_usd
                
                # 调试日志
                logger.debug(f"[OKX Position] {pos['instId']}: pos={contract_size}, ctVal={ct_val}, "
                           f"notionalUsd={pos.get('notionalUsd')}, margin={pos.get('margin')}, "
                           f"imr={pos.get('imr')}, lever={leverage}, calculated_margin={margin_from_okx:.2f}")
                
                positions.append({
                    'inst_id': pos['instId'],
                    'coin': pos['instId'].split('-')[0],  # BTC-USDT-SWAP -> BTC
                    'side': 'long' if pos['posSide'] == 'long' else 'short',
                    'quantity': actual_quantity,  # 实际币数量
                    'contract_size': abs(contract_size),  # 合约张数
                    'ct_val': ct_val,  # 合约面值
                    'avg_price': float(pos.get('avgPx', 0)),
                    'leverage': leverage,
                    'unrealized_pnl': float(pos.get('upl', 0)),
                    'unrealized_pnl_ratio': float(pos.get('uplRatio', 0)),
                    'margin': margin_from_okx,  # OKX 返回的实际保证金
                    'notional_usd': notional_usd,  # 名义价值
                    'liq_price': float(pos.get('liqPx', 0)) if pos.get('liqPx') else None,
                })
        
        # 更新缓存
        with _cache_lock:
            self._positions_cache = positions.copy()
            self._positions_cache_time = time.time()
        
        return positions
    
    # ============================================================
    # 交易相关 API
    # ============================================================
    
    def set_leverage(self, inst_id: str, leverage: int, margin_mode: str = None, pos_side: str = None) -> bool:
        """
        设置杠杆倍数
        
        Args:
            inst_id: 产品ID（如 BTC-USDT-SWAP）
            leverage: 杠杆倍数
            margin_mode: 保证金模式 (isolated/cross)
            pos_side: 持仓方向（双向持仓模式下需要）
            
        Returns:
            是否成功
        """
        data = {
            'instId': inst_id,
            'lever': str(leverage),
            'mgnMode': margin_mode or TradingConfig.OKX_MARGIN_MODE,
        }
        
        if pos_side:
            data['posSide'] = pos_side
        
        result = self._request('POST', '/api/v5/account/set-leverage', data=data)
        
        if result['success']:
            logger.info(f"杠杆设置成功: {inst_id} -> {leverage}x")
        else:
            logger.error(f"杠杆设置失败: {result['error']}")
        
        return result['success']
    
    def place_order(self, 
                   coin: str, 
                   side: str, 
                   quantity: float, 
                   leverage: int = None,
                   order_type: str = 'market',
                   price: float = None,
                   stop_loss: float = None,
                   take_profit: float = None,
                   reduce_only: bool = False,
                   use_expire_time: bool = True) -> Dict:
        """
        下单
        文档: POST /api/v5/trade/order
        限速: 60次/2s (按 User ID + Instrument ID)
        
        Args:
            coin: 币种（如 BTC）
            side: 交易方向 ('buy_to_enter', 'sell_to_enter', 'close_long', 'close_short')
            quantity: 数量（张数，OKX合约以张为单位）
            leverage: 杠杆倍数
            order_type: 订单类型 ('market' 或 'limit')
            price: 限价单价格
            stop_loss: 止损价格
            take_profit: 止盈价格
            reduce_only: 是否只减仓
            use_expire_time: 是否使用订单有效期 (防止网络延迟导致的过期订单)
            
        Returns:
            订单结果
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        
        # 确定交易方向和持仓方向
        if side == 'buy_to_enter':
            order_side = 'buy'
            pos_side = 'long'
        elif side == 'sell_to_enter':
            order_side = 'sell'
            pos_side = 'short'
        elif side == 'close_long':
            order_side = 'sell'
            pos_side = 'long'
        elif side == 'close_short':
            order_side = 'buy'
            pos_side = 'short'
        else:
            return {'success': False, 'error': f'Invalid side: {side}'}
        
        # 设置杠杆（需要在确定 pos_side 之后）
        if leverage:
            self.set_leverage(inst_id, leverage, pos_side=pos_side)
        
        # 构建订单数据
        data = {
            'instId': inst_id,
            'tdMode': TradingConfig.OKX_MARGIN_MODE,  # 保证金模式
            'side': order_side,
            'posSide': pos_side,
            'ordType': order_type,
            'sz': str(quantity),  # 合约张数（支持小数，如 0.31）
        }
        
        # 只减仓模式
        if reduce_only:
            data['reduceOnly'] = True
        
        # 限价单需要价格
        if order_type == 'limit' and price:
            data['px'] = str(price)
        
        # 注意：OKX 普通订单不支持直接设置止损止盈
        # 止损止盈需要使用策略订单 API (/api/v5/trade/order-algo)
        # 这里先下普通订单，止损止盈由交易员手动或后续设置
        if stop_loss or take_profit:
            logger.info(f"止损止盈将通过策略订单单独设置: SL={stop_loss}, TP={take_profit}")
        
        # 添加交易有效期 (expTime) 防止网络延迟导致过期订单执行
        extra_headers = {}
        if use_expire_time:
            extra_headers['expTime'] = self._get_expire_time()
            logger.debug(f"订单有效期设置: expTime={extra_headers['expTime']}")
        
        result = self._request('POST', '/api/v5/trade/order', data=data, extra_headers=extra_headers)
        
        if result['success'] and result['data']:
            order_data = result['data'][0]
            order_id = order_data.get('ordId')
            logger.info(f"下单成功: {inst_id} {side} {quantity}张, 订单ID: {order_id}")
            return {
                'success': True,
                'order_id': order_id,
                'inst_id': inst_id,
                'side': side,
                'quantity': quantity
            }
        else:
            error_msg = result.get('error', 'Unknown error')
            logger.error(f"下单失败: {inst_id} {side} - {error_msg}")
            return {'success': False, 'error': error_msg}
    
    def set_stop_loss_take_profit(self, coin: str, pos_side: str, 
                                   stop_loss: float = None, take_profit: float = None) -> Dict:
        """
        设置止损止盈（策略订单）
        
        Args:
            coin: 币种
            pos_side: 持仓方向 ('long' 或 'short')
            stop_loss: 止损价格
            take_profit: 止盈价格
            
        Returns:
            设置结果
        """
        if not stop_loss and not take_profit:
            return {'success': True, 'message': '无止损止盈需要设置'}
        
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        results = []
        
        # 获取当前持仓数量
        positions = self.get_positions()
        pos_sz = 0
        for pos in positions:
            if pos['coin'] == coin and pos['side'] == pos_side:
                pos_sz = pos['quantity']
                break
        
        if pos_sz <= 0:
            logger.warning(f"未找到 {coin} {pos_side} 持仓，无法设置止损止盈")
            return {'success': False, 'error': '未找到持仓'}
        
        logger.info(f"设置止损止盈: {coin} {pos_side} 持仓={pos_sz}张, SL=${stop_loss}, TP=${take_profit}")
        
        # 设置止损
        if stop_loss:
            sl_data = {
                'instId': inst_id,
                'tdMode': TradingConfig.OKX_MARGIN_MODE,
                'side': 'sell' if pos_side == 'long' else 'buy',
                'posSide': pos_side,
                'ordType': 'conditional',
                'sz': str(pos_sz),
                'slTriggerPx': str(round(stop_loss, 2)),
                'slOrdPx': '-1',  # 市价
            }
            logger.info(f"止损订单数据: {sl_data}")
            sl_result = self._request('POST', '/api/v5/trade/order-algo', data=sl_data)
            if sl_result['success']:
                logger.info(f"止损设置成功: {coin} @ ${stop_loss}")
                results.append({'type': 'stop_loss', 'success': True})
            else:
                logger.error(f"止损设置失败: {sl_result.get('error')}")
                results.append({'type': 'stop_loss', 'success': False, 'error': sl_result.get('error')})
        
        # 设置止盈
        if take_profit:
            tp_data = {
                'instId': inst_id,
                'tdMode': TradingConfig.OKX_MARGIN_MODE,
                'side': 'sell' if pos_side == 'long' else 'buy',
                'posSide': pos_side,
                'ordType': 'conditional',
                'sz': str(pos_sz),
                'tpTriggerPx': str(round(take_profit, 2)),
                'tpOrdPx': '-1',  # 市价
            }
            logger.info(f"止盈订单数据: {tp_data}")
            tp_result = self._request('POST', '/api/v5/trade/order-algo', data=tp_data)
            if tp_result['success']:
                logger.info(f"止盈设置成功: {coin} @ ${take_profit}")
                results.append({'type': 'take_profit', 'success': True})
            else:
                logger.error(f"止盈设置失败: {tp_result.get('error')}")
                results.append({'type': 'take_profit', 'success': False, 'error': tp_result.get('error')})
        
        return {
            'success': all(r['success'] for r in results),
            'results': results
        }
    
    def close_position(self, coin: str, side: str) -> Dict:
        """
        平仓
        
        Args:
            coin: 币种
            side: 持仓方向 ('long' 或 'short')
            
        Returns:
            平仓结果
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        
        data = {
            'instId': inst_id,
            'mgnMode': TradingConfig.OKX_MARGIN_MODE,
            'posSide': side,
        }
        
        result = self._request('POST', '/api/v5/trade/close-position', data=data)
        
        if result['success']:
            logger.info(f"平仓成功: {inst_id} {side}")
            return {'success': True, 'inst_id': inst_id, 'side': side}
        else:
            logger.error(f"平仓失败: {inst_id} {side} - {result['error']}")
            return {'success': False, 'error': result['error']}
    
    def close_all_positions(self) -> List[Dict]:
        """
        一键平仓所有持仓
        
        Returns:
            平仓结果列表
        """
        positions = self.get_positions()
        results = []
        
        for pos in positions:
            result = self.close_position(pos['coin'], pos['side'])
            result['coin'] = pos['coin']
            result['quantity'] = pos['quantity']
            result['unrealized_pnl'] = pos['unrealized_pnl']
            results.append(result)
        
        return results
    
    def cancel_order(self, inst_id: str, order_id: str) -> bool:
        """
        撤销订单
        
        Args:
            inst_id: 产品ID
            order_id: 订单ID
            
        Returns:
            是否成功
        """
        data = {
            'instId': inst_id,
            'ordId': order_id,
        }
        
        result = self._request('POST', '/api/v5/trade/cancel-order', data=data)
        
        return result['success']
    
    def get_order(self, inst_id: str, order_id: str) -> Dict:
        """
        查询订单详情
        
        Args:
            inst_id: 产品ID
            order_id: 订单ID
            
        Returns:
            订单详情
        """
        params = {
            'instId': inst_id,
            'ordId': order_id,
        }
        
        result = self._request('GET', '/api/v5/trade/order', params=params)
        
        if result['success'] and result['data']:
            return result['data'][0]
        return None
    
    # ============================================================
    # 市场数据 API
    # ============================================================
    
    def get_ticker(self, coin: str) -> Dict:
        """
        获取单个币种的行情
        
        Args:
            coin: 币种
            
        Returns:
            行情数据
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {'instId': inst_id}
        
        result = self._request('GET', '/api/v5/market/ticker', params=params)
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'coin': coin,
                'inst_id': inst_id,
                'price': float(data.get('last', 0)),
                'bid': float(data.get('bidPx', 0)),
                'ask': float(data.get('askPx', 0)),
                'volume_24h': float(data.get('vol24h', 0)),
                'change_24h': float(data.get('sodUtc0', 0)),  # 今日开盘价
                'high_24h': float(data.get('high24h', 0)),
                'low_24h': float(data.get('low24h', 0)),
            }
        return None
    
    def get_tickers(self, coins: List[str] = None) -> Dict[str, Dict]:
        """
        获取多个币种的行情
        
        Args:
            coins: 币种列表（默认使用配置中的交易币种）
            
        Returns:
            行情数据字典
        """
        if coins is None:
            coins = TradingConfig.TRADING_COINS
        
        tickers = {}
        for coin in coins:
            ticker = self.get_ticker(coin)
            if ticker:
                tickers[coin] = ticker
        
        return tickers
    
    def get_kline(self, coin: str, bar: str = '1H', limit: int = 100) -> List[Dict]:
        """
        获取 K 线数据
        
        Args:
            coin: 币种
            bar: K 线周期 (1m/5m/15m/30m/1H/4H/1D 等)
            limit: 数量限制
            
        Returns:
            K 线数据列表
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {
            'instId': inst_id,
            'bar': bar,
            'limit': str(limit),
        }
        
        result = self._request('GET', '/api/v5/market/candles', params=params)
        
        if result['success'] and result['data']:
            klines = []
            for k in result['data']:
                klines.append({
                    'timestamp': int(k[0]),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                    'volume_ccy': float(k[6]),
                })
            return klines
        return []
    
    # ============================================================
    # 合约信息 API
    # ============================================================
    
    def get_instrument(self, coin: str) -> Dict:
        """
        获取合约信息（包括合约乘数、最小下单量等）
        
        Args:
            coin: 币种
            
        Returns:
            合约信息
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {
            'instType': TradingConfig.OKX_INST_TYPE,
            'instId': inst_id,
        }
        
        result = self._request('GET', '/api/v5/public/instruments', params=params)
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'inst_id': data['instId'],
                'coin': coin,
                'ct_val': float(data.get('ctVal', 1)),  # 合约乘数
                'ct_mult': float(data.get('ctMult', 1)),  # 合约乘数
                'min_sz': float(data.get('minSz', 1)),  # 最小下单数量
                'lot_sz': float(data.get('lotSz', 1)),  # 下单数量精度
                'tick_sz': float(data.get('tickSz', 0.01)),  # 价格精度
                'lever': int(data.get('lever', 1)),  # 最大杠杆
            }
        return None
    
    def calculate_contract_size(self, coin: str, usdt_amount: float, price: float) -> float:
        """
        计算合约张数（支持小数）
        
        OKX 永续合约以张为单位，每张的价值取决于合约乘数
        例如 BTC-USDT-SWAP 每张 = 0.01 BTC, 最小下单 0.01 张
        
        Args:
            coin: 币种
            usdt_amount: USDT 金额
            price: 当前价格
            
        Returns:
            合约张数（按 lot_sz 精度对齐）
        """
        instrument = self.get_instrument(coin)
        if not instrument:
            logger.warning(f"无法获取 {coin} 合约信息，使用默认值")
            ct_val = 0.01 if coin == 'BTC' else 0.1
            min_sz = 0.01
            lot_sz = 0.01
        else:
            ct_val = instrument['ct_val']
            min_sz = instrument['min_sz']
            lot_sz = instrument['lot_sz']
        
        # 每张合约价值 = 合约乘数 * 当前价格
        contract_value = ct_val * price
        
        # 张数 = USDT金额 / 每张价值
        contracts = usdt_amount / contract_value
        
        # 按 lot_sz 精度向下取整
        if lot_sz > 0:
            contracts = int(contracts / lot_sz) * lot_sz
        
        # 确保不低于最小下单量
        return max(min_sz, contracts)
    
    def get_contract_value(self, coin: str, price: float) -> float:
        """
        获取单张合约价值（USDT）
        
        Args:
            coin: 币种
            price: 当前价格
            
        Returns:
            单张合约价值
        """
        instrument = self.get_instrument(coin)
        if not instrument:
            ct_val = 0.01 if coin == 'BTC' else (0.1 if coin == 'ETH' else 1.0)
        else:
            ct_val = instrument['ct_val']
        
        return ct_val * price
    
    # ============================================================
    # 辅助方法
    # ============================================================
    
    def test_connection(self, try_backup: bool = True) -> bool:
        """
        测试 API 连接（支持自动尝试备用 URL）
        
        Args:
            try_backup: 主 URL 失败时是否尝试备用 URL
            
        Returns:
            是否连接成功
        """
        try:
            # 首先尝试当前 URL
            result = self._request('GET', '/api/v5/account/balance')
            if result['success']:
                logger.info(f"OKX API 连接测试成功 (URL: {self.base_url})")
                return True
            
            # 如果当前 URL 失败且允许尝试备用
            if try_backup and self.auto_switch:
                other_url = self.backup_url if self.base_url == self.primary_url else self.primary_url
                logger.info(f"主 URL 连接失败，尝试备用 URL: {other_url}")
                
                # 临时切换 URL
                original_url = self.base_url
                self.base_url = other_url
                
                result = self._request('GET', '/api/v5/account/balance')
                if result['success']:
                    logger.info(f"OKX API 连接测试成功 (备用 URL: {self.base_url})")
                    # 保持使用成功的 URL
                    return True
                else:
                    # 恢复原 URL
                    self.base_url = original_url
            
            logger.error(f"OKX API 连接测试失败: {result.get('error', '未知错误')}")
            return False
        except Exception as e:
            logger.error(f"OKX API 连接测试异常: {e}")
            return False
    
    def get_connection_status(self) -> Dict:
        """
        获取连接状态信息
        
        Returns:
            连接状态详情
        """
        return {
            'current_url': self.base_url,
            'primary_url': self.primary_url,
            'backup_url': self.backup_url,
            'consecutive_failures': self._consecutive_failures,
            'last_success_time': self._last_success_time,
            'balance_cache_age': time.time() - self._balance_cache_time if self._balance_cache else None,
            'positions_cache_age': time.time() - self._positions_cache_time if self._positions_cache else None,
            'has_cached_data': bool(self._balance_cache or self._positions_cache)
        }
    
    def get_account_config(self) -> Dict:
        """
        获取账户配置
        文档: GET /api/v5/account/config
        限速: 5次/2s
        
        Returns:
            账户配置信息
        """
        result = self._request('GET', '/api/v5/account/config')
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'uid': data.get('uid'),
                'acct_lv': data.get('acctLv'),  # 账户层级 1:现货 2:合约 3:跨币种 4:组合保证金
                'pos_mode': data.get('posMode'),  # 持仓模式 long_short_mode/net_mode
                'auto_loan': data.get('autoLoan'),  # 是否自动借币
                'greeks_type': data.get('greeksType'),  # Greeks 类型 PA/BS
                'level': data.get('level'),  # VIP等级
                'margin_mode': data.get('mgnIsoMode'),  # 逐仓保证金模式
            }
        return None
    
    def get_trade_fee(self, inst_type: str = 'SWAP', inst_id: str = None) -> Dict:
        """
        获取当前账户交易手续费费率
        文档: GET /api/v5/account/trade-fee
        限速: 5次/2s
        
        Args:
            inst_type: 产品类型 SPOT/MARGIN/SWAP/FUTURES/OPTION
            inst_id: 产品ID (可选)
            
        Returns:
            手续费率信息
        """
        params = {'instType': inst_type}
        if inst_id:
            params['instId'] = inst_id
        
        result = self._request('GET', '/api/v5/account/trade-fee', params=params)
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'category': data.get('category'),
                'maker_rate': float(data.get('maker', 0)),  # Maker费率
                'taker_rate': float(data.get('taker', 0)),  # Taker费率
                'maker_u': float(data.get('makerU', 0)),    # USDT保证金Maker
                'taker_u': float(data.get('takerU', 0)),    # USDT保证金Taker
            }
        return None
    
    def get_max_avail_size(self, coin: str, td_mode: str = None) -> Dict:
        """
        获取最大可用余额/保证金
        文档: GET /api/v5/account/max-avail-size
        限速: 20次/2s
        
        Args:
            coin: 币种
            td_mode: 交易模式 isolated/cross/cash
            
        Returns:
            最大可用信息
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {
            'instId': inst_id,
            'tdMode': td_mode or TradingConfig.OKX_MARGIN_MODE,
        }
        
        result = self._request('GET', '/api/v5/account/max-avail-size', params=params)
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'inst_id': data.get('instId'),
                'avail_buy': float(data.get('availBuy', 0)),  # 最大可买
                'avail_sell': float(data.get('availSell', 0)),  # 最大可卖
            }
        return None
    
    def get_max_size(self, coin: str, td_mode: str = None, leverage: int = None) -> Dict:
        """
        获取最大可下单数量
        文档: GET /api/v5/account/max-size
        限速: 20次/2s
        
        Args:
            coin: 币种
            td_mode: 交易模式
            leverage: 杠杆倍数 (可选)
            
        Returns:
            最大可下单数量
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {
            'instId': inst_id,
            'tdMode': td_mode or TradingConfig.OKX_MARGIN_MODE,
        }
        if leverage:
            params['lever'] = str(leverage)
        
        result = self._request('GET', '/api/v5/account/max-size', params=params)
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'inst_id': data.get('instId'),
                'max_buy': float(data.get('maxBuy', 0)),   # 最大可买张数
                'max_sell': float(data.get('maxSell', 0)),  # 最大可卖张数
            }
        return None
    
    def get_leverage_info(self, coin: str, margin_mode: str = None) -> Dict:
        """
        获取杠杆倍数
        文档: GET /api/v5/account/leverage-info
        限速: 20次/2s
        
        Args:
            coin: 币种
            margin_mode: 保证金模式 isolated/cross
            
        Returns:
            杠杆信息
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {
            'instId': inst_id,
            'mgnMode': margin_mode or TradingConfig.OKX_MARGIN_MODE,
        }
        
        result = self._request('GET', '/api/v5/account/leverage-info', params=params)
        
        if result['success'] and result['data']:
            levers = {}
            for data in result['data']:
                pos_side = data.get('posSide', 'net')
                levers[pos_side] = int(float(data.get('lever', 1)))
            return levers
        return None
    
    def get_position_risk(self, inst_type: str = 'SWAP') -> Dict:
        """
        查看账户持仓风险
        文档: GET /api/v5/account/account-position-risk
        限速: 10次/2s
        
        Args:
            inst_type: 产品类型
            
        Returns:
            持仓风险信息
        """
        params = {'instType': inst_type}
        result = self._request('GET', '/api/v5/account/account-position-risk', params=params)
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'adj_eq': float(data.get('adjEq', 0)),  # 有效保证金
                'ts': data.get('ts'),
                'bal_data': data.get('balData', []),  # 币种资产
                'pos_data': data.get('posData', []),  # 持仓详情
            }
        return None
    
    def batch_orders(self, orders: List[Dict]) -> List[Dict]:
        """
        批量下单
        文档: POST /api/v5/trade/batch-orders
        限速: 300/2s (使用批量可减少限速消耗)
        
        Args:
            orders: 订单列表，每个订单包含 instId, tdMode, side, ordType, sz 等
            
        Returns:
            批量下单结果
        """
        result = self._request('POST', '/api/v5/trade/batch-orders', data=orders)
        
        if result['success']:
            return result['data']
        return []
    
    def get_orders_pending(self, inst_type: str = 'SWAP', inst_id: str = None) -> List[Dict]:
        """
        获取未成交订单列表
        文档: GET /api/v5/trade/orders-pending
        限速: 60次/2s
        
        Args:
            inst_type: 产品类型
            inst_id: 产品ID (可选)
            
        Returns:
            未成交订单列表
        """
        params = {'instType': inst_type}
        if inst_id:
            params['instId'] = inst_id
        
        result = self._request('GET', '/api/v5/trade/orders-pending', params=params)
        
        if result['success']:
            return result['data']
        return []
    
    def get_orders_history(self, inst_type: str = 'SWAP', state: str = None, 
                           limit: int = 100) -> List[Dict]:
        """
        获取历史订单记录（近七天）
        文档: GET /api/v5/trade/orders-history
        限速: 40次/2s
        
        Args:
            inst_type: 产品类型
            state: 订单状态 canceled/filled
            limit: 返回数量 (最大100)
            
        Returns:
            历史订单列表
        """
        params = {
            'instType': inst_type,
            'limit': str(limit),
        }
        if state:
            params['state'] = state
        
        result = self._request('GET', '/api/v5/trade/orders-history', params=params)
        
        if result['success']:
            return result['data']
        return []
    
    def get_fills(self, inst_type: str = 'SWAP', limit: int = 100) -> List[Dict]:
        """
        获取成交明细（近三天）
        文档: GET /api/v5/trade/fills
        限速: 60次/2s
        
        Args:
            inst_type: 产品类型
            limit: 返回数量 (最大100)
            
        Returns:
            成交明细列表
        """
        params = {
            'instType': inst_type,
            'limit': str(limit),
        }
        
        result = self._request('GET', '/api/v5/trade/fills', params=params)
        
        if result['success']:
            return result['data']
        return []
    
    def get_algo_orders_pending(self, ord_type: str = 'conditional') -> List[Dict]:
        """
        获取未完成策略委托单列表
        文档: GET /api/v5/trade/orders-algo-pending
        限速: 20次/2s
        
        Args:
            ord_type: 订单类型 conditional/oco/trigger/move_order_stop/iceberg/twap
            
        Returns:
            策略订单列表
        """
        params = {'ordType': ord_type}
        result = self._request('GET', '/api/v5/trade/orders-algo-pending', params=params)
        
        if result['success']:
            return result['data']
        return []
    
    def cancel_algo_order(self, algo_id: str, inst_id: str) -> bool:
        """
        撤销策略订单
        文档: POST /api/v5/trade/cancel-algos
        限速: 20次/2s
        
        Args:
            algo_id: 策略订单ID
            inst_id: 产品ID
            
        Returns:
            是否成功
        """
        data = [{
            'algoId': algo_id,
            'instId': inst_id,
        }]
        
        result = self._request('POST', '/api/v5/trade/cancel-algos', data=data)
        return result['success']
    
    def get_history_candles(self, coin: str, bar: str = '1H', 
                            limit: int = 100, after: str = None) -> List[Dict]:
        """
        获取历史K线数据
        文档: GET /api/v5/market/history-candles
        限速: 20次/2s
        
        与 get_kline 不同，此接口可获取更早的历史数据
        
        Args:
            coin: 币种
            bar: K线周期
            limit: 数量限制
            after: 请求此时间戳之前的数据
            
        Returns:
            K线数据列表
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {
            'instId': inst_id,
            'bar': bar,
            'limit': str(limit),
        }
        if after:
            params['after'] = after
        
        result = self._request('GET', '/api/v5/market/history-candles', params=params)
        
        if result['success'] and result['data']:
            klines = []
            for k in result['data']:
                klines.append({
                    'timestamp': int(k[0]),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                    'volume_ccy': float(k[6]),
                    'confirm': k[8] if len(k) > 8 else '1',  # K线状态 0:未完结 1:已完结
                })
            return klines
        return []
    
    def get_funding_rate(self, coin: str) -> Dict:
        """
        获取永续合约当前资金费率
        文档: GET /api/v5/public/funding-rate
        限速: 20次/2s
        
        Args:
            coin: 币种
            
        Returns:
            资金费率信息
        """
        inst_id = f"{coin}{TradingConfig.OKX_INST_SUFFIX}"
        params = {'instId': inst_id}
        
        result = self._request('GET', '/api/v5/public/funding-rate', params=params)
        
        if result['success'] and result['data']:
            data = result['data'][0]
            return {
                'inst_id': data.get('instId'),
                'funding_rate': float(data.get('fundingRate', 0)),
                'next_funding_rate': float(data.get('nextFundingRate', 0)) if data.get('nextFundingRate') else None,
                'funding_time': data.get('fundingTime'),
                'next_funding_time': data.get('nextFundingTime'),
            }
        return None
    
    def get_server_time(self) -> Dict:
        """
        获取服务器时间（用于时间同步校验）
        文档: GET /api/v5/public/time
        限速: 10次/2s
        
        Returns:
            服务器时间信息
        """
        result = self._request('GET', '/api/v5/public/time')
        
        if result['success'] and result['data']:
            data = result['data'][0]
            server_ts = int(data.get('ts', 0))
            local_ts = int(time.time() * 1000)
            time_diff = local_ts - server_ts
            
            return {
                'server_time': server_ts,
                'local_time': local_ts,
                'time_diff_ms': time_diff,
                'time_synced': abs(time_diff) < 5000,  # 允许5秒误差
            }
        return None
    
    def health_check(self) -> Dict:
        """
        API 健康检查
        
        检查项目:
        1. 服务器连通性
        2. 时间同步状态
        3. API Key 有效性
        4. 账户状态
        
        Returns:
            健康检查结果
        """
        health = {
            'status': 'unknown',
            'checks': {},
            'errors': [],
        }
        
        # 1. 检查服务器连通性和时间同步
        try:
            server_time = self.get_server_time()
            if server_time:
                health['checks']['server_connectivity'] = True
                health['checks']['time_synced'] = server_time.get('time_synced', False)
                health['checks']['time_diff_ms'] = server_time.get('time_diff_ms', 0)
                
                if not server_time.get('time_synced'):
                    health['errors'].append(f"时间不同步: 差异{server_time.get('time_diff_ms')}ms")
            else:
                health['checks']['server_connectivity'] = False
                health['errors'].append("无法连接服务器")
        except Exception as e:
            health['checks']['server_connectivity'] = False
            health['errors'].append(f"服务器连接异常: {str(e)}")
        
        # 2. 检查账户配置和API Key有效性
        try:
            config = self.get_account_config()
            if config:
                health['checks']['api_key_valid'] = True
                health['checks']['account_level'] = config.get('acct_lv')
                health['checks']['account_mode'] = config.get('pos_mode')
            else:
                health['checks']['api_key_valid'] = False
                health['errors'].append("API Key 无效或权限不足")
        except Exception as e:
            health['checks']['api_key_valid'] = False
            health['errors'].append(f"账户检查异常: {str(e)}")
        
        # 3. 检查余额读取
        try:
            balance = self.get_balance()
            health['checks']['balance_readable'] = balance is not None
            if balance:
                health['checks']['available_usdt'] = balance.get('total_usd', 0)
            else:
                health['errors'].append("无法读取账户余额")
        except Exception as e:
            health['checks']['balance_readable'] = False
            health['errors'].append(f"余额读取异常: {str(e)}")
        
        # 确定总体状态
        critical_checks = ['server_connectivity', 'api_key_valid', 'balance_readable']
        all_ok = all(health['checks'].get(c, False) for c in critical_checks)
        
        if all_ok and not health['errors']:
            health['status'] = 'healthy'
        elif all_ok:
            health['status'] = 'degraded'  # 有警告但核心功能正常
        else:
            health['status'] = 'unhealthy'
        
        return health
    
    def get_api_status(self) -> Dict:
        """
        获取 API 状态统计
        
        Returns:
            API 状态信息，包括连续失败次数、最后成功时间等
        """
        now = time.time()
        last_success_ago = now - self._last_success_time if self._last_success_time else None
        
        return {
            'base_url': self.base_url,
            'demo_trading': self.demo_trading,
            'consecutive_failures': self._consecutive_failures,
            'last_success_time': self._last_success_time,
            'last_success_ago_seconds': round(last_success_ago, 1) if last_success_ago else None,
            'connect_timeout': self.connect_timeout,
            'read_timeout': self.read_timeout,
            'max_retries': self.max_retries,
        }


# 全局实例（延迟初始化）
_okx_exchange = None


def get_okx_exchange() -> OKXExchange:
    """获取 OKX 交易所全局实例"""
    global _okx_exchange
    if _okx_exchange is None:
        _okx_exchange = OKXExchange()
    return _okx_exchange


def reset_okx_exchange():
    """重置 OKX 交易所全局实例（用于配置变更后刷新）"""
    global _okx_exchange
    _okx_exchange = None
    logger.info("OKX 交易所实例已重置")

