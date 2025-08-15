import asyncio
import json
import logging
import random
import time
import os
import csv
from datetime import datetime, timedelta
import requests
import tenacity
from flask import Flask, jsonify
from threading import Thread
import websockets

# ======== تنظیمات ========
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT']

# توصیه امنیتی: این‌ها را به صورت متغیر محیطی ست کن؛ اما برای راحتی اجرا، مقادیر پیش‌فرض گذاشته شده
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8136421090:AAFrb8RI6BQ2tH49YXX_5S32_W0yWfT04Cg')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '570096331')

PORT = int(os.getenv('PORT', 8080))
BINANCE_WS_BASE = 'wss://stream.binance.com:443/stream?streams='  # Binance Global'  # Binance Global

LOG_FILE = 'whalepulse_pro.log'

# بازه گزارش‌های دوره‌ای
REPORT_INTERVAL = 15 * 60         # 15 دقیقه
HOURLY_REPORT_INTERVAL = 60 * 60  # 1 ساعت

# آستانه تغییر برای تشخیص «گزارش 15 دقیقه‌ای لازم است یا نه»
MIN_CHANGE_PERCENT = 0.1          # 0.1%
MIN_CHANGE_VOLUME = 0.01          # 1%

# تنظیمات تلاش مجدد
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5

# هشداری‌ها
ALERT_THRESHOLD = float(os.getenv('ALERT_THRESHOLD', '5'))  # درصد تغییر برای هشدار فوری
ALERT_COOLDOWN = int(os.getenv('ALERT_COOLDOWN', '900'))    # ثانیه (پیش‌فرض 15 دقیقه)

# ذخیره CSV
CSV_FILE = os.getenv('CSV_FILE', 'market_data.csv')
CSV_SAVE_INTERVAL = int(os.getenv('CSV_SAVE_INTERVAL', '30'))  # هر چند ثانیه یکبار برای هر نماد ثبت شود

# متغیرهای گلوبال
last_report_time = 0
last_hourly_report_time = 0
last_report_data = {}

# وضعیت اپ
app_status = {
    'status': 'starting',
    'websocket_connected': False,
    'last_message_time': None,
    'messages_processed': 0,
    'last_telegram_send': None,
    'uptime_start': datetime.now()
}

# وضعیت بازار برای داشبورد و API
market_state = {}                 # {'BTCUSDT': {'price':..., 'volume':..., 'price_change_percent':..., 'updated_at':...}}
last_alert_time = {}              # زمان آخرین هشدار برای هر نماد
last_csv_write = {}               # زمان آخرین ثبت CSV برای هر نماد

# ======== لاگ ========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('whale_ws')

# ======== Flask App ========
app = Flask(__name__)

@app.route('/')
def home():
    uptime = datetime.now() - app_status['uptime_start']
    return f"""
    <h1>🐋 WhalePulse-Pro</h1>
    <p><strong>Status:</strong> {app_status['status']}</p>
    <p><strong>WebSocket:</strong> {'🟢 Connected' if app_status['websocket_connected'] else '🔴 Disconnected'}</p>
    <p><strong>Uptime:</strong> {uptime}</p>
    <p><strong>Messages Processed:</strong> {app_status['messages_processed']}</p>
    <p><strong>Symbols:</strong> {', '.join(SYMBOLS)}</p>
    <p><strong>Last Activity:</strong> {app_status['last_message_time']}</p>
    <hr>
    <a href="/status">📊 JSON Status</a> | 
    <a href="/health">🏥 Health Check</a> | 
    <a href="/test">🧪 Test Telegram</a> |
    <a href="/dashboard">📈 Live Dashboard</a>
    """

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy' if app_status['websocket_connected'] else 'unhealthy',
        'timestamp': datetime.now().isoformat(),
        'uptime_seconds': (datetime.now() - app_status['uptime_start']).total_seconds()
    })

@app.route('/status')
def status():
    return jsonify({
        **app_status,
        'uptime_start': app_status['uptime_start'].isoformat(),
        'symbols': SYMBOLS,
        'telegram_configured': bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        'alert_threshold': ALERT_THRESHOLD,
        'alert_cooldown_sec': ALERT_COOLDOWN
    })

