from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import time
import threading
import json
import re
from datetime import datetime
from pathlib import Path
import logging
from trading_engine import TradingEngine
from real_trading_engine import create_trading_engine, RealTradingEngine
from market_data import MarketDataFetcher
from ai_trader import AITrader
from database import Database
from version import __version__, __github_owner__, __repo__, GITHUB_REPO_URL, LATEST_RELEASE_URL
from risk_manager import PerformanceAnalyzer
from circuit_breaker import circuit_manager
from trading_config import TradingConfig
from okx_exchange import get_okx_exchange

app = Flask(__name__)
CORS(app)

# Logging setup
LOG_DIR = Path(__file__).resolve().parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / 'app.log'

logger = logging.getLogger('AITradeGame')
if not logger.handlers:
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False

db = Database('AITradeGame.db')
market_fetcher = MarketDataFetcher()
trading_engines = {}
auto_trading = True
TRADE_FEE_RATE = 0.001  # 默认交易费率
performance_analyzer = PerformanceAnalyzer(db)


def detect_provider_type(provider: dict) -> str:
    """Infer provider_type from DB record."""
    if not provider:
        return 'openai'

    provider_type = provider.get('provider_type')
    if provider_type:
        return provider_type.lower()

    api_url = (provider.get('api_url') or '').lower()
    name = (provider.get('name') or '').lower()

    if 'anthropic' in api_url or 'anthropic' in name or 'claude' in api_url:
        return 'anthropic'
    if 'gemini' in api_url or 'googleapis' in api_url:
        return 'gemini'
    if 'siliconflow' in api_url:
        return 'siliconflow'
    if 'deepseek' in api_url or 'deepseek' in name:
        return 'deepseek'
    if 'azure' in api_url:
        return 'azure_openai'

    return 'openai'

@app.route('/')
def index():
    return render_template('index.html')

# ============ Provider API Endpoints ============

@app.route('/api/providers', methods=['GET'])
def get_providers():
    """Get all API providers"""
    providers = db.get_all_providers()
    return jsonify(providers)

