import asyncio
import json
import logging
import random
import time
import os
from datetime import datetime, timedelta
import requests
import tenacity
from flask import Flask, jsonify
from threading import Thread
import websockets

# ======== تنظیمات ========
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'USDTDUSDT', 'BTBUSDT', 'TOTALUSDT', 'TOTAL2USDT', 'TOTAL3USDT']
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '8136421090:AAFrb8RI6BQ2tH49YXX_5S32_W0yWfT04Cg')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '570096331')
PORT = int(os.getenv('PORT', 10000))
BINANCE_WS_BASE = 'wss://testnet.binance.vision/ws/'  # تست‌نت برای جلوگیری از خطای نرخ
LOG_FILE = 'whalepulse_pro.log'
REPORT_INTERVAL = 15 * 60       # 15 دقیقه
HOURLY_REPORT_INTERVAL = 60 * 60  # 1 ساعت
MIN_CHANGE_PERCENT = 0.1        # 0.1% تغییر قیمت
MIN_CHANGE_VOLUME = 0.01        # 1% تغییر حجم
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5

# متغیرهای گلوبال
last_report_time = 0
last_hourly_report_time = 0
last_report_data = {}
app_status = {
    'status': 'starting',
    'websocket_connected': False,
    'last_message_time': None,
    'messages_processed': 0,
    'last_telegram_send': None,
    'uptime_start': datetime.now()
}

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
    <a href="/healthz">🏥 Healthz Check</a> | 
    <a href="/test">🧪 Test Telegram</a>
    """

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy' if app_status['websocket_connected'] else 'unhealthy',
        'timestamp': datetime.now().isoformat(),
        'uptime_seconds': (datetime.now() - app_status['uptime_start']).total_seconds()
    })

@app.route('/healthz')
def healthz():
    return "OK", 200

@app.route('/status')
def status():
    return jsonify({
        **app_status,
        'uptime_start': app_status['uptime_start'].isoformat(),
        'symbols': SYMBOLS,
        'telegram_configured': bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
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
    
    response = requests.post(url, json=payload, timeout=10)
    if response.status_code == 200:
        app_status['last_telegram_send'] = datetime.now().isoformat()
        logger.info('✅ پیام تلگرام ارسال شد.')
        return True
    else:
        error_msg = f'❌ Telegram failed {response.status_code}: {response.text}'
        logger.error(error_msg)
        raise Exception(error_msg)

# ======== WebSocket Functions ========
def build_stream_path(symbols):
    parts = [s.lower() + '@ticker' for s in symbols]
    return [f'{BINANCE_WS_BASE}{part}' for part in parts]  # جدا کردن به تک‌نمادها

def get_symbol_info(symbol):
    symbol_info = {
        'BTCUSDT': {'name': '₿ Bitcoin', 'emoji': '₿', 'category': 'Major'},
        'ETHUSDT': {'name': '⟠ Ethereum', 'emoji': '⟠', 'category': 'Major'},
        'SOLUSDT': {'name': '◎ Solana', 'emoji': '◎', 'category': 'Major'},
        'XRPUSDT': {'name': '⨯ Ripple', 'emoji': '⨯', 'category': 'Major'},
        'ADAUSDT': {'name': '₳ Cardano', 'emoji': '₳', 'category': 'Major'},
        'USDTDUSDT': {'name': '📊 USDT Dominance', 'emoji': '📊', 'category': 'Index'},
        'BTBUSDT': {'name': '🔥 BTB Token', 'emoji': '🔥', 'category': 'Index'},
        'TOTALUSDT': {'name': '📈 Total Market Cap', 'emoji': '📈', 'category': 'Index'},
        'TOTAL2USDT': {'name': '📊 Total2 (Altcoins)', 'emoji': '📊', 'category': 'Index'},
        'TOTAL3USDT': {'name': '📉 Total3 (Others)', 'emoji': '📉', 'category': 'Index'}
    }
    return symbol_info.get(symbol, {'name': symbol, 'emoji': '💰', 'category': 'Other'})

def format_symbol_link(symbol):
    base_symbol = symbol.replace('USDT', '')
    binance_link = f"https://www.binance.com/en/trade/{base_symbol}_USDT"
    tradingview_link = f"https://www.tradingview.com/symbols/{symbol}"
    return binance_link, tradingview_link

def format_price(symbol, price):
    if symbol in ['USDTDUSDT', 'TOTALUSDT', 'TOTAL2USDT', 'TOTAL3USDT']:
        return f"{price:.2f}%"
    elif symbol == 'ETHBTC':
        return f"{price:.6f}"
    elif symbol in ['BTCUSDT']:
        return f"${price:,.0f}"
    elif symbol in ['ETHUSDT']:
        return f"${price:,.0f}"
    elif symbol in ['SOLUSDT', 'XRPUSDT', 'ADAUSDT']:
        return f"${price:.2f}"
    else:
        return f"${price:.4f}"

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

def build_report_message(data):
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    header = f"🐋 <b>WhalePulse-Pro Market Report</b>\n⏰ {now_str}\n\n"
    
    major_coins = []
    indices = []
    
    for sym, vals in data.items():
        info = get_symbol_info(sym)
        category = info['category']
        
        if category == 'Major':
            major_coins.append((sym, vals, info))
        elif category == 'Index':
            indices.append((sym, vals, info))
    
    sections = []
    
    if major_coins:
        sections.append("💰 <b>Major Cryptocurrencies</b>")
        for sym, vals, info in major_coins:
            arrow = "📈" if vals['price_change_percent'] >= 0 else "📉"
            percent_str = f"+{vals['price_change_percent']:.2f}%" if vals['price_change_percent'] >= 0 else f"{vals['price_change_percent']:.2f}%"
            binance_link, tradingview_link = format_symbol_link(sym)
            formatted_price = format_price(sym, vals['price'])
            section = (
                f"{info['emoji']} <b>{info['name']}</b>\n"
                f"💵 {formatted_price}\n"
                f"📊 Vol: {vals['volume']:,.0f}\n"
                f"{arrow} {percent_str}\n"
                f"🔗 <a href='{binance_link}'>Trade</a> | <a href='{tradingview_link}'>Chart</a>\n"
            )
            sections.append(section)
    
    if indices:
        sections.append("\n📊 <b>Market Indices</b>")
        for sym, vals, info in indices:
            arrow = "📈" if vals['price_change_percent'] >= 0 else "📉"
            percent_str = f"+{vals['price_change_percent']:.2f}%" if vals['price_change_percent'] >= 0 else f"{vals['price_change_percent']:.2f}%"
            formatted_price = format_price(sym, vals['price'])
            section = (
                f"{info['emoji']} <b>{info['name']}</b>\n"
                f"📈 {formatted_price}\n"
                f"{arrow} {percent_str}\n"
            )
            sections.append(section)
    
    footer = "\n🤖 <i>WhalePulse-Pro | Market Intelligence</i>"
    message = header + "\n".join(sections) + footer
    
    if len(message) > 4000:
        message = message[:3900] + "...\n\n📝 <i>Message truncated</i>"
    
    return message

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
    
    try:
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
                    data = msg.get('data') or msg
                    
                    if data and data.get('e') == '24hrTicker':
                        symbol = data.get('s') or data.get('symbol')
                        if not symbol or symbol not in SYMBOLS:
                            continue
                            
                        volume = float(data.get('v') or data.get('volume') or 0)
                        price = float(data.get('c') or data.get('close') or 0)
                        price_change_percent = float(data.get('P') or data.get('priceChangePercent') or 0)
                        
                        current_data[symbol] = {
                            'volume': volume,
                            'price': price,
                            'price_change_percent': price_change_percent
                        }
                        
                        if message_count % 100 == 0:
                            logger.info(f"Processed {message_count} messages. Current symbols: {len(current_data)}")
                        
                        now = time.time()
                        if len(current_data) >= len(SYMBOLS) and now - last_report_time >= REPORT_INTERVAL:
                            if should_send_report(current_data):
                                try:
                                    message_text = build_report_message(current_data)
                                    if send_to_telegram(message_text):
                                        last_report_data = current_data.copy()
                                        last_report_time = now
                                        logger.info("📊 گزارش 15 دقیقه ارسال شد")
                                except Exception as e:
                                    logger.error(f"❌ Error sending 15min report: {e}")
                        
                        if len(current_data) >= len(SYMBOLS) and now - last_hourly_report_time >= HOURLY_REPORT_INTERVAL:
                            try:
                                logger.info("📊 ارسال گزارش ساعتی...")
                                message_text = build_report_message(current_data)
                                send_to_telegram(message_text)
                                last_hourly_report_time = now
                                logger.info("✅ گزارش ساعتی ارسال شد")
                            except Exception as e:
                                logger.error(f"❌ Error sending hourly report: {e}")
                
                except json.JSONDecodeError as e:
                    logger.warning(f'JSON decode error: {e}')
                except Exception as e:
                    logger.error(f'❌ Error processing WS message: {e}')
                    
    except Exception as e:
        app_status['websocket_connected'] = False
        app_status['status'] = 'error'
        logger.error(f'❌ WebSocket connection error: {e}')
        raise

# ======== WebSocket Loop ========
async def watcher_loop():
    uris = build_stream_path(SYMBOLS)  # لیست URLهای تک‌نمادی
    attempt = 0
    max_attempts = 50
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        if test_telegram_bot():
            logger.info("✅ Telegram bot verified")
        else:
            logger.warning("⚠️ Telegram bot verification failed")
    
    while attempt < max_attempts:
        try:
            attempt += 1
            backoff = min(300, (2 ** min(attempt, 8))) + random.uniform(0, 5)
            logger.info(f'🔄 Connection attempt {attempt}/{max_attempts}, backoff {backoff:.1f}s')
            
            tasks = [connect_and_run(uri) for uri in uris]  # اجرای موازی برای هر نماد
            await asyncio.gather(*tasks, return_exceptions=True)
            
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
        else:
            attempt = 0  # Reset on successful connection

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
    logger.info("🌟 Starting WhalePulse-Pro for Render deployment...")
    logger.info(f"📊 Monitoring: {SYMBOLS}")
    logger.info(f"📱 Telegram configured: {bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)}")
    logger.info(f"🚀 Port: {PORT}")
    
    app_status['status'] = 'flask_starting'
    
    try:
        app.run(
            host='0.0.0.0', 
            port=PORT, 
            debug=False, 
            threaded=True,
            use_reloader=False  # Important for Render
        )
    except Exception as e:
        logger.error(f"💥 Flask startup error: {e}")
        app_status['status'] = 'flask_error'
