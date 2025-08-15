import pandas as pd
import time
import logging
from flask import Flask, jsonify
from threading import Thread, Lock
from binance.client import Client
from datetime import datetime

# Binance API Keys
API_KEY = ''
API_SECRET = ''

# Telegram Bot Details
TELEGRAM_TOKEN = ''
CHAT_ID = ''

# Logging Setup
logging.basicConfig(filename='step11_live_telegram_feed.log',
                    level=logging.INFO,
                    format='%(asctime)s - %(message)s')

# Flask App
app = Flask(__name__)

# Shared Data & Locks
signals_data = {}
last_sent_signals = {}
signal_lock = Lock()
app_status = {'status': 'running'}

# Binance Client
client = Client(API_KEY, API_SECRET)

# Configuration
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT']
CHANGE_THRESHOLD_PERCENT = 0.5
REPORT_INTERVAL_MINUTES = 60


# Function: Fetch Realtime Binance Prices
def fetch_binance_data():
    data = {}
    for symbol in SYMBOLS:
        try:
            ticker = client.get_ticker(symbol=symbol)
            data[symbol] = {
                'price': float(ticker['lastPrice']),
                'price_change_percent': float(ticker['priceChangePercent']),
                'volume': float(ticker['volume'])
            }
        except Exception as e:
            logging.error(f"Error fetching {symbol}: {str(e)}")
    return data


# Function: Check if Signal Should Be Sent
def should_send_signal(symbol, new_data):
    with signal_lock:
        last_data = last_sent_signals.get(symbol)
        if last_data is None:
            last_sent_signals[symbol] = new_data
            return True
        price_diff = abs(new_data['price_change_percent'] - last_data['price_change_percent'])
        if price_diff >= CHANGE_THRESHOLD_PERCENT:
            last_sent_signals[symbol] = new_data
            return True
    return False


# Function: Send Telegram Message
def send_telegram_message(text):
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Telegram send error: {str(e)}")


# Function: Generate Report
def generate_report():
    with signal_lock:
        report_lines = ["📊 Hourly Market Report"]
        for symbol, vals in signals_data.items():
            report_lines.append(
                f"{symbol}: Price=${vals['price']}, "
                f"Change={vals['price_change_percent']}%, "
                f"Volume={vals['volume']}"
            )
    return "\n".join(report_lines)


# Function: Check if Report Should be Sent
last_report_time = None
def should_send_report():
    global last_report_time
    now = datetime.utcnow()
    if last_report_time is None:
        last_report_time = now
        return True
    elapsed_minutes = (now - last_report_time).total_seconds() / 60
    if elapsed_minutes >= REPORT_INTERVAL_MINUTES:
        last_report_time = now
        return True
    return False


# Background Task: Fetch and Send Signals
def signal_worker():
    while True:
        try:
            data = fetch_binance_data()
            with signal_lock:
                signals_data.update(data)
            for symbol, vals in data.items():
                if should_send_signal(symbol, vals):
                    msg = (f"🚨 {symbol} Alert!\n"
                           f"Price: ${vals['price']}\n"
                           f"Change: {vals['price_change_percent']}%\n"
                           f"Volume: {vals['volume']}")
                    send_telegram_message(msg)
            if should_send_report():
                report = generate_report()
                send_telegram_message(report)
        except Exception as e:
            logging.error(f"Signal worker error: {str(e)}")
        time.sleep(60)


# Flask Route: Status
@app.route("/status", methods=["GET"])
def get_status():
    return jsonify(app_status)


# Flask Route: Data
@app.route("/data", methods=["GET"])
def get_data():
    with signal_lock:
        return jsonify(signals_data)


# Start Background Thread
def start_background_worker():
    thread = Thread(target=signal_worker, daemon=True)
    thread.start()


if __name__ == "__main__":
    try:
        start_background_worker()
        app.run(host="0.0.0.0", port=5000)
    except Exception as e:
        logging.error(f"Flask app error: {str(e)}")
        app_status['status'] = 'flask_error'