@app.route('/api/providers', methods=['POST'])
def add_provider():
    """Add new API provider"""
    data = request.json
    try:
        provider_id = db.add_provider(
            name=data['name'],
            api_url=data['api_url'],
            api_key=data['api_key'],
            models=data.get('models', '')
        )
        return jsonify({'id': provider_id, 'message': 'Provider added successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/providers/<int:provider_id>', methods=['DELETE'])
def delete_provider(provider_id):
    """Delete API provider"""
    try:
        db.delete_provider(provider_id)
        return jsonify({'message': 'Provider deleted successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/providers/models', methods=['POST'])
def fetch_provider_models():
    """Fetch available models from provider's API"""
    data = request.json
    api_url = data.get('api_url')
    api_key = data.get('api_key')

    if not api_url or not api_key:
        return jsonify({'error': 'API URL and key are required'}), 400

    try:
        # This is a placeholder - implement actual API call based on provider
        # For now, return empty list or common models
        models = []

        # Try to detect provider type and call appropriate API
        if 'openai.com' in api_url.lower():
            # OpenAI API call
            import requests
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            response = requests.get(f'{api_url}/models', headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
                models = [m['id'] for m in result.get('data', []) if 'gpt' in m['id'].lower()]
        elif 'deepseek' in api_url.lower():
            # DeepSeek API
            import requests
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }
            response = requests.get(f'{api_url}/models', headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
                models = [m['id'] for m in result.get('data', [])]
        else:
            # Default: return common model names
            models = ['gpt-3.5-turbo', 'gpt-4', 'gpt-4-turbo']

        return jsonify({'models': models})
    except Exception as e:
        logger.exception("Fetch models failed")
        return jsonify({'error': f'Failed to fetch models: {str(e)}'}), 500

# ============ Model API Endpoints ============

@app.route('/api/models', methods=['GET'])
def get_models():
    models = db.get_all_models()
    return jsonify(models)

@app.route('/api/models', methods=['POST'])
def add_model():
    data = request.json
    try:
        # Get provider info
        provider = db.get_provider(data['provider_id'])
        if not provider:
            return jsonify({'error': 'Provider not found'}), 404

        model_id = db.add_model(
            name=data['name'],
            provider_id=data['provider_id'],
            model_name=data['model_name'],
            initial_capital=float(data.get('initial_capital', 100000))
        )

        model = db.get_model(model_id)
        
        # Get provider info
        provider = db.get_provider(model['provider_id'])
        if not provider:
            return jsonify({'error': 'Provider not found'}), 404
        
        provider_type = detect_provider_type(provider)

        trading_engines[model_id] = create_trading_engine(
            model_id=model_id,
            db=db,
            market_fetcher=market_fetcher,
            ai_trader=AITrader(
                provider_type=provider_type,
                api_key=provider['api_key'],
                api_url=provider['api_url'],
                model_name=model['model_name'],
                db=db,  # 传入db用于获取历史数据
                market_fetcher=market_fetcher  # 传入市场数据获取器
            )
        )
        trading_mode = "真实交易" if TradingConfig.ENABLE_REAL_TRADING else "模拟交易"
        logger.info(f"Model {model_id} ({data['name']}) initialized [{trading_mode}]")

        return jsonify({'id': model_id, 'message': 'Model added successfully'})

    except Exception as e:
        logger.exception("Failed to add model")
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>', methods=['DELETE'])
def delete_model(model_id):
    try:
        model = db.get_model(model_id)
        model_name = model['name'] if model else f"ID-{model_id}"
        
        db.delete_model(model_id)
        if model_id in trading_engines:
            del trading_engines[model_id]
        
        logger.info("Model %s (%s) deleted", model_id, model_name)
        return jsonify({'message': 'Model deleted successfully'})
    except Exception as e:
        logger.exception("Delete model %s failed", model_id)
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>/portfolio', methods=['GET'])
def get_portfolio(model_id):
    prices_data = market_fetcher.get_current_prices(['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE'])
    current_prices = {coin: prices_data[coin]['price'] for coin in prices_data}
    
    # 真实交易模式：从 OKX 获取持仓
    if TradingConfig.ENABLE_REAL_TRADING:
        try:
            exchange = get_okx_exchange()
            balance = exchange.get_account_balance()
            okx_positions = exchange.get_positions()
            
            # 转换为标准格式
            positions = []
            positions_value = 0
            unrealized_pnl = 0
            
            for pos in okx_positions:
                coin = pos['coin']
                current_price = current_prices.get(coin, pos['avg_price'])
                
                # 使用 OKX 返回的保证金，如果没有则计算
                margin = pos.get('margin', 0)
                if margin <= 0:
                    notional = pos.get('notional_usd', 0) or (pos['quantity'] * current_price)
                    margin = notional / pos['leverage'] if pos['leverage'] > 0 else notional
                
                positions.append({
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
                    'margin': margin,
                    'notional_usd': pos.get('notional_usd', 0),
                    'liq_price': pos.get('liq_price'),
                })
                positions_value += margin
                unrealized_pnl += pos['unrealized_pnl']
            
            model = db.get_model(model_id)
            initial_capital = model['initial_capital'] if model else 10000
            total_equity = balance.get('total_equity', 0) if balance.get('success') else 0
            available_balance = balance.get('available_balance', 0) if balance.get('success') else 0
            
            portfolio = {
                'model_id': model_id,
                'total_value': total_equity,
                'cash': available_balance,
                'positions_value': positions_value,
                'positions': positions,
                'realized_pnl': 0,
                'unrealized_pnl': unrealized_pnl,
                'initial_capital': initial_capital,
                'is_real_trading': True
            }
        except Exception as e:
            logger.error(f"获取 OKX 持仓失败: {e}")
            portfolio = db.get_portfolio(model_id, current_prices)
    else:
        portfolio = db.get_portfolio(model_id, current_prices)
    
    # 获取所有历史数据用于缩放查看
    account_value = db.get_account_value_history(model_id, limit=100000)
    
    return jsonify({
        'portfolio': portfolio,
        'account_value_history': account_value
    })

@app.route('/api/models/<int:model_id>/close-all-positions', methods=['POST'])
def close_all_positions(model_id):
    """一键平仓：关闭指定模型的所有持仓"""
    try:
        model = db.get_model(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        
        # 获取当前市场价格
        prices_data = market_fetcher.get_current_prices(TradingConfig.TRADING_COINS)
        current_prices = {coin: prices_data[coin]['price'] for coin in prices_data}
        
        # 执行一键平仓
        closed_positions = db.close_all_positions(model_id, current_prices)
        
        if not closed_positions:
            return jsonify({
                'success': True,
                'message': '没有持仓需要平仓',
                'closed_positions': []
            })
        
        # 计算总盈亏
        total_net_pnl = sum(pos['net_pnl'] for pos in closed_positions)
        total_fee = sum(pos['fee'] for pos in closed_positions)
        
        # 记录账户价值快照
        updated_portfolio = db.get_portfolio(model_id, current_prices)
        db.record_account_value(
            model_id,
            updated_portfolio['total_value'],
            updated_portfolio['cash'],
            updated_portfolio['positions_value']
        )
        
        logger.info(f"[一键平仓] Model {model_id}: 平仓 {len(closed_positions)} 个持仓, "
                   f"总盈亏: ${total_net_pnl:.2f}, 总费用: ${total_fee:.2f}")
        
        return jsonify({
            'success': True,
            'message': f'成功平仓 {len(closed_positions)} 个持仓',
            'closed_positions': closed_positions,
            'total_net_pnl': total_net_pnl,
            'total_fee': total_fee
        })
        
    except Exception as e:
        logger.error(f"[一键平仓] Model {model_id} 失败: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/models/<int:model_id>/trades', methods=['GET'])
def get_trades(model_id):
    limit = request.args.get('limit', 50, type=int)
    trades = db.get_trades(model_id, limit=limit)
    return jsonify(trades)

@app.route('/api/models/<int:model_id>/conversations', methods=['GET'])
def get_conversations(model_id):
    limit = request.args.get('limit', 20, type=int)
    conversations = db.get_conversations(model_id, limit=limit)
    return jsonify(conversations)

@app.route('/api/aggregated/portfolio', methods=['GET'])
def get_aggregated_portfolio():
    """Get aggregated portfolio data across all models"""
    prices_data = market_fetcher.get_current_prices(['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE'])
    current_prices = {coin: prices_data[coin]['price'] for coin in prices_data}

    # Get aggregated data
    models = db.get_all_models()
    total_portfolio = {
        'total_value': 0,
        'cash': 0,
        'positions_value': 0,
        'realized_pnl': 0,
        'unrealized_pnl': 0,
        'initial_capital': 0,
        'positions': []
    }

    all_positions = {}

    for model in models:
        portfolio = db.get_portfolio(model['id'], current_prices)
        if portfolio:
            total_portfolio['total_value'] += portfolio.get('total_value', 0)
            total_portfolio['cash'] += portfolio.get('cash', 0)
            total_portfolio['positions_value'] += portfolio.get('positions_value', 0)
            total_portfolio['realized_pnl'] += portfolio.get('realized_pnl', 0)
            total_portfolio['unrealized_pnl'] += portfolio.get('unrealized_pnl', 0)
            total_portfolio['initial_capital'] += portfolio.get('initial_capital', 0)

            # Aggregate positions by coin and side
            for pos in portfolio.get('positions', []):
                key = f"{pos['coin']}_{pos['side']}"
                if key not in all_positions:
                    all_positions[key] = {
                        'coin': pos['coin'],
                        'side': pos['side'],
                        'quantity': 0,
                        'avg_price': 0,
                        'total_cost': 0,
                        'leverage': pos['leverage'],
                        'current_price': pos['current_price'],
                        'pnl': 0
                    }

                # Weighted average calculation
                current_pos = all_positions[key]
                current_cost = current_pos['quantity'] * current_pos['avg_price']
                new_cost = pos['quantity'] * pos['avg_price']
                total_quantity = current_pos['quantity'] + pos['quantity']

                if total_quantity > 0:
                    current_pos['avg_price'] = (current_cost + new_cost) / total_quantity
                    current_pos['quantity'] = total_quantity
                    current_pos['total_cost'] = current_cost + new_cost
                    current_pos['pnl'] = (pos['current_price'] - current_pos['avg_price']) * total_quantity

    total_portfolio['positions'] = list(all_positions.values())

    # Get multi-model chart data - 获取所有历史数据用于缩放查看
    chart_data = db.get_multi_model_chart_data(limit=100000)

    return jsonify({
        'portfolio': total_portfolio,
        'chart_data': chart_data,
        'model_count': len(models)
    })

@app.route('/api/models/chart-data', methods=['GET'])
def get_models_chart_data():
    """Get chart data for all models"""
    limit = request.args.get('limit', 100, type=int)
    chart_data = db.get_multi_model_chart_data(limit=limit)
    return jsonify(chart_data)

@app.route('/api/market/prices', methods=['GET'])
def get_market_prices():
    coins = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE']
    prices = market_fetcher.get_current_prices(coins)
    return jsonify(prices)

@app.route('/api/models/<int:model_id>/execute', methods=['POST'])
def execute_trading(model_id):
    if model_id not in trading_engines:
        model = db.get_model(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404

        # Get provider info
        provider = db.get_provider(model['provider_id'])
        if not provider:
            return jsonify({'error': 'Provider not found'}), 404

        provider_type = detect_provider_type(provider)

        trading_engines[model_id] = create_trading_engine(
            model_id=model_id,
            db=db,
            market_fetcher=market_fetcher,
            ai_trader=AITrader(
                provider_type=provider_type,
                api_key=provider['api_key'],
                api_url=provider['api_url'],
                model_name=model['model_name'],
                db=db,  # 传入db用于获取历史数据
                market_fetcher=market_fetcher  # 传入市场数据获取器
            )
        )
    
    try:
        result = trading_engines[model_id].execute_trading_cycle()
        return jsonify(result)
    except Exception as e:
        logger.exception("Manual execute for model %s failed", model_id)
        return jsonify({'error': str(e)}), 500

def trading_loop():
    logger.info("Trading loop started")
    
    while auto_trading:
        try:
            if not trading_engines:
                time.sleep(30)
                continue
            
            logger.info("=" * 60)
            logger.info("[CYCLE] %s", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            logger.info("[INFO] Active models: %s", len(trading_engines))
            logger.info("=" * 60)
            
            for model_id, engine in list(trading_engines.items()):
                try:
                    # 检查模型是否仍然存在
                    if db.get_model(model_id) is None:
                        logger.info("[SKIP] Model %s 已删除，跳过", model_id)
                        if model_id in trading_engines:
                            del trading_engines[model_id]
                        continue
                    
                    logger.info("[EXEC] Model %s", model_id)
                    result = engine.execute_trading_cycle()
                    
                    if result.get('success'):
                        logger.info("[OK] Model %s completed", model_id)
                        if result.get('executions'):
                            for exec_result in result['executions']:
                                signal = exec_result.get('signal', 'unknown')
                                coin = exec_result.get('coin', 'unknown')
                                msg = exec_result.get('message', '')
                                if signal != 'hold':
                                    logger.info("[TRADE] %s: %s", coin, msg)
                    else:
                        error = result.get('error', 'Unknown error')
                        logger.warning("Model %s failed: %s", model_id, error)
                        
                except Exception:
                    logger.exception("Model %s exception", model_id)
                    continue
            
            logger.info("=" * 60)
            logger.info(f"[SLEEP] Waiting {TradingConfig.get_trading_cycle_minutes()} minutes for next cycle (LOW FREQUENCY MODE)")
            logger.info("=" * 60)
            
            # 低频交易：使用配置的交易周期
            time.sleep(TradingConfig.TRADING_CYCLE_SECONDS)
            
        except Exception:
            logger.exception("[CRITICAL] Trading loop error")
            logger.info("[RETRY] Retrying in 60 seconds")
            time.sleep(60)
    
    logger.info("Trading loop stopped")

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    models = db.get_all_models()
    leaderboard = []
    
    prices_data = market_fetcher.get_current_prices(['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'DOGE'])
    current_prices = {coin: prices_data[coin]['price'] for coin in prices_data}
    
    for model in models:
        portfolio = db.get_portfolio(model['id'], current_prices)
        account_value = portfolio.get('total_value', model['initial_capital'])
        returns = ((account_value - model['initial_capital']) / model['initial_capital']) * 100
        
        # 获取性能指标
        metrics = performance_analyzer.get_performance_metrics(model['id'])
        
        leaderboard.append({
            'model_id': model['id'],
            'model_name': model['name'],
            'account_value': account_value,
            'returns': returns,
            'initial_capital': model['initial_capital'],
            'sharpe_ratio': metrics.get('sharpe_ratio', 0),
            'max_drawdown': metrics.get('max_drawdown', 0),
            'win_rate': metrics.get('win_rate', 0),
            'profit_factor': metrics.get('profit_factor', 0)
        })
    
    leaderboard.sort(key=lambda x: x['returns'], reverse=True)
    return jsonify(leaderboard)

@app.route('/api/models/<int:model_id>/performance', methods=['GET'])
def get_model_performance(model_id):
    """获取模型性能指标"""
    try:
        metrics = performance_analyzer.get_performance_metrics(model_id)
        return jsonify({
            'success': True,
            'metrics': metrics
        })
    except Exception as e:
        logger.exception(f"Get performance for model {model_id} failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/system/circuit-breakers', methods=['GET'])
def get_circuit_breakers():
    """获取所有熔断器状态"""
    try:
        states = circuit_manager.get_all_states()
        return jsonify({
            'success': True,
            'circuit_breakers': states
        })
    except Exception as e:
        logger.exception("Get circuit breakers failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/system/circuit-breakers/reset', methods=['POST'])
def reset_circuit_breakers():
    """重置所有或特定模型的熔断器"""
    try:
        data = request.json or {}
        model_id = data.get('model_id')
        
        if model_id:
            # 重置特定模型的熔断器
            if model_id in trading_engines:
                engine = trading_engines[model_id]
                if hasattr(engine.ai_trader, 'reset_circuit_breaker'):
                    engine.ai_trader.reset_circuit_breaker()
                    return jsonify({
                        'success': True, 
                        'message': f'Model {model_id} 熔断器已重置'
                    })
            return jsonify({'success': False, 'error': '模型不存在'}), 404
        else:
            # 重置所有熔断器
            circuit_manager.reset_all()
            return jsonify({
                'success': True,
                'message': 'All circuit breakers reset'
            })
    except Exception as e:
        logger.exception("Reset circuit breakers failed")
        return jsonify({'error': str(e)}), 500

# ============ OKX 交易所 API ============

@app.route('/api/okx/status', methods=['GET'])
def get_okx_status():
    """获取 OKX 交易所连接状态"""
    try:
        exchange = get_okx_exchange()
        connected = exchange.test_connection(try_backup=True)
        
        # 获取详细连接状态
        connection_status = exchange.get_connection_status()
        
        return jsonify({
            'connected': connected,
            'demo_trading': TradingConfig.OKX_DEMO_TRADING,
            'real_trading_enabled': TradingConfig.ENABLE_REAL_TRADING,
            'api_configured': bool(TradingConfig.OKX_API_KEY),
            'margin_mode': TradingConfig.OKX_MARGIN_MODE,
            'inst_type': TradingConfig.OKX_INST_TYPE,
            'current_url': connection_status.get('current_url'),
            'backup_url': getattr(TradingConfig, 'OKX_API_URL_BACKUP', 'https://aws.okx.com'),
            'consecutive_failures': connection_status.get('consecutive_failures', 0),
            'has_cached_data': connection_status.get('has_cached_data', False),
        })
    except Exception as e:
        logger.exception("Get OKX status failed")
        return jsonify({
            'error': str(e), 
            'connected': False,
            'hint': '连接失败，可能需要检查网络或切换到备用API域名'
        }), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查端点"""
    return jsonify({
        'status': 'ok',
        'server': 'running',
        'real_trading_enabled': TradingConfig.ENABLE_REAL_TRADING
    })

@app.route('/api/okx/switch-url', methods=['POST'])
def switch_okx_url():
    """手动切换 OKX API URL"""
    try:
        if not TradingConfig.ENABLE_REAL_TRADING:
            return jsonify({'error': '真实交易未启用'}), 400
        
        data = request.json or {}
        use_backup = data.get('use_backup', None)
        
        exchange = get_okx_exchange()
        
        if use_backup is not None:
            # 明确指定使用主/备 URL
            if use_backup:
                exchange.base_url = exchange.backup_url
            else:
                exchange.base_url = exchange.primary_url
        else:
            # 切换到另一个 URL
            if exchange.base_url == exchange.primary_url:
                exchange.base_url = exchange.backup_url
            else:
                exchange.base_url = exchange.primary_url
        
        # 测试新 URL
        connected = exchange.test_connection(try_backup=False)
        
        return jsonify({
            'success': True,
            'current_url': exchange.base_url,
            'connected': connected,
            'message': f"已切换到 {exchange.base_url}" + (" (连接成功)" if connected else " (连接失败)")
        })
    except Exception as e:
        logger.exception("Switch OKX URL failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/okx/account', methods=['GET'])
def get_okx_account():
    """获取 OKX 账户信息"""
    try:
        if not TradingConfig.ENABLE_REAL_TRADING:
            # 返回 200 而不是 400，让前端能正常处理
            return jsonify({
                'success': False,
                'error': '真实交易未启用',
                'balance': {
                    'success': False,
                    'total_equity': 0,
                    'available_balance': 0
                },
                'positions': []
            })
        
        exchange = get_okx_exchange()
        balance = exchange.get_account_balance(use_cache_on_fail=True)
        positions = exchange.get_positions(use_cache_on_fail=True)
        
        # 检查是否使用了缓存数据
        from_cache = balance.get('from_cache', False)
        
        # 确保余额数据格式正确
        if not balance.get('success', False) and not from_cache:
            logger.warning(f"获取余额失败: {balance.get('error', '未知错误')}")
            balance = {
                'success': False,
                'total_equity': 0,
                'available_balance': 0,
                'error': balance.get('error', '获取余额失败')
            }
        elif from_cache:
            logger.info(f"使用缓存数据: 余额=${balance.get('total_equity', 0):.2f}")
        
        return jsonify({
            'success': True,
            'balance': balance,
            'positions': positions or [],
            'from_cache': from_cache,
            'cache_warning': '数据来自缓存，可能不是最新的' if from_cache else None
        })
    except Exception as e:
        logger.exception("Get OKX account failed")
        return jsonify({
            'success': False,
            'error': str(e),
            'balance': {
                'success': False,
                'total_equity': 0,
                'available_balance': 0
            },
            'positions': []
        }), 500

@app.route('/api/okx/positions', methods=['GET'])
def get_okx_positions():
    """获取 OKX 持仓"""
    try:
        if not TradingConfig.ENABLE_REAL_TRADING:
            return jsonify({'error': '真实交易未启用'}), 400
        
        exchange = get_okx_exchange()
        positions = exchange.get_positions()
        
        return jsonify({
            'success': True,
            'positions': positions
        })
    except Exception as e:
        logger.exception("Get OKX positions failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/okx/close-all', methods=['POST'])
def okx_close_all_positions():
    """OKX 一键平仓"""
    try:
        if not TradingConfig.ENABLE_REAL_TRADING:
            return jsonify({'error': '真实交易未启用'}), 400
        
        exchange = get_okx_exchange()
        results = exchange.close_all_positions()
        
        return jsonify({
            'success': True,
            'results': results
        })
    except Exception as e:
        logger.exception("OKX close all positions failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/okx/ticker/<coin>', methods=['GET'])
def get_okx_ticker(coin):
    """获取 OKX 实时行情"""
    try:
        exchange = get_okx_exchange()
        ticker = exchange.get_ticker(coin.upper())
        
        if ticker:
            return jsonify({'success': True, 'ticker': ticker})
        else:
            return jsonify({'error': f'无法获取 {coin} 行情'}), 404
    except Exception as e:
        logger.exception(f"Get OKX ticker {coin} failed")
        return jsonify({'error': str(e)}), 500

# ============ 交易状态与安全控制 API ============

@app.route('/api/trading/status', methods=['GET'])
def get_trading_status():
    """获取交易系统状态"""
    try:
        allowed, reason = TradingConfig.is_trading_allowed()
        
        return jsonify({
            'trading_allowed': allowed,
            'reason': reason,
            'mode': '真实交易' if TradingConfig.ENABLE_REAL_TRADING else '模拟交易',
            'okx_demo': TradingConfig.OKX_DEMO_TRADING,
            'emergency_stop': TradingConfig.EMERGENCY_STOP,
            'conservative_mode': TradingConfig.REAL_TRADING_CONSERVATIVE,
            'effective_params': {
                'max_leverage': TradingConfig.get_effective_max_leverage(),
                'risk_range': TradingConfig.get_effective_risk_per_trade(),
                'confidence_threshold': TradingConfig.get_effective_confidence_threshold(),
                'max_positions': TradingConfig.get_effective_max_positions(),
            },
            'safety_limits': {
                'max_daily_loss_pct': TradingConfig.MAX_DAILY_LOSS_PCT,
                'max_total_loss_pct': TradingConfig.MAX_TOTAL_LOSS_PCT,
                'max_daily_trades': TradingConfig.MAX_DAILY_TRADES,
            }
        })
    except Exception as e:
        logger.exception("Get trading status failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/emergency-stop', methods=['POST'])
def emergency_stop():
    """紧急停止交易"""
    global auto_trading
    try:
        data = request.json or {}
        action = data.get('action', 'stop')  # 'stop' 或 'resume'
        close_positions = data.get('close_positions', False)  # 是否平仓
        
        if action == 'stop':
            TradingConfig.EMERGENCY_STOP = True
            auto_trading = False
            message = '紧急停止已启用，所有交易已暂停'
            
            # 如果需要平仓
            if close_positions and TradingConfig.ENABLE_REAL_TRADING:
                exchange = get_okx_exchange()
                close_results = exchange.close_all_positions()
                message += f'，已平仓 {len(close_results)} 个持仓'
            
            logger.warning(f"[紧急停止] {message}")
            
        elif action == 'resume':
            TradingConfig.EMERGENCY_STOP = False
            auto_trading = True
            message = '紧急停止已解除，交易已恢复'
            logger.info(f"[紧急停止解除] {message}")
        else:
            return jsonify({'error': f'无效的操作: {action}'}), 400
        
        return jsonify({
            'success': True,
            'message': message,
            'emergency_stop': TradingConfig.EMERGENCY_STOP,
            'auto_trading': auto_trading
        })
        
    except Exception as e:
        logger.exception("Emergency stop failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/trading/config', methods=['GET'])
def get_trading_config():
    """获取当前交易配置"""
    try:
        return jsonify({
            'trading_mode': {
                'real_trading': TradingConfig.ENABLE_REAL_TRADING,
                'demo_trading': TradingConfig.OKX_DEMO_TRADING,
                'conservative_mode': TradingConfig.REAL_TRADING_CONSERVATIVE,
            },
            'risk_params': {
                'max_leverage': TradingConfig.get_effective_max_leverage(),
                'base_risk': TradingConfig.get_effective_risk_per_trade()[0],
                'max_risk': TradingConfig.get_effective_risk_per_trade()[1],
                'max_positions': TradingConfig.get_effective_max_positions(),
                'confidence_threshold': TradingConfig.get_effective_confidence_threshold(),
            },
            'safety': {
                'emergency_stop': TradingConfig.EMERGENCY_STOP,
                'max_daily_loss': TradingConfig.MAX_DAILY_LOSS_PCT,
                'max_total_loss': TradingConfig.MAX_TOTAL_LOSS_PCT,
                'max_daily_trades': TradingConfig.MAX_DAILY_TRADES,
            },
            'trading_coins': TradingConfig.TRADING_COINS,
            'cycle_seconds': TradingConfig.TRADING_CYCLE_SECONDS,
            'cooldown_seconds': TradingConfig.COOLDOWN_PERIOD_SECONDS,
        })
    except Exception as e:
        logger.exception("Get trading config failed")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings', methods=['GET'])
def get_settings():
    """Get system settings"""
    try:
        settings = db.get_settings()
        return jsonify(settings)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings', methods=['PUT'])
def update_settings():
    """Update system settings"""
    try:
        data = request.json
        trading_frequency_minutes = int(data.get('trading_frequency_minutes', 60))
        trading_fee_rate = float(data.get('trading_fee_rate', 0.001))

        success = db.update_settings(trading_frequency_minutes, trading_fee_rate)

        if success:
            return jsonify({'success': True, 'message': 'Settings updated successfully'})
        else:
            return jsonify({'success': False, 'error': 'Failed to update settings'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/version', methods=['GET'])
def get_version():
    """Get current version information"""
    return jsonify({
        'current_version': __version__,
        'github_repo': GITHUB_REPO_URL,
        'latest_release_url': LATEST_RELEASE_URL
    })

@app.route('/api/check-update', methods=['GET'])
def check_update():
    """Check for GitHub updates"""
    try:
        import requests

        # Get latest release from GitHub
        headers = {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'AITradeGame/1.0'
        }

        # Try to get latest release
        try:
            response = requests.get(
                f"https://api.github.com/repos/{__github_owner__}/{__repo__}/releases/latest",
                headers=headers,
                timeout=5
            )

            if response.status_code == 200:
                release_data = response.json()
                latest_version = release_data.get('tag_name', '').lstrip('v')
                release_url = release_data.get('html_url', '')
                release_notes = release_data.get('body', '')

                # Compare versions
                is_update_available = compare_versions(latest_version, __version__) > 0

                return jsonify({
                    'update_available': is_update_available,
                    'current_version': __version__,
                    'latest_version': latest_version,
                    'release_url': release_url,
                    'release_notes': release_notes,
                    'repo_url': GITHUB_REPO_URL
                })
            else:
                # If API fails, still return current version info
                return jsonify({
                    'update_available': False,
                    'current_version': __version__,
                    'error': 'Could not check for updates'
                })
        except Exception as e:
            logger.warning("GitHub API error: %s", e)
            return jsonify({
                'update_available': False,
                'current_version': __version__,
                'error': 'Network error checking updates'
            })

    except Exception as e:
        logger.exception("Check update failed")
        return jsonify({
            'update_available': False,
            'current_version': __version__,
            'error': str(e)
        }), 500

def compare_versions(version1, version2):
    """Compare two version strings.

    Returns:
        1 if version1 > version2
        0 if version1 == version2
        -1 if version1 < version2
    """
    def normalize(v):
        # Extract numeric parts from version string
        parts = re.findall(r'\d+', v)
        # Pad with zeros to make them comparable
        return [int(p) for p in parts]

    v1_parts = normalize(version1)
    v2_parts = normalize(version2)

    # Pad shorter version with zeros
    max_len = max(len(v1_parts), len(v2_parts))
    v1_parts.extend([0] * (max_len - len(v1_parts)))
    v2_parts.extend([0] * (max_len - len(v2_parts)))

    # Compare
    if v1_parts > v2_parts:
        return 1
    elif v1_parts < v2_parts:
        return -1
    else:
        return 0

def init_trading_engines():
    try:
        models = db.get_all_models()

        if not models:
            logger.warning("No trading models found")
            return

        logger.info("[INIT] Initializing trading engines...")
        for model in models:
            model_id = model['id']
            model_name = model['name']

            try:
                # Get provider info
                provider = db.get_provider(model['provider_id'])
                if not provider:
                    logger.warning("Model %s (%s): Provider not found", model_id, model_name)
                    continue

                provider_type = detect_provider_type(provider)

                trading_engines[model_id] = create_trading_engine(
                    model_id=model_id,
                    db=db,
                    market_fetcher=market_fetcher,
                    ai_trader=AITrader(
                        provider_type=provider_type,
                        api_key=provider['api_key'],
                        api_url=provider['api_url'],
                        model_name=model['model_name'],
                        db=db,  # 传入db用于获取历史数据
                        market_fetcher=market_fetcher  # 传入市场数据获取器
                    )
                )
                trading_mode = "真实交易" if TradingConfig.ENABLE_REAL_TRADING else "模拟交易"
                logger.info(f"Model {model_id} ({model_name}) initialized [{trading_mode}]")
            except Exception:
                logger.exception("Model %s (%s) init failed", model_id, model_name)
                continue

        logger.info("Initialized %s engine(s)", len(trading_engines))

    except Exception:
        logger.exception("Init engines failed")

if __name__ == '__main__':
    import webbrowser
    import os
    
    logger.info("=" * 60)
    logger.info("AITradeGame - Starting...")
    logger.info("=" * 60)
    logger.info("Initializing database...")
    
    db.init_db()
    
    logger.info("Database initialized")
    logger.info("Initializing trading engines...")
    
    init_trading_engines()
    
    if auto_trading:
        trading_thread = threading.Thread(target=trading_loop, daemon=True)
        trading_thread.start()
        logger.info("Auto-trading enabled")

    logger.info("=" * 60)
    logger.info("AITradeGame is running!")
    logger.info("Server: http://localhost:5000")
    logger.info("Press Ctrl+C to stop")
    logger.info("=" * 60)
    
    # 自动打开浏览器
    def open_browser():
        time.sleep(1.5)  # 等待服务器启动
        url = "http://localhost:5000"
        try:
            webbrowser.open(url)
            logger.info("Browser opened: %s", url)
        except Exception as e:
            logger.warning("Could not open browser: %s", e)
    
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    app.run(debug=False, host='0.0.0.0', port=5003, use_reloader=False)