@app.route('/test')
def test_telegram():
    try:
        result = test_telegram_bot()
        if result:
            send_to_telegram("🧪 Test message from WhalePulse-Pro!")
            return jsonify({'success': True, 'message': 'Telegram test successful!'})
        else:
            return jsonify({'success': False, 'message': 'Telegram bot test failed'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/ping')
def ping():
    app_status['last_ping'] = datetime.now().isoformat()
    return jsonify({
        'pong': True,
        'timestamp': app_status['last_ping'],
        'status': app_status['status']
    })

@app.route('/api/market')
def api_market():
    """وضعیت زنده بازار برای داشبورد"""
    return jsonify(market_state)

@app.route('/dashboard')
def dashboard():
    """داشبورد وب ساده (بدون نیاز به فایل template)"""
    return """
<!DOCTYPE html>
<html lang="fa">
<head>
<meta charset="utf-8">
<title>WhalePulse-Pro Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{font-family:Arial,Segoe UI,Tahoma,sans-serif;background:#0e0f12;color:#e5e7eb;margin:0;padding:24px}
  h1{margin:0 0 12px 0}
  .sub{color:#9ca3af;margin-bottom:20px}
  table{width:100%;border-collapse:collapse;background:#111318;border-radius:12px;overflow:hidden}
  th,td{padding:12px 10px;border-bottom:1px solid #1f2430;text-align:center}
  th{background:#151923;color:#cbd5e1;font-weight:600}
  tr:hover{background:#151823}
  .up{color:#16a34a;font-weight:700}
  .down{color:#ef4444;font-weight:700}
  .muted{color:#9ca3af}
  .pill{display:inline-block;padding:4px 10px;border-radius:999px;background:#1f2430;color:#cbd5e1;font-size:12px}
  .rowhead{display:flex;gap:8px;align-items:center;justify-content:center}
</style>
</head>
<body>
  <h1>🐋 WhalePulse-Pro Dashboard</h1>
  <div class="sub">Live prices &amp; 24h change • auto-refresh</div>
  <div id="meta" class="sub"></div>
  <table>
    <thead>
      <tr><th>Symbol</th><th>Price</th><th>Volume</th><th>Change 24h</th><th>Updated</th></tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
<script>
async function load(){
  const res = await fetch('/api/market');
  const data = await res.json();
  const tbody = document.getElementById('tbody');
  let html = '';
  const symbols = Object.keys(data).sort();
  for(const sym of symbols){
    const d = data[sym];
    const cls = (d.price_change_percent || 0) >= 0 ? 'up':'down';
    const price = Number(d.price||0);
    let pstr = '$' + (['BTCUSDT','ETHUSDT'].includes(sym) ? price.toFixed(0) : price.toFixed(2));
    html += `<tr>
      <td class="rowhead"><span class="pill">${sym}</span></td>
      <td>${pstr}</td>
      <td>${Number(d.volume||0).toLocaleString('en-US')}</td>
      <td class="${cls}">${Number(d.price_change_percent||0).toFixed(2)}%</td>
      <td class="muted">${(d.updated_at||'').replace('T',' ').split('.')[0]}</td>
    </tr>`;
  }
  tbody.innerHTML = html || '<tr><td colspan="5" class="muted">Waiting for data...</td></tr>';
  document.getElementById('meta').textContent = `Symbols: ${symbols.join(', ')}`;
}
load();
setInterval(load, 3000);
</script>
</body>
</html>
    """

# ======== Telegram Functions ========
def test_telegram_bot():
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe'
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            bot_info = response.json()
            logger.info(f"✅ Bot info: {bot_info.get('result', {}).get('username', 'Unknown')}")
            return True
        else:
            logger.error(f"❌ Bot test failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Bot test exception: {e}")
        return False

@tenacity.retry(
    stop=tenacity.stop_after_attempt(RETRY_ATTEMPTS),
    wait=tenacity.wait_fixed(RETRY_DELAY),
    retry=tenacity.retry_if_exception_type(Exception),
    before_sleep=lambda r: logger.warning(f"Retrying Telegram (attempt {r.attempt_number})...")
)
def send_to_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning('⚠️ Telegram token or chat id not configured.')
        return False
    
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            app_status['last_telegram_send'] = datetime.now().isoformat()
            logger.info('✅ پیام تلگرام ارسال شد.')
            return True
        else:
            error_msg = f'❌ Telegram failed {response.status_code}: {response.text}'
            logger.error(error_msg)
            raise Exception(error_msg)
    except Exception as e:
        logger.error(f'❌ Telegram exception: {e}')
        raise

# ======== ابزارها ========
def get_symbol_info(symbol):
    mapping = {
        'BTCUSDT': {'name': '₿ Bitcoin', 'emoji': '₿'},
        'ETHUSDT': {'name': '⟠ Ethereum', 'emoji': '⟠'},
        'SOLUSDT': {'name': '◎ Solana', 'emoji': '◎'},
        'XRPUSDT': {'name': '⨯ Ripple', 'emoji': '⨯'},
        'ADAUSDT': {'name': '₳ Cardano', 'emoji': '₳'}
    }
    return mapping.get(symbol, {'name': symbol, 'emoji': '💰'})

def format_price(symbol, price):
    if symbol in ['BTCUSDT', 'ETHUSDT']:
        return f"${price:,.0f}"
    elif symbol in ['SOLUSDT', 'ADAUSDT', 'XRPUSDT']:
        return f"${price:.2f}"
    return f"${price:.4f}"

def build_report_message(data):
    now_str = (datetime.now() + timedelta(hours=3.5)).strftime('%Y-%m-%d %H:%M:%S')
    header = f"🐋 <b>WhalePulse-Pro Market Report</b>\n⏰ {now_str} (+03:30)\n\n"
    sections = []
    for sym, vals in data.items():
        info = get_symbol_info(sym)
        arrow = "📈" if vals['price_change_percent'] >= 0 else "📉"
        percent_str = f"{vals['price_change_percent']:+.2f}%"
        sections.append(
            f"{info['emoji']} <b>{info['name']}</b>\n"
            f"💵 {format_price(sym, vals['price'])}\n"
            f"📊 Vol: {vals['volume']:,.0f}\n"
            f"{arrow} {percent_str}\n"
        )
    footer = "\n🤖 <i>WhalePulse-Pro | Market Intelligence</i>"
    message = header + "\n".join(sections) + footer
    if len(message) > 4000:
        message = message[:3900] + "...\n\n📝 <i>Message truncated</i>"
    return message

def should_send_report(new_data):
    global last_report_data
    if not last_report_data:
        return True
    for sym, vals in new_data.items():
        last_vals = last_report_data.get(sym)
        if not last_vals:
            return True
        price_diff = abs(vals['price_change_percent'] - last_vals['price_change_percent'])
        if price_diff >= MIN_CHANGE_PERCENT:
            logger.info(f"Price change detected for {sym}: {price_diff:.2f}%")
            return True
        if last_vals['volume'] > 0:
            volume_diff = abs(vals['volume'] - last_vals['volume']) / last_vals['volume']
            if volume_diff >= MIN_CHANGE_VOLUME:
                logger.info(f"Volume change detected for {sym}: {volume_diff:.2%}")
                return True
    return False

def ensure_csv_header():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['timestamp','symbol','price','volume','price_change_percent'])

def append_csv_row(symbol, price, volume, change_percent):
    ensure_csv_header()
    with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([datetime.now().isoformat(), symbol, price, volume, change_percent])

def maybe_save_csv(symbol, price, volume, change_percent, now_ts):
    last = last_csv_write.get(symbol, 0)
    if now_ts - last >= CSV_SAVE_INTERVAL:
        append_csv_row(symbol, price, volume, change_percent)
        last_csv_write[symbol] = now_ts

def maybe_alert(symbol, price, change_percent, now_ts):
    if abs(change_percent) >= ALERT_THRESHOLD:
        last = last_alert_time.get(symbol, 0)
        if now_ts - last >= ALERT_COOLDOWN:
            msg = (f"🚨 <b>ALERT</b>: {symbol} {change_percent:+.2f}%\n"
                   f"💵 Price: {format_price(symbol, price)}")
            send_to_telegram(msg)
            last_alert_time[symbol] = now_ts

# ======== WebSocket Handler ========
@tenacity.retry(
    stop=tenacity.stop_after_attempt(RETRY_ATTEMPTS),
    wait=tenacity.wait_fixed(RETRY_DELAY),
    retry=tenacity.retry_if_exception_type(Exception),
    before_sleep=lambda r: logger.warning(f"Retrying WebSocket (attempt {r.attempt_number})...")
)
async def connect_and_run(uri):
    global last_report_time, last_hourly_report_time, last_report_data
    logger.info(f'🔌 Connecting to {uri}')
    async with websockets.connect(
        uri,
        ping_interval=30,
        ping_timeout=10,
        max_size=None,
        close_timeout=5
    ) as ws:
        app_status['websocket_connected'] = True
        app_status['status'] = 'running'
        logger.info('✅ WebSocket connected successfully')
        current_data = {}
        message_count = 0

        async for message in ws:
            try:
                message_count += 1
                app_status['messages_processed'] += 1
                app_status['last_message_time'] = datetime.now().isoformat()

                msg = json.loads(message)
                data = msg.get('data') or msg  # multi-stream: {'stream':..., 'data': {...}}
                if not isinstance(data, dict) or data.get('e') != '24hrTicker':
                    continue

                symbol = data.get('s')
                if symbol not in SYMBOLS:
                    continue

                volume = float(data.get('v', 0))
                price = float(data.get('c', 0))
                price_change_percent = float(data.get('P', 0))
                now_ts = time.time()

                # بروزرسانی وضعیت سراسری بازار برای داشبورد
                market_state[symbol] = {
                    'price': price,
                    'volume': volume,
                    'price_change_percent': price_change_percent,
                    'updated_at': datetime.now().isoformat()
                }

                # داده برای گزارش‌های دوره‌ای
                current_data[symbol] = {
                    'volume': volume,
                    'price': price,
                    'price_change_percent': price_change_percent
                }

                # CSV (نمونه‌برداری دوره‌ای برای هر نماد)
                maybe_save_csv(symbol, price, volume, price_change_percent, now_ts)

                # هشدار درصدی با کول‌داون
                maybe_alert(symbol, price, price_change_percent, now_ts)

                # گزارش‌های دوره‌ای
                if len(current_data) >= len(SYMBOLS):
                    if now_ts - last_report_time >= REPORT_INTERVAL and should_send_report(current_data):
                        try:
                            message_text = build_report_message(current_data)
                            if send_to_telegram(message_text):
                                last_report_data = current_data.copy()
                                last_report_time = now_ts
                                logger.info("📊 گزارش 15 دقیقه ارسال شد")
                        except Exception as e:
                            logger.error(f"❌ Error sending 15min report: {e}")

                    if now_ts - last_hourly_report_time >= HOURLY_REPORT_INTERVAL:
                        try:
                            logger.info("📊 ارسال گزارش ساعتی...")
                            message_text = build_report_message(current_data)
                            send_to_telegram(message_text)
                            last_hourly_report_time = now_ts
                            logger.info("✅ گزارش ساعتی ارسال شد")
                        except Exception as e:
                            logger.error(f"❌ Error sending hourly report: {e}")

                if message_count % 200 == 0:
                    logger.info(f"Processed {message_count} WS messages. Symbols tracked: {len(current_data)}")

            except json.JSONDecodeError as e:
                logger.warning(f'JSON decode error: {e}')
            except Exception as e:
                logger.error(f'❌ Error processing WS message: {e}')

# ======== WebSocket Loop ========
def build_stream_path(symbols):
    parts = [s.lower() + '@ticker' for s in symbols]
    return f'{BINANCE_WS_BASE}{"/".join(parts)}'

async def watcher_loop():
    uri = build_stream_path(SYMBOLS)

    # تست تلگرام در شروع (اختیاری)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        if test_telegram_bot():
            logger.info("✅ Telegram bot verified")
        else:
            logger.warning("⚠️ Telegram bot verification failed")

    attempt = 0
    max_attempts = 50
    while attempt < max_attempts:
        try:
            attempt += 1
            backoff = min(300, (2 ** min(attempt, 8))) + random.uniform(0, 5)
            logger.info(f'🔄 Connection attempt {attempt}/{max_attempts}, backoff {backoff:.1f}s')
            await connect_and_run(uri)
            attempt = 0  # اگر ارتباط پایدار بود، شمارنده را ریست کن
        except KeyboardInterrupt:
            logger.info("👋 Interrupted by user")
            break
        except Exception as e:
            app_status['websocket_connected'] = False
            app_status['status'] = f'reconnecting_attempt_{attempt}'
            logger.error(f'💥 WebSocket error: {e}')
            if attempt >= max_attempts:
                app_status['status'] = 'failed_max_attempts'
                logger.error("❌ Max reconnection attempts reached!")
                break
            logger.info(f'⏳ Waiting {backoff:.1f}s before reconnect...')
            await asyncio.sleep(backoff)

# ======== Background Thread ========
def run_websocket_loop():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger.info("🚀 Starting WebSocket watcher...")
        loop.run_until_complete(watcher_loop())
    except Exception as e:
        logger.error(f"💥 WebSocket loop error: {e}")
        app_status['status'] = 'websocket_thread_error'

# Start WebSocket thread
websocket_thread = Thread(target=run_websocket_loop, daemon=True)
websocket_thread.start()

# ======== Main ========
if __name__ == "__main__":
    logger.info("🌟 Starting WhalePulse-Pro...")
    logger.info(f"📊 Monitoring: {SYMBOLS}")
    logger.info(f"📱 Telegram configured: {bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)}")
    logger.info(f"🚀 Port: {PORT}")
    app_status['status'] = 'flask_starting'
    try:
        # ایجاد هدر CSV در صورت نیاز
        ensure_csv_header()

        app.run(
            host='0.0.0.0',
            port=PORT,
            debug=False,
            threaded=True,
            use_reloader=False
        )
    except Exception as e:
        logger.error(f"💥 Flask startup error: {e}")
        app_status['status'] = 'flask_error'
