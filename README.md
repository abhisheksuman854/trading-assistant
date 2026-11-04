# TradeLens AI: Indian Stock Decision Support & Trade Shortlist Assistant

TradeLens AI is an AI-powered quantitative market intelligence and decision support platform for the Indian stock market (NSE / BSE / F&O).

Rather than outputting generic "Buy" tips, TradeLens AI acts as a research analyst sitting beside you every morning: scanning the market, filtering high-conviction opportunities, explaining the supporting evidence, calculating empirical win probabilities, and enforcing hard "Why NOT to Trade" gates to protect your capital.

---

## ⚡ Quick Start (Zero Infrastructure / ₹0 Cost)

This project has been built to run immediately on macOS with **Python 3.12** without requiring any paid APIs or cloud subscriptions.

### 1. Run the Terminal Scanner (Instant Shortlist)
Run the automated trade shortlisting scanner directly in your terminal:
```bash
/opt/homebrew/bin/python3.12 backend/cli_scanner.py
```
This scans the universe, calculates technical and fundamental scores, runs Black-Scholes option Greeks and OI buildup checks, identifies vetoed setups (e.g., earnings risk), and checks open positions for trailing exits.

### 2. Start the Backend API Server
Launch the lightweight REST API server:
```bash
/opt/homebrew/bin/python3.12 backend/app/main.py
```
The API starts at `http://127.0.0.1:8000`. Available endpoints:
- `GET /api/scan`: Runs universe scan and returns shortlisted swing trades, options, and vetoed setups.
- `GET /api/stock/{SYMBOL}`: Deep-dive profile for a specific stock (e.g., `/api/stock/RELIANCE`).
- `POST /api/positions/monitor`: Monitors open user positions for target hits, stop breaches, and trailing stops.

### 3. Open the Interactive Web Dashboard
Open `frontend/index.html` in your browser (e.g. Chrome, Safari):
```bash
open frontend/index.html
```
The dashboard connects directly to your local backend API at `http://127.0.0.1:8000/api/scan`, and includes a high-fidelity local fallback so it functions completely even if the backend is offline.

---

## 🧠 The 6 Core Intelligence Engines

1. **Trade Shortlisting Engine (`app/core/shortlist_engine.py`)**:
   - Scans 1,500+ NSE stocks (or the F&O universe).
   - Generates the **Top 5-10 Shortlisted Setups** with Entry Range, Target 1, Target 2, Stop-Loss, and Risk/Reward ratio.
   - Monitors open user positions for exit directives and profit-locking trailing stops.

2. **"Why NOT to Trade" Gatekeeper**:
   - Rejects stocks with earnings announcements in $< 48$ hours.
   - Rejects chasing when price is overextended from 20 EMA ($> 7\%$ or $> 2.5\times\text{ATR}$).
   - Rejects false breakouts occurring on below-average volume ($\text{RVOL} < 0.7\times$).
   - Rejects trades where Risk-to-Reward is $< 1:1.5$.

3. **Fundamental Analysis & Valuation Engine (`app/core/fundamentals.py`)**:
   - 2-Stage Discounted Cash Flow (DCF) intrinsic value (Conservative, Base, Optimistic).
   - Margin of Safety calculation ($\%$ upside to intrinsic value).
   - Benjamin Graham Number ($\sqrt{22.5 \times \text{EPS} \times \text{BVPS}}$).
   - Debt-to-Equity, Interest Coverage Ratio (ICR), Current Ratio, Quick Ratio.
   - Profitability: ROE, ROCE, 3-Yr Revenue & PAT Growth.
   - Cash conversion: Operating Cash Flow (CFO), Free Cash Flow (FCF), CFO/PAT ratio.
   - Dedicated Banking/NBFC module: CRAR %, Gross NPA %, Net NPA %, NIM %, CASA %.

4. **Technical Analysis Engine (`app/core/technicals.py`)**:
   - Moving average ribbons: EMA 9, 20, 50, 200.
   - Wilder's RSI (14) with momentum zone detection ($55-70$).
   - MACD (12, 26, 9) signal line and histogram crossovers.
   - Relative Volume (RVOL) against 20-day SMA.
   - Average True Range (ATR) & Classic / Camarilla Pivot Points.
   - Candlestick pattern recognition: Bullish Engulfing, Hammer/Pin Bar, Inside Bar.

5. **Derivatives & Options Intelligence Engine (`app/core/options_analytics.py`)**:
   - Black-Scholes option pricing and Greeks (Delta, Gamma, Theta, Vega).
   - Strike Selection Matrix: Side-by-side comparison of ITM, ATM, and OTM strikes.
   - Open Interest (OI) buildup classification: Long Buildup, Short Covering, Short Buildup, Long Unwinding.
   - Total & Strike-level Put-Call Ratio (PCR).
   - Sudden OI shift alerts (e.g. Call writing at resistance or Put writing at support).
   - Estimated payoff and capital required per lot.

6. **Historical Probability & Expectancy Engine (`app/core/probability.py`)**:
   - Target-first vs. Stop-first empirical win rate.
   - Sample size transparency (e.g., $60.6\%$ target-first rate across 350 historical setups).
   - Mathematical Net Expectancy per trade after accounting for brokerage, STT, and slippage.

---

## 📁 Project Structure

```
tradelens-ai/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                  # Zero-dependency API server
│   │   ├── core/
│   │   │   ├── technicals.py        # EMA, RSI, MACD, ATR, RVOL, Pivots
│   │   │   ├── fundamentals.py      # DCF, Graham, Solvency, Banking CRAR/NPA
│   │   │   ├── options_analytics.py # Black-Scholes Greeks, Strike matrix, OI
│   │   │   ├── suitability.py       # 6-Bucket instrument classifier
│   │   │   ├── probability.py       # Historical expectancy & sample sizes
│   │   │   └── shortlist_engine.py  # Multi-factor ranking & position monitor
│   │   └── data/
│   │       ├── base.py              # MarketDataProvider interface
│   │       └── sample_universe.py   # Indian stock universe with F&O chains
│   ├── cli_scanner.py               # Interactive CLI shortlisting assistant
│   ├── test_api.py                  # Automated test suite
│   └── requirements.txt             # Optional external libraries (yfinance, fastapi)
├── frontend/
│   └── index.html                   # Interactive dark-theme dashboard
└── README.md
```

---

## 🔌 Connecting Live Broker APIs (Next Steps)
The data layer is provider-independent (`MarketDataProvider` in `backend/app/data/base.py`). To connect live data:
1. **Angel One SmartAPI**: Plug your API key and WebSocket client into `backend/app/data/angel_one.py`.
2. **Zerodha Kite Connect**: Plug your Kite session into `backend/app/data/zerodha.py`.
3. **Yahoo Finance**: Install `yfinance` (`pip install yfinance`) to pull live daily/intraday candles for any `.NS` ticker.

