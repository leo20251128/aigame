# 🤖 AITradeGame

<div align="center">

![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.8+-green.svg)
![License](https://img.shields.io/badge/license-MIT-orange.svg)

**AI 驱动的加密货币量化交易系统**

*使用大语言模型（LLM）进行智能交易决策，支持 OKX 永续合约真实交易与模拟交易*

[English](#english) | [中文](#中文)

</div>

---

## 中文

### ✨ 功能特点

| 特性 | 描述 |
|------|------|
| 🧠 **AI 智能决策** | 使用 OpenAI/Claude/DeepSeek 等 LLM 分析市场数据并做出交易决策 |
| 📊 **实时市场数据** | 从 Binance/CoinGecko 获取实时价格、K线和技术指标 |
| 💹 **OKX 真实交易** | 支持 OKX 永续合约的真实交易，自动下单、止盈止损 |
| 🛡️ **风险管理** | 动态止盈止损、熔断器保护、日亏损上限 |
| 📈 **仓位管理** | 根据波动率和置信度自动调整仓位大小和杠杆 |
| 🌐 **Web 界面** | 直观的交易面板、实时图表、持仓监控 |
| 🔄 **高可用性** | 自动切换备用 API、连接缓存、优雅降级 |

---

### 🚀 快速开始

#### 1. 克隆仓库

```bash
git clone https://github.com/chadyi/AITradeGame.git
cd AITradeGame
```

#### 2. 安装依赖

```bash
pip install -r requirements.txt
```

#### 3. 配置系统

**方式一：编辑配置文件**

编辑 `config.yaml` 文件，填入你的 API 密钥：

```yaml
okx:
  api_key: "your-api-key"
  secret_key: "your-secret-key"
  passphrase: "your-passphrase"
  enable_real_trading: true
```

**方式二：使用环境变量（推荐）**

```bash
export OKX_API_KEY="your-api-key"
export OKX_SECRET_KEY="your-secret-key"
export OKX_PASSPHRASE="your-passphrase"
```

#### 4. 启动系统

```bash
python app.py
```

打开浏览器访问 **http://localhost:5000**

---

### ⚙️ 配置说明

所有配置都在 `config.yaml` 文件中，支持热修改后重启生效。

#### OKX 交易所配置

```yaml
okx:
  enable_real_trading: true        # 是否启用真实交易
  api_url: "https://www.okx.com"   # 主 API 地址
  api_url_backup: "https://aws.okx.com"  # 备用 API（网络不稳定时）
  auto_switch_url: true            # 自动切换备用 URL
  demo_trading: false              # 是否使用 OKX 模拟盘
  margin_mode: "isolated"          # 保证金模式：isolated/cross
```

#### 交易参数

```yaml
trading:
  cycle_seconds: 900               # 交易周期（秒），建议 15 分钟
  cooldown_seconds: 2700           # 冷却期（秒），避免频繁交易
  coins:                           # 交易币种
    - "BTC"
    - "ETH"
    - "BNB"
    - "XRP"
    - "DOGE"
```

#### AI 决策参数

```yaml
ai:
  min_confidence: 0.80             # 最低置信度阈值（0-1）
  max_positions: 2                 # 最大同时持仓数
  max_new_positions_per_cycle: 1   # 每周期最多开仓数
```

#### 杠杆配置

```yaml
leverage:
  default: 3                       # 默认杠杆
  max: 5                           # 最大杠杆（建议不超过 5 倍）
  min: 1                           # 最小杠杆
```

#### 风险控制

```yaml
risk:
  base_risk_per_trade: 0.08        # 单笔风险比例 8%
  max_trade_value_pct: 0.40        # 单笔最大占比 40%
  max_volatility_threshold: 80     # 最大波动率阈值

safety:
  max_daily_loss_pct: 0.10         # 日亏损上限 10%
  max_total_loss_pct: 0.15         # 总亏损上限 15%
  max_daily_trades: 50             # 日交易次数上限
```

#### 止盈止损

```yaml
take_profit:
  enabled: true
  quick_profit_threshold: 0.10     # 盈利 10% 立即全平
  rules:                           # 阶梯止盈
    - [0.08, 1.0, "盈利8%全平"]
    - [0.05, 0.50, "盈利5%平半仓"]
    - [0.03, 0.30, "盈利3%平30%"]

stop_loss:
  default_pct: 0.08                # 默认止损 8%
  max_pct: 0.12                    # 最大止损 12%
```

---

### 🐳 Docker 部署

#### 使用 docker-compose

```bash
docker-compose up -d
```

#### 环境变量配置

```yaml
# docker-compose.yml
services:
  aitradegame:
    environment:
      - OKX_API_KEY=your-api-key
      - OKX_SECRET_KEY=your-secret-key
      - OKX_PASSPHRASE=your-passphrase
```

---

### 📡 API 接口

#### 系统状态

| 接口 | 方法 | 描述 |
|------|------|------|
| `/api/health` | GET | 健康检查 |
| `/api/okx/status` | GET | OKX 连接状态 |
| `/api/okx/switch-url` | POST | 切换 API URL |

#### 交易管理

| 接口 | 方法 | 描述 |
|------|------|------|
| `/api/models` | GET | 获取所有交易模型 |
| `/api/models` | POST | 创建交易模型 |
| `/api/models/<id>/portfolio` | GET | 获取持仓 |
| `/api/models/<id>/execute` | POST | 手动执行交易 |
| `/api/models/<id>/close-all-positions` | POST | 一键平仓 |

#### 市场数据

| 接口 | 方法 | 描述 |
|------|------|------|
| `/api/market/prices` | GET | 获取市场价格 |
| `/api/leaderboard` | GET | 模型排行榜 |

---

### 📁 项目结构

```
AITradeGame/
├── app.py                 # Flask 主应用入口
├── config.yaml            # 系统配置文件（超参数调优）
├── trading_config.py      # 配置加载器
│
├── ai_trader.py           # AI 交易决策核心
├── trading_engine.py      # 模拟交易引擎
├── real_trading_engine.py # OKX 真实交易引擎
├── okx_exchange.py        # OKX 交易所 API 适配器
│
├── market_data.py         # 市场数据获取（Binance/CoinGecko）
├── database.py            # SQLite 数据库操作
├── risk_manager.py        # 风险管理模块
├── circuit_breaker.py     # 熔断器保护机制
├── version.py             # 版本信息
│
├── static/                # 前端静态文件
│   ├── app.js
│   └── style.css
├── templates/
│   └── index.html
│
├── logs/                  # 日志目录
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

### 🔧 常见问题

#### Q: OKX API 连接超时怎么办？

系统会自动切换到备用 API（`aws.okx.com`）。也可手动切换：

```bash
curl -X POST http://localhost:5000/api/okx/switch-url \
  -H "Content-Type: application/json" \
  -d '{"use_backup": true}'
```

或修改配置：

```yaml
okx:
  api_url: "https://aws.okx.com"
```

#### Q: 如何添加新的交易币种？

修改 `config.yaml` 中的 `trading.coins` 列表：

```yaml
trading:
  coins:
    - "BTC"
    - "ETH"
    - "SOL"    # 新增
    - "AVAX"   # 新增
```

#### Q: 如何调整交易频率？

修改 `trading.cycle_seconds`（交易周期）和 `trading.cooldown_seconds`（冷却期）：

```yaml
trading:
  cycle_seconds: 1800      # 30 分钟一个周期
  cooldown_seconds: 3600   # 1 小时冷却
```

#### Q: 数据库如何重置？

删除 `AITradeGame.db` 文件，重启系统会自动创建新数据库。

---

### ⚠️ 风险提示

> **重要警告**
> 
> 1. 🚨 加密货币交易具有**极高风险**，可能导致全部本金损失
> 2. 📚 本系统**仅供学习和研究**使用，不构成任何投资建议
> 3. 🧪 真实交易前请先使用**模拟盘**充分测试
> 4. 💰 永远**不要**投入超过你能承受损失的资金
> 5. 📉 杠杆交易会**放大亏损**，请谨慎使用

---

## English

### ✨ Features

| Feature | Description |
|---------|-------------|
| 🧠 **AI-Powered Decisions** | Uses LLMs (OpenAI/Claude/DeepSeek) to analyze market data and make trading decisions |
| 📊 **Real-time Market Data** | Fetches prices, candlesticks, and technical indicators from Binance/CoinGecko |
| 💹 **OKX Real Trading** | Supports real trading on OKX perpetual contracts with auto order placement |
| 🛡️ **Risk Management** | Dynamic take-profit/stop-loss, circuit breakers, daily loss limits |
| 📈 **Position Sizing** | Auto-adjusts position size and leverage based on volatility and confidence |
| 🌐 **Web Interface** | Intuitive dashboard with real-time charts and position monitoring |
| 🔄 **High Availability** | Auto-switches to backup API, connection caching, graceful degradation |

---

### 🚀 Quick Start

#### 1. Clone the repository

```bash
git clone https://github.com/chadyi/AITradeGame.git
cd AITradeGame
```

#### 2. Install dependencies

```bash
pip install -r requirements.txt
```

#### 3. Configure the system

Edit `config.yaml` or use environment variables:

```bash
export OKX_API_KEY="your-api-key"
export OKX_SECRET_KEY="your-secret-key"
export OKX_PASSPHRASE="your-passphrase"
```

#### 4. Start the system

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

---

### ⚙️ Configuration

All settings are in `config.yaml`. Key sections:

- **okx**: Exchange API settings
- **trading**: Trading pairs, cycle time, cooldown
- **ai**: Confidence threshold, max positions
- **leverage**: Leverage rules based on volatility
- **risk**: Position sizing, max risk per trade
- **take_profit/stop_loss**: Exit rules
- **safety**: Daily loss limits, emergency stop

---

### 🐳 Docker Deployment

```bash
docker-compose up -d
```

---

### ⚠️ Risk Warning

> **IMPORTANT**
> 
> 1. 🚨 Cryptocurrency trading involves **significant risk** of loss
> 2. 📚 This system is for **educational purposes only** - not financial advice
> 3. 🧪 Test thoroughly on **demo accounts** before real trading
> 4. 💰 Never invest more than you can afford to lose
> 5. 📉 Leverage **amplifies losses** - use with caution

---

### 📄 License

MIT License - See [LICENSE](LICENSE) for details.

---

<div align="center">

**Made with ❤️ by [chadyi](https://github.com/chadyi)**

</div>
