# 📈 Algo Trading — Groww API

A Python-based algorithmic trading system built on top of the **Groww API**. This project enables automated trading strategies, real-time market data fetching, order management, and portfolio tracking — all from Python.

---

## 🚀 Features

- 🔐 OAuth-based authentication with Groww API
- 📊 Real-time and historical market data fetching
- 📋 Order placement, modification, and cancellation
- 💼 Portfolio and holdings management
- 🤖 Pluggable strategy framework (momentum, mean-reversion, etc.)
- 📉 Risk management & position sizing utilities
- 📦 Modular, extensible architecture

---

## 🛠️ Tech Stack

| Layer         | Technology                    |
| ------------- | ----------------------------- |
| Language      | Python 3.10+                  |
| Broker API    | [Groww API](https://groww.in) |
| HTTP Client   | `requests` / `httpx`          |
| Data Analysis | `pandas`, `numpy`             |
| Scheduling    | `APScheduler` / `schedule`    |
| Config        | `python-dotenv`               |

---

## 📁 Project Structure

```
algo-trading/
├── src/
│   ├── api/                  # Groww API client wrappers
│   │   ├── auth.py           # Authentication & token management
│   │   ├── market_data.py    # Quotes, OHLC, order book
│   │   └── orders.py         # Order placement & management
│   ├── strategies/           # Trading strategy implementations
│   │   ├── base_strategy.py  # Abstract base class
│   │   ├── momentum.py       # Momentum strategy
│   │   └── mean_reversion.py # Mean reversion strategy
│   ├── risk/                 # Risk management
│   │   └── position_sizing.py
│   ├── utils/                # Helpers & utilities
│   │   ├── logger.py
│   │   └── config.py
│   └── main.py               # Entry point
├── tests/                    # Unit & integration tests
├── notebooks/                # Research & backtesting notebooks
├── .env.example              # Environment variable template
├── requirements.txt
└── README.md
```

---

## ⚙️ Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/algo-trading.git
cd algo-trading
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # macOS/Linux
# venv\Scripts\activate         # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your Groww API credentials:

```env
GROWW_API_KEY=your_api_key_here
GROWW_SECRET_KEY=your_secret_key_here
GROWW_CLIENT_ID=your_client_id_here
```

> ⚠️ **Never commit your `.env` file.** It is already included in `.gitignore`.

---

## 🧪 Running the Bot

```bash
python src/main.py
```

To run in paper trading (dry run) mode:

```bash
DRY_RUN=true python src/main.py
```

---

## 🔑 Groww API Authentication

This project uses Groww's API for executing trades. To get started:

1. Register / log in at [groww.in](https://groww.in)
2. Navigate to **Profile → API Access** (or contact Groww support for API credentials)
3. Generate your **API Key** and **Secret Key**
4. Add them to your `.env` file

---

## 📊 Example Strategy

```python
from src.strategies.base_strategy import BaseStrategy

class MyStrategy(BaseStrategy):
    def should_buy(self, symbol: str) -> bool:
        data = self.market_data.get_ohlc(symbol, period="1D")
        return data["close"].iloc[-1] > data["close"].rolling(20).mean().iloc[-1]

    def should_sell(self, symbol: str) -> bool:
        data = self.market_data.get_ohlc(symbol, period="1D")
        return data["close"].iloc[-1] < data["close"].rolling(20).mean().iloc[-1]
```

---

## ⚠️ Disclaimer

> This project is for **educational and research purposes only**. Algorithmic trading involves significant financial risk. The authors are not responsible for any financial losses. Always test thoroughly in a paper trading environment before deploying real capital.

---

## 📜 License

[MIT License](LICENSE)

---

## 🤝 Contributing

Pull requests are welcome! Please open an issue first to discuss what you'd like to change.

1. Fork the repo
2. Create your branch: `git checkout -b feature/my-strategy`
3. Commit your changes: `git commit -m 'Add my strategy'`
4. Push to the branch: `git push origin feature/my-strategy`
5. Open a Pull Request
