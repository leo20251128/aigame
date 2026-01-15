"""
Market data module - Binance API integration
"""
import requests
import time
from typing import Dict, List
from statistics import pstdev

class MarketDataFetcher:
    """Fetch real-time market data from Binance API"""
    
    def __init__(self):
        self.binance_base_url = "https://api.binance.com/api/v3"
        self.binance_futures_url = "https://fapi.binance.com/fapi/v1"  # Binance期货API
        self.okx_public_url = "https://www.okx.com/api/v5/public"  # OKX公共API
        self.coingecko_base_url = "https://api.coingecko.com/api/v3"

        # Binance symbol mapping
        self.binance_symbols = {
            'BTC': 'BTCUSDT',
            'ETH': 'ETHUSDT',
            'SOL': 'SOLUSDT',
            'BNB': 'BNBUSDT',
            'XRP': 'XRPUSDT',
            'DOGE': 'DOGEUSDT'
        }
        
        # OKX 永续合约 symbol mapping
        self.okx_swap_symbols = {
            'BTC': 'BTC-USDT-SWAP',
            'ETH': 'ETH-USDT-SWAP',
            'SOL': 'SOL-USDT-SWAP',
            'BNB': 'BNB-USDT-SWAP',
            'XRP': 'XRP-USDT-SWAP',
            'DOGE': 'DOGE-USDT-SWAP'
        }

        # CoinGecko mapping for technical indicators
        self.coingecko_mapping = {
            'BTC': 'bitcoin',
            'ETH': 'ethereum',
            'SOL': 'solana',
            'BNB': 'binancecoin',
            'XRP': 'ripple',
            'DOGE': 'dogecoin'
        }

        self._cache = {}
        self._cache_time = {}
        self._cache_duration = 5  # Cache for 5 seconds
        self._sentiment_cache = {}
        self._sentiment_cache_time = {}
        self._sentiment_cache_ttl = 300  # 5 minutes

        # Rate limiting for CoinGecko API (free tier: 10-30 calls/minute)
        self._last_coingecko_call = 0
        self._coingecko_rate_limit_delay = 3.0  # 3 seconds between calls (20 calls/min max)
        self._historical_cache = {}
        self._historical_cache_time = {}
        self._historical_cache_ttl = 600  # Cache historical data for 10 minutes
    
    def get_current_prices(self, coins: List[str]) -> Dict[str, float]:
        """Get current prices from Binance API"""
        # Check cache
        cache_key = 'prices_' + '_'.join(sorted(coins))
        if cache_key in self._cache:
            if time.time() - self._cache_time[cache_key] < self._cache_duration:
                return self._cache[cache_key]
        
        prices = {}
        
        try:
            # Batch fetch Binance 24h ticker data
            symbols = [self.binance_symbols.get(coin) for coin in coins if coin in self.binance_symbols]
            
            if symbols:
                # Build symbols parameter
                symbols_param = '[' + ','.join([f'"{s}"' for s in symbols]) + ']'
                
                response = requests.get(
                    f"{self.binance_base_url}/ticker/24hr",
                    params={'symbols': symbols_param},
                    timeout=5
                )
                response.raise_for_status()
                data = response.json()
                
                # Parse data
                for item in data:
                    symbol = item['symbol']
                    # Find corresponding coin
                    for coin, binance_symbol in self.binance_symbols.items():
                        if binance_symbol == symbol:
                            prices[coin] = {
                                'price': float(item['lastPrice']),
                                'change_24h': float(item['priceChangePercent']),
                                'volume_24h': float(item.get('quoteVolume', 0))
                            }
                            break
            
            # Update cache
            self._cache[cache_key] = prices
            self._cache_time[cache_key] = time.time()
            
            return prices
            
        except Exception as e:
            print(f"[ERROR] Binance API failed: {e}")
            # Fallback to CoinGecko
            return self._get_prices_from_coingecko(coins)
    
    def _get_prices_from_coingecko(self, coins: List[str]) -> Dict[str, float]:
        """Fallback: Fetch prices from CoinGecko"""
        try:
            coin_ids = [self.coingecko_mapping.get(coin, coin.lower()) for coin in coins]
            
            response = requests.get(
                f"{self.coingecko_base_url}/simple/price",
                params={
                    'ids': ','.join(coin_ids),
                    'vs_currencies': 'usd',
                    'include_24hr_change': 'true',
                    'include_24hr_vol': 'true'
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            prices = {}
            for coin in coins:
                coin_id = self.coingecko_mapping.get(coin, coin.lower())
                if coin_id in data:
                    prices[coin] = {
                        'price': data[coin_id]['usd'],
                        'change_24h': data[coin_id].get('usd_24h_change', 0),
                        'volume_24h': data[coin_id].get('usd_24h_vol', 0)
                    }
            
            return prices
        except Exception as e:
            print(f"[ERROR] CoinGecko fallback also failed: {e}")
            return {coin: {'price': 0, 'change_24h': 0} for coin in coins}
    
    def get_market_data(self, coin: str) -> Dict:
        """Get detailed market data from CoinGecko"""
        coin_id = self.coingecko_mapping.get(coin, coin.lower())

        try:
            # Enforce rate limiting
            self._rate_limit_coingecko()

            response = requests.get(
                f"{self.coingecko_base_url}/coins/{coin_id}",
                params={'localization': 'false', 'tickers': 'false', 'community_data': 'false'},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            market_data = data.get('market_data', {})

            return {
                'current_price': market_data.get('current_price', {}).get('usd', 0),
                'market_cap': market_data.get('market_cap', {}).get('usd', 0),
                'total_volume': market_data.get('total_volume', {}).get('usd', 0),
                'price_change_24h': market_data.get('price_change_percentage_24h', 0),
                'price_change_7d': market_data.get('price_change_percentage_7d', 0),
                'high_24h': market_data.get('high_24h', {}).get('usd', 0),
                'low_24h': market_data.get('low_24h', {}).get('usd', 0),
            }
        except Exception as e:
            print(f"[ERROR] Failed to get market data for {coin}: {e}")
            return {}
    
    def _rate_limit_coingecko(self):
        """Enforce rate limiting for CoinGecko API calls"""
        now = time.time()
        time_since_last_call = now - self._last_coingecko_call
        if time_since_last_call < self._coingecko_rate_limit_delay:
            sleep_time = self._coingecko_rate_limit_delay - time_since_last_call
            time.sleep(sleep_time)
        self._last_coingecko_call = time.time()

    def get_historical_prices(self, coin: str, days: int = 30) -> List[Dict]:
        """Get historical prices (with volume) - Binance first, CoinGecko fallback"""
        # Check cache first
        cache_key = f"{coin}_historical_{days}"
        if cache_key in self._historical_cache:
            if time.time() - self._historical_cache_time[cache_key] < self._historical_cache_ttl:
                return self._historical_cache[cache_key]

        # Try Binance first (no rate limits for free tier)
        binance_symbol = self.binance_symbols.get(coin)
        if binance_symbol:
            try:
                # Binance klines: interval = 1d for daily data
                response = requests.get(
                    f"{self.binance_base_url}/klines",
                    params={
                        'symbol': binance_symbol,
                        'interval': '1d',
                        'limit': days
                    },
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()

                prices = []
                for kline in data:
                    # kline format: [timestamp, open, high, low, close, volume, ...]
                    prices.append({
                        'timestamp': kline[0],
                        'price': float(kline[4]),  # Close price
                        'volume': float(kline[7])  # Quote asset volume (in USDT)
                    })

                # Update cache
                self._historical_cache[cache_key] = prices
                self._historical_cache_time[cache_key] = time.time()

                return prices
            except Exception as e:
                print(f"[WARN] Binance klines failed for {coin}: {e}, trying CoinGecko...")

        # Fallback to CoinGecko
        coin_id = self.coingecko_mapping.get(coin, coin.lower())
        try:
            # Enforce rate limiting
            self._rate_limit_coingecko()

            response = requests.get(
                f"{self.coingecko_base_url}/coins/{coin_id}/market_chart",
                params={'vs_currency': 'usd', 'days': days},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()

            price_series = data.get('prices', [])
            volume_series = data.get('total_volumes', [])

            prices = []
            for idx, price_data in enumerate(price_series):
                volume = volume_series[idx][1] if idx < len(volume_series) else 0
                prices.append({
                    'timestamp': price_data[0],
                    'price': price_data[1],
                    'volume': volume
                })

            # Update cache
            self._historical_cache[cache_key] = prices
            self._historical_cache_time[cache_key] = time.time()

            return prices
        except Exception as e:
            print(f"[ERROR] Failed to get historical prices for {coin} from both APIs: {e}")
            return []
    
    def calculate_atr(self, coin: str, period: int = 14) -> float:
        """
        计算平均真实波幅 (Average True Range)
        
        Args:
            coin: 币种
            period: 周期 (默认14)
            
        Returns:
            ATR值
        """
        historical = self.get_historical_prices(coin, days=period + 5)
        
        if len(historical) < period + 1:
            return 0.0
        
        true_ranges = []
        for i in range(1, len(historical)):
            high = historical[i].get('high', historical[i]['price'])
            low = historical[i].get('low', historical[i]['price'])
            prev_close = historical[i-1]['price']
            
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            true_ranges.append(tr)
        
        if len(true_ranges) < period:
            return 0.0
        
        # 计算ATR (简单移动平均)
        atr = sum(true_ranges[-period:]) / period
        return round(atr, 2)
    
    def get_intraday_klines(self, coin: str, interval: str = '3m', limit: int = 10) -> Dict:
        """
        获取日内K线数据（3分钟间隔）
        
        Args:
            coin: 币种
            interval: K线间隔 ('1m', '3m', '5m', '15m', '1h', '4h')
            limit: 获取数量（默认10）
            
        Returns:
            包含价格序列和技术指标序列的字典
        """
        cache_key = f"{coin}_intraday_{interval}_{limit}"
        if cache_key in self._cache:
            if time.time() - self._cache_time.get(cache_key, 0) < 60:  # 1分钟缓存
                return self._cache[cache_key]
        
        binance_symbol = self.binance_symbols.get(coin)
        if not binance_symbol:
            return {}
        
        try:
            # 获取更多数据以计算指标
            fetch_limit = max(limit + 30, 50)  # 需要额外数据来计算EMA/RSI等
            
            response = requests.get(
                f"{self.binance_base_url}/klines",
                params={
                    'symbol': binance_symbol,
                    'interval': interval,
                    'limit': fetch_limit
                },
                timeout=10
            )
            response.raise_for_status()
            klines = response.json()
            
            if not klines:
                return {}
            
            # 解析K线数据: [timestamp, open, high, low, close, volume, ...]
            prices = [float(k[4]) for k in klines]  # close prices
            volumes = [float(k[5]) for k in klines]
            
            # 计算技术指标
            ema20 = self._calculate_ema_series(prices, 20)
            rsi7 = self._calculate_rsi_series(prices, 7)
            rsi14 = self._calculate_rsi_series(prices, 14)
            macd_data = self._calculate_macd_series(prices)
            
            # 只返回最近limit个数据点（即使指标数量不足也返回可用数据）
            result = {
                'prices': [round(p, 4) for p in prices[-limit:]],
                'volumes': [round(v, 2) for v in volumes[-limit:]],
                'ema20': [round(e, 4) for e in ema20[-limit:]] if ema20 else [],
                'rsi7': [round(r, 1) for r in rsi7[-limit:]] if rsi7 else [],
                'rsi14': [round(r, 1) for r in rsi14[-limit:]] if rsi14 else [],
                'macd': [round(m, 6) for m in macd_data['macd'][-limit:]] if macd_data and macd_data.get('macd') else [],
                'macd_signal': [round(s, 6) for s in macd_data['signal'][-limit:]] if macd_data and macd_data.get('signal') else [],
                'interval': interval,
                'count': len(prices[-limit:])
            }
            
            print(f"[DEBUG] {coin} intraday data: prices={len(result['prices'])}, ema20={len(result['ema20'])}, rsi7={len(result['rsi7'])}")
            
            # 缓存
            self._cache[cache_key] = result
            self._cache_time[cache_key] = time.time()
            
            return result
            
        except Exception as e:
            print(f"[ERROR] Failed to get intraday klines for {coin}: {e}")
            return {}
    
    def get_4h_klines(self, coin: str, limit: int = 10) -> Dict:
        """
        获取4小时K线数据
        
        Args:
            coin: 币种
            limit: 获取数量（默认10）
            
        Returns:
            包含价格序列和技术指标序列的字典
        """
        cache_key = f"{coin}_4h_{limit}"
        if cache_key in self._cache:
            if time.time() - self._cache_time.get(cache_key, 0) < 300:  # 5分钟缓存
                return self._cache[cache_key]
        
        binance_symbol = self.binance_symbols.get(coin)
        if not binance_symbol:
            return {}
        
        try:
            # 获取更多数据以计算指标
            fetch_limit = max(limit + 60, 80)
            
            response = requests.get(
                f"{self.binance_base_url}/klines",
                params={
                    'symbol': binance_symbol,
                    'interval': '4h',
                    'limit': fetch_limit
                },
                timeout=10
            )
            response.raise_for_status()
            klines = response.json()
            
            if not klines:
                return {}
            
            # 解析K线数据
            prices = [float(k[4]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            volumes = [float(k[5]) for k in klines]
            
            # 计算技术指标
            ema20 = self._calculate_ema_series(prices, 20)
            ema50 = self._calculate_ema_series(prices, 50)
            rsi14 = self._calculate_rsi_series(prices, 14)
            macd_data = self._calculate_macd_series(prices)
            atr3 = self._calculate_atr_series(highs, lows, prices, 3)
            atr14 = self._calculate_atr_series(highs, lows, prices, 14)
            
            # 计算平均成交量
            avg_volume = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes) if volumes else 0
            
            result = {
                'prices': [round(p, 4) for p in prices[-limit:]],
                'ema20': [round(e, 4) for e in ema20[-limit:]] if ema20 else [],
                'ema50': [round(e, 4) for e in ema50[-limit:]] if ema50 else [],
                'rsi14': [round(r, 1) for r in rsi14[-limit:]] if rsi14 else [],
                'macd': [round(m, 6) for m in macd_data['macd'][-limit:]] if macd_data and macd_data.get('macd') else [],
                'macd_signal': [round(s, 6) for s in macd_data['signal'][-limit:]] if macd_data and macd_data.get('signal') else [],
                'atr3': round(atr3[-1], 4) if atr3 else 0,
                'atr14': round(atr14[-1], 4) if atr14 else 0,
                'current_volume': round(volumes[-1], 2) if volumes else 0,
                'avg_volume': round(avg_volume, 2),
                'interval': '4h',
                'count': len(prices[-limit:])
            }
            
            print(f"[DEBUG] {coin} 4h data: prices={len(result['prices'])}, ema20={len(result['ema20'])}, macd={len(result['macd'])}")
            
            # 缓存
            self._cache[cache_key] = result
            self._cache_time[cache_key] = time.time()
            
            return result
            
        except Exception as e:
            print(f"[ERROR] Failed to get 4h klines for {coin}: {e}")
            return {}
    
    def _calculate_rsi_series(self, prices: List[float], period: int = 14) -> List[float]:
        """计算RSI序列"""
        if len(prices) < period + 1:
            return []
        
        rsi_values = []
        for i in range(period, len(prices)):
            window = prices[i-period:i+1]
            changes = [window[j] - window[j-1] for j in range(1, len(window))]
            gains = [c if c > 0 else 0 for c in changes]
            losses = [-c if c < 0 else 0 for c in changes]
            
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            
            if avg_loss == 0:
                rsi_values.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi_values.append(100 - (100 / (1 + rs)))
        
        return rsi_values
    
    def _calculate_macd_series(self, prices: List[float]) -> Dict:
        """计算MACD序列"""
        if len(prices) < 26:
            return {}
        
        ema12 = self._calculate_ema_series(prices, 12)
        ema26 = self._calculate_ema_series(prices, 26)
        
        # MACD = EMA12 - EMA26
        macd_line = [ema12[i] - ema26[i] for i in range(len(ema26))]
        
        # Signal = EMA9 of MACD
        signal_line = self._calculate_ema_series(macd_line, 9)
        
        return {
            'macd': macd_line,
            'signal': signal_line
        }
    
    def _calculate_atr_series(self, highs: List[float], lows: List[float], closes: List[float], period: int) -> List[float]:
        """计算ATR序列"""
        if len(closes) < period + 1:
            return []
        
        true_ranges = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            true_ranges.append(tr)
        
        # 计算ATR (简单移动平均)
        atr_values = []
        for i in range(period - 1, len(true_ranges)):
            atr = sum(true_ranges[i-period+1:i+1]) / period
            atr_values.append(atr)
        
        return atr_values
    
    def get_funding_rate(self, coin: str) -> Dict:
        """
        获取永续合约资金费率 (使用 OKX API)
        
        OKX API: GET /api/v5/public/funding-rate
        文档: https://www.okx.com/docs-v5/zh/#public-data-rest-api-get-funding-rate
        
        Args:
            coin: 币种
            
        Returns:
            包含当前资金费率和下次结算时间的字典
        """
        cache_key = f"{coin}_funding_okx"
        if cache_key in self._cache:
            if time.time() - self._cache_time.get(cache_key, 0) < 60:  # 1分钟缓存
                return self._cache[cache_key]
        
        okx_symbol = self.okx_swap_symbols.get(coin)
        if not okx_symbol:
            return {}
        
        try:
            response = requests.get(
                f"{self.okx_public_url}/funding-rate",
                params={'instId': okx_symbol},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') != '0' or not data.get('data'):
                return {}
            
            funding_data = data['data'][0]
            
            result = {
                'funding_rate': float(funding_data.get('fundingRate', 0)) * 100,  # 转为百分比
                'next_funding_rate': float(funding_data.get('nextFundingRate', 0)) * 100,
                'next_funding_time': int(funding_data.get('fundingTime', 0)),
            }
            
            self._cache[cache_key] = result
            self._cache_time[cache_key] = time.time()
            
            return result
            
        except Exception as e:
            print(f"[WARN] Failed to get OKX funding rate for {coin}: {e}")
            return {}
    
    def get_open_interest(self, coin: str) -> Dict:
        """
        获取永续合约持仓量 (使用 OKX API)
        
        OKX API: GET /api/v5/public/open-interest
        文档: https://www.okx.com/docs-v5/zh/#public-data-rest-api-get-open-interest
        
        Args:
            coin: 币种
            
        Returns:
            包含持仓量和变化率的字典
        """
        cache_key = f"{coin}_oi_okx"
        if cache_key in self._cache:
            if time.time() - self._cache_time.get(cache_key, 0) < 60:
                return self._cache[cache_key]
        
        okx_symbol = self.okx_swap_symbols.get(coin)
        if not okx_symbol:
            return {}
        
        try:
            response = requests.get(
                f"{self.okx_public_url}/open-interest",
                params={'instType': 'SWAP', 'instId': okx_symbol},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') != '0' or not data.get('data'):
                return {}
            
            oi_data = data['data'][0]
            oi = float(oi_data.get('oi', 0))  # 持仓量（张数）
            oi_ccy = float(oi_data.get('oiCcy', 0))  # 持仓量（币数）
            
            # OKX 没有直接提供24h变化，这里简单返回0
            # 如果需要可以自己存储历史数据计算
            result = {
                'open_interest': oi,
                'open_interest_ccy': oi_ccy,
                'oi_change_24h': 0  # OKX API不直接提供
            }
            
            self._cache[cache_key] = result
            self._cache_time[cache_key] = time.time()
            
            return result
            
        except Exception as e:
            print(f"[WARN] Failed to get OKX open interest for {coin}: {e}")
            return {}
    
    def get_mark_price(self, coin: str) -> Dict:
        """
        获取标记价格 (使用 OKX API)
        
        OKX API: GET /api/v5/public/mark-price
        
        Args:
            coin: 币种
            
        Returns:
            包含标记价格的字典
        """
        cache_key = f"{coin}_mark_okx"
        if cache_key in self._cache:
            if time.time() - self._cache_time.get(cache_key, 0) < 10:  # 10秒缓存
                return self._cache[cache_key]
        
        okx_symbol = self.okx_swap_symbols.get(coin)
        if not okx_symbol:
            return {}
        
        try:
            response = requests.get(
                f"{self.okx_public_url}/mark-price",
                params={'instType': 'SWAP', 'instId': okx_symbol},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get('code') != '0' or not data.get('data'):
                return {}
            
            mark_data = data['data'][0]
            
            result = {
                'mark_price': float(mark_data.get('markPx', 0)),
            }
            
            self._cache[cache_key] = result
            self._cache_time[cache_key] = time.time()
            
            return result
            
        except Exception as e:
            print(f"[WARN] Failed to get OKX mark price for {coin}: {e}")
            return {}
    
    def get_futures_data(self, coin: str) -> Dict:
        """
        获取合约综合数据（资金费率+持仓量+标记价格）- 使用 OKX API
        
        Args:
            coin: 币种
            
        Returns:
            合约数据字典
        """
        funding = self.get_funding_rate(coin)
        oi = self.get_open_interest(coin)
        mark = self.get_mark_price(coin)
        
        return {
            'funding_rate': funding.get('funding_rate', 0),
            'next_funding_rate': funding.get('next_funding_rate', 0),
            'mark_price': mark.get('mark_price', 0),
            'open_interest': oi.get('open_interest', 0),
            'open_interest_ccy': oi.get('open_interest_ccy', 0),
            'oi_change_24h': oi.get('oi_change_24h', 0)
        }
    
    def calculate_multi_timeframe_signals(self, coin: str) -> Dict:
        """
        多时间框架趋势分析
        
        Args:
            coin: 币种
            
        Returns:
            多时间框架信号字典
        """
        signals = {}
        
        # 短期 (7天)
        hist_7d = self.get_historical_prices(coin, days=7)
        if len(hist_7d) >= 7:
            prices_7d = [p['price'] for p in hist_7d]
            change_7d = (prices_7d[-1] - prices_7d[0]) / prices_7d[0]
            signals['trend_7d'] = 'bullish' if change_7d > 0 else 'bearish'
            signals['strength_7d'] = abs(change_7d)
        
        # 中期 (30天)
        hist_30d = self.get_historical_prices(coin, days=30)
        if len(hist_30d) >= 30:
            prices_30d = [p['price'] for p in hist_30d]
            change_30d = (prices_30d[-1] - prices_30d[0]) / prices_30d[0]
            signals['trend_30d'] = 'bullish' if change_30d > 0 else 'bearish'
            signals['strength_30d'] = abs(change_30d)
        
        # 趋势一致性评分
        if signals.get('trend_7d') and signals.get('trend_30d'):
            if signals['trend_7d'] == signals['trend_30d']:
                signals['trend_alignment'] = 1.0
            else:
                signals['trend_alignment'] = 0.0
        
        return signals
    
    def calculate_technical_indicators(self, coin: str) -> Dict:
        """Calculate extended technical, volatility, sentiment signals"""
        historical = self.get_historical_prices(coin, days=30)
        
        if not historical or len(historical) < 26:
            return {}
        
        prices = [p['price'] for p in historical]
        volumes = [p.get('volume', 0) for p in historical]
        
        # Simple Moving Averages
        sma_7 = sum(prices[-7:]) / 7 if len(prices) >= 7 else prices[-1]
        sma_14 = sum(prices[-14:]) / 14 if len(prices) >= 14 else prices[-1]
        
        # RSI 14
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [c if c > 0 else 0 for c in changes]
        losses = [-c if c < 0 else 0 for c in changes]
        avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else (sum(gains) / len(gains) if gains else 0)
        avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else (sum(losses) / len(losses) if losses else 0)
        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
        
        # MACD (12, 26, 9)
        ema12 = self._calculate_ema_series(prices, 12)
        ema26 = self._calculate_ema_series(prices, 26)
        macd_line = [a - b for a, b in zip(ema12[-len(ema26):], ema26[-len(ema12):])]
        signal_line = self._calculate_ema_series(macd_line, 9)
        macd_value = macd_line[-1] if macd_line else 0
        macd_signal = signal_line[-1] if signal_line else 0
        
        # Bollinger Bands (20)
        bollinger = None
        if len(prices) >= 20:
            recent = prices[-20:]
            mid = sum(recent) / 20
            variance = sum((p - mid) ** 2 for p in recent) / 20
            std = variance ** 0.5
            bollinger = {
                'upper': mid + 2 * std,
                'mid': mid,
                'lower': mid - 2 * std
            }
        
        # 7d volatility (annualized %)
        volatility_7d = 0
        if len(prices) >= 8:
            returns = []
            for i in range(len(prices) - 7, len(prices)):
                prev_price = prices[i-1]
                if prev_price > 0:
                    returns.append((prices[i] - prev_price) / prev_price)
            if len(returns) >= 2:
                volatility_7d = pstdev(returns) * (365 ** 0.5) * 100
        
        sentiment_score, news_signal = self._get_sentiment_signal(coin)
        
        # 计算ATR
        atr = self.calculate_atr(coin, period=14)
        
        # 多时间框架信号
        mtf_signals = self.calculate_multi_timeframe_signals(coin)
        
        indicators = {
            'sma_7': sma_7,
            'sma_14': sma_14,
            'rsi_14': rsi,
            'current_price': prices[-1],
            'price_change_7d': ((prices[-1] - prices[-8]) / prices[-8]) * 100 if len(prices) >= 8 and prices[-8] > 0 else 0,
            'macd': macd_value,
            'macd_signal': macd_signal,
            'bollinger': bollinger,
            'volatility_7d': volatility_7d,
            'sentiment_score': sentiment_score,
            'news_signal': news_signal,
            'average_volume_7d': sum(volumes[-7:]) / 7 if len(volumes) >= 7 else (volumes[-1] if volumes else 0),
            'atr_14': atr
        }
        
        # 合并多时间框架信号
        indicators.update(mtf_signals)
        
        return indicators

    def _calculate_ema_series(self, values: List[float], period: int) -> List[float]:
        if not values:
            return []
        ema_values = []
        k = 2 / (period + 1)
        ema = values[0]
        ema_values.append(ema)
        for price in values[1:]:
            ema = price * k + ema * (1 - k)
            ema_values.append(ema)
        return ema_values

    def _get_sentiment_signal(self, coin: str):
        coin_id = self.coingecko_mapping.get(coin, coin.lower())
        cache_key = f"{coin_id}_sentiment"
        now = time.time()
        if cache_key in self._sentiment_cache:
            if now - self._sentiment_cache_time[cache_key] < self._sentiment_cache_ttl:
                return self._sentiment_cache[cache_key]

        try:
            # Enforce rate limiting
            self._rate_limit_coingecko()

            response = requests.get(
                f"{self.coingecko_base_url}/coins/{coin_id}",
                params={
                    'localization': 'false',
                    'tickers': 'false',
                    'market_data': 'false',
                    'community_data': 'true',
                    'developer_data': 'false',
                    'sparkline': 'false'
                },
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            up = data.get('sentiment_votes_up_percentage')
            down = data.get('sentiment_votes_down_percentage')
            if up is None or down is None:
                sentiment_score = 0
            else:
                sentiment_score = max(-1.0, min(1.0, (up - down) / 100))

            if sentiment_score > 0.25:
                news_signal = 'positive'
            elif sentiment_score < -0.25:
                news_signal = 'negative'
            else:
                news_signal = 'neutral'

            self._sentiment_cache[cache_key] = (sentiment_score, news_signal)
            self._sentiment_cache_time[cache_key] = now
            return sentiment_score, news_signal
        except Exception as e:
            print(f"[WARN] Sentiment fetch failed for {coin}: {e}")
            return 0, 'neutral'
    
    def get_market_sentiment(self) -> Dict:
        """
        获取整体市场情绪数据
        包括恐慌贪婪指数、BTC主导地位、市场整体趋势等
        """
        cache_key = "market_sentiment"
        now = time.time()
        
        if cache_key in self._sentiment_cache:
            if now - self._sentiment_cache_time.get(cache_key, 0) < 600:  # 10分钟缓存
                return self._sentiment_cache[cache_key]
        
        result = {
            'fear_greed_index': 50,
            'fear_greed_label': '中性',
            'btc_dominance': 50,
            'market_trend': '震荡',
            'volume_trend': '正常',
            'social_sentiment': '中性'
        }
        
        try:
            # 获取恐慌贪婪指数
            try:
                fng_response = requests.get(
                    'https://api.alternative.me/fng/',
                    timeout=5
                )
                if fng_response.status_code == 200:
                    fng_data = fng_response.json()
                    if fng_data.get('data'):
                        fng_value = int(fng_data['data'][0].get('value', 50))
                        fng_class = fng_data['data'][0].get('value_classification', 'Neutral')
                        result['fear_greed_index'] = fng_value
                        # 翻译标签
                        label_map = {
                            'Extreme Fear': '极度恐慌',
                            'Fear': '恐慌',
                            'Neutral': '中性',
                            'Greed': '贪婪',
                            'Extreme Greed': '极度贪婪'
                        }
                        result['fear_greed_label'] = label_map.get(fng_class, fng_class)
            except:
                pass
            
            # 获取市场整体数据
            try:
                self._rate_limit_coingecko()
                global_response = requests.get(
                    f"{self.coingecko_base_url}/global",
                    timeout=10
                )
                if global_response.status_code == 200:
                    global_data = global_response.json().get('data', {})
                    result['btc_dominance'] = global_data.get('market_cap_percentage', {}).get('btc', 50)
                    
                    # 市场涨跌比例
                    market_cap_change = global_data.get('market_cap_change_percentage_24h_usd', 0)
                    if market_cap_change > 3:
                        result['market_trend'] = '强势上涨'
                    elif market_cap_change > 1:
                        result['market_trend'] = '温和上涨'
                    elif market_cap_change < -3:
                        result['market_trend'] = '大幅下跌'
                    elif market_cap_change < -1:
                        result['market_trend'] = '温和下跌'
                    else:
                        result['market_trend'] = '横盘震荡'
            except:
                pass
            
            # 根据恐慌贪婪指数判断社交情绪
            fng = result['fear_greed_index']
            if fng >= 75:
                result['social_sentiment'] = '市场狂热，散户FOMO情绪强烈，需警惕见顶'
            elif fng >= 55:
                result['social_sentiment'] = '市场乐观，多头情绪占主导'
            elif fng >= 45:
                result['social_sentiment'] = '市场观望，多空分歧明显'
            elif fng >= 25:
                result['social_sentiment'] = '市场恐慌，但可能是抄底机会'
            else:
                result['social_sentiment'] = '极度恐慌，恐慌性抛售，逆向机会可能来临'
            
            self._sentiment_cache[cache_key] = result
            self._sentiment_cache_time[cache_key] = now
            
        except Exception as e:
            print(f"[WARN] Market sentiment fetch failed: {e}")
        
        return result


if __name__ == '__main__':
    fetcher = MarketDataFetcher()
    coins = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE']

    print("=== Current Prices (with volume) ===")
    prices = fetcher.get_current_prices(coins)
    for coin in coins:
        info = prices.get(coin, {})
        print(f"{coin}: price=${info.get('price', 0):.2f}, change_24h={info.get('change_24h', 0):+.2f}%, volume_24h=${info.get('volume_24h', 0)/1e6:.2f}M")

    print("\n=== Technical Indicators Sample ===")
    for coin in coins:
        indicators = fetcher.calculate_technical_indicators(coin)
        if not indicators:
            print(f"{coin}: insufficient data")
            continue
        print(f"{coin}:")
        print(f"  SMA7/SMA14: {indicators['sma_7']:.2f} / {indicators['sma_14']:.2f}")
        print(f"  RSI14: {indicators['rsi_14']:.2f}")
        print(f"  MACD / Signal: {indicators['macd']:.4f} / {indicators['macd_signal']:.4f}")
        if indicators.get('bollinger'):
            bb = indicators['bollinger']
            print(f"  Bollinger upper/mid/lower: {bb['upper']:.2f} / {bb['mid']:.2f} / {bb['lower']:.2f}")
        print(f"  7d Volatility: {indicators['volatility_7d']:.2f}%")
        print(f"  Sentiment score: {indicators['sentiment_score']:+.2f}, news signal: {indicators['news_signal']}")
        print(f"  Avg volume 7d: ${indicators['average_volume_7d']/1e6:.2f}M")

