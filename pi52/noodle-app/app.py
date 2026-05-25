"""
煮麵自動化系統 V4 - Pi52
========================
硬體配置：
  /dev/ttyUSB0, 9600 baud, RS485 Modbus RTU（CH340 USB 轉 RS485）

  站號 0x01  電磁爐
  站號 0x08  溫度感測器（CH1）
  站號 0x0B  繼電器模組一（推桿 1-3）
  站號 0x0C  繼電器模組二（推桿 4-6 + 風扇 1-3）

繼電器通道對應（05H 寫單線圈）：
  Module 0x0B：
    coil 0/1  → 推桿1 伸出/縮回
    coil 2/3  → 推桿2 伸出/縮回
    coil 4/5  → 推桿3 伸出/縮回

  Module 0x0C：
    coil 0/1  → 推桿5 伸出/縮回
    coil 2/3  → 推桿6 伸出/縮回
    coil 4    → 風扇3
    coil 6/7  → 推桿4 伸出/縮回
    coil 6    → 風扇1
    coil 7    → 風扇2
"""

import time
import struct
import threading

import datetime
import json
import os
import serial
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)
START_TIME = datetime.datetime.now()

# ── 硬體常數 ──────────────────────────────────────────────────────────────────
SERIAL_PORT    = '/dev/ttyRS485'
BAUD_RATE      = 9600
RELAY1_ADDR    = 0x0B   # 繼電器模組一（目前唯一一台）
INDUCTION_ADDR = 0x01   # 電磁爐
TEMP_ADDR      = 0x08   # 溫度感測器

# 推桿通道對應 {推桿號: (伸出線圈, 縮回線圈, 繼電器站號)} or None=待接
ACTUATOR_MAP = {
    1: (0, 1, 0x0B),
    2: (2, 3, 0x0B),
    3: (4, 5, 0x0B),
    4: (6, 7, 0x0B),
    5: (0, 1, 0x0C),
    6: (2, 3, 0x0C),
}

# 風扇對應 {風扇號: (線圈, 繼電器站號)}
FAN_MAP = {
    1: (6, 0x0C),
    2: (7, 0x0C),
    3: (4, 0x0C),
}

# ── 共用狀態 ──────────────────────────────────────────────────────────────────
serial_lock   = threading.Lock()
current_temp  = None        # 最新溫度（None = 感測器故障/未讀到）
induction_pwr       = 0     # 目前電磁爐功率（命令値）
induction_actual_pwr = None  # 電磁爐回讀功率
induction_igbt_temp  = None  # IGBT 溫度（°C）
induction_error      = None  # 錯誤代碼（0=無故障）
fan_states    = {1: False, 2: False, 3: False}
relay_ok      = {'0b': False, '0c': False}
auto_temp_on  = False       # 自動溫控開關

AUTO_MAX_PWR = 65   # 自動控溫最高功率上限

# ── Session 記錄 ──────────────────────────────────────────────────────────────
SESSIONS_DIR    = '/var/www/html/sessions'
current_session = None          # 當前加熱 session dict
session_lock    = threading.Lock()

# ── Alerts 佇列（前端輪詢） ───────────────────────────────────────────────────
alerts      = []                # [{ts, level, msg}]  level: warn | critical
alerts_lock = threading.Lock()

# ── 監控偵測狀態 ──────────────────────────────────────────────────────────────
_prev_error   = 0
_igbt_warned  = False           # 是否已發出 IGBT 過熱警報
_stop_count   = 0               # 連續偵測到「有命令但報錯」次數
_igbt_high_count = 0            # 連續偵測到 IGBT 過高的次數（防抖動）
_was_auto_on  = False           # 追蹤 auto_temp 狀態變化以自動管理 session


# ── 溫度曲線記錄 ──────────────────────────────────────────────────────────────
TEMP_LOG_MAX = 1800          # 最多保留 1800 筆（@2s = 1 小時）
temp_log     = []            # [{ts, temp, pwr}, ...]
temp_log_lock = threading.Lock()

auto_cfg = {
    'pwr_low':     45,   # T < temp_boost 時的加熱功率
    'pwr_high':    45,   # temp_boost <= T < temp_keep 時的衝刺功率
    'pwr_keep':    40,   # 保溫功率（T >= temp_keep）
    'temp_boost':  89,   # 切換到高功率的溫度閾值（℃）
    'temp_keep':   100,  # 進入保溫的溫度閾值（℃）
    'temp_reheat': 93,   # 保溫中低於此溫度重新加熱（℃）
}

_reached_boil = False  # 是否已達 temp_keep，用於保溫滯環控制

# ── 序列埠（延遲初始化）────────────────────────────────────────────────────────
_ser = None

def get_ser():
    global _ser
    if _ser and _ser.is_open:
        return _ser
    for _ in range(10):
        try:
            _ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            _ser.rts = False
            return _ser
        except Exception:
            time.sleep(1)
    raise IOError(f'無法開啟序列埠 {SERIAL_PORT}')


# ── Modbus 底層 ───────────────────────────────────────────────────────────────
def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _send(data: bytes) -> bytes:
    """附加 CRC 後送出，回傳回應。呼叫前必須持有 serial_lock。"""
    global _ser
    frame = data + struct.pack('<H', _crc16(data))
    try:
        ser = get_ser()
        ser.reset_input_buffer()
        ser.rts = True
        ser.write(frame)
        ser.flush()
        ser.rts = False
        time.sleep(0.05)
        return ser.read(ser.inWaiting())
    except (serial.SerialException, OSError):
        _ser = None   # USB 斷線後強制重連
        return b""


# ── 硬體操作 ──────────────────────────────────────────────────────────────────
def coil_write(relay_addr: int, coil: int, on: bool):
    """05H 寫單線圈（FF 00 = ON，00 00 = OFF）"""
    val  = b'\xFF\x00' if on else b'\x00\x00'
    data = bytes([relay_addr, 0x05]) + struct.pack('>H', coil) + val
    with serial_lock:
        return _send(data)


def induction_set(pwr: int):
    """設定電磁爐功率 0~100%"""
    global induction_pwr
    pwr  = max(0, min(70, int(pwr)))   # 最高 70%
    data = bytes([INDUCTION_ADDR, 0x06, 0x10, 0x00, 0x00, pwr])
    with serial_lock:
        _send(data)
    induction_pwr = pwr



def induction_read_status():
    """查詢電磁爐: 功率(0x0000) / IGBT溫度(0x0002) / 錯誤碼(0x0017)"""
    global induction_actual_pwr, induction_igbt_temp, induction_error
    with serial_lock:
        r1 = _send(bytes([INDUCTION_ADDR, 0x03, 0x00, 0x00, 0x00, 0x01]))
    if r1 and len(r1) >= 5:
        induction_actual_pwr = struct.unpack('>H', r1[3:5])[0]
    with serial_lock:
        r2 = _send(bytes([INDUCTION_ADDR, 0x03, 0x00, 0x02, 0x00, 0x01]))
    if r2 and len(r2) >= 5:
        induction_igbt_temp = struct.unpack('>H', r2[3:5])[0]
    with serial_lock:
        r3 = _send(bytes([INDUCTION_ADDR, 0x03, 0x00, 0x17, 0x00, 0x01]))
    if r3 and len(r3) >= 5:
        induction_error = struct.unpack('>H', r3[3:5])[0]



def temp_read():
    """讀取 CH1 溫度（°C），感測器故障回傳 None"""
    data = bytes([TEMP_ADDR, 0x04, 0x00, 0x00, 0x00, 0x01])
    with serial_lock:
        resp = _send(data)
    if resp and len(resp) >= 5:
        raw = struct.unpack('>H', resp[3:5])[0]
        if raw == 0xFFFF:
            return None
        neg = raw >> 15
        val = raw & 0x7FFF
        return -(val * 0.1) if neg else val * 0.1
    return None


# ── 推桿操作 ──────────────────────────────────────────────────────────────────
def actuator_extend(ch: int):
    """推桿伸出（籃子下降入水）"""
    mapping = ACTUATOR_MAP.get(ch)
    if not mapping:
        raise ValueError(f'推桿 {ch} 待接第二台繼電器模組')
    ext_coil, ret_coil, addr = mapping
    coil_write(addr, ret_coil, False)   # 先關縮回訊號（安全互鎖）
    time.sleep(0.1)
    coil_write(addr, ext_coil, True)    # 開伸出


def _retract_worker(ch: int, travel_time: int):
    """推桿縮回後自動全停（在背景執行緒執行）"""
    mapping = ACTUATOR_MAP.get(ch)
    if not mapping:
        return
    ext_coil, ret_coil, addr = mapping
    coil_write(addr, ext_coil, False)   # 關伸出
    time.sleep(0.1)
    coil_write(addr, ret_coil, True)    # 開縮回
    time.sleep(travel_time)             # 等推桿走完全程
    coil_write(addr, ret_coil, False)   # 全停


def actuator_retract(ch: int, travel_time: int):
    """推桿縮回（非阻塞，背景執行）"""
    t = threading.Thread(target=_retract_worker, args=(ch, travel_time), daemon=True)
    t.start()


# ── 自動溫控背景執行緒 ────────────────────────────────────────────────────────

def _relay_probe_loop():
    global relay_ok
    while True:
        for addr, key in [(0x0B, '0b'), (0x0C, '0c')]:
            try:
                frame = bytes([addr, 0x01, 0x00, 0x00, 0x00, 0x01])
                with serial_lock:
                    resp = _send(frame)
                relay_ok[key] = len(resp) >= 4 and resp[0] == addr
            except Exception:
                relay_ok[key] = False
        time.sleep(5)

threading.Thread(target=_relay_probe_loop, daemon=True).start()

def _induction_monitor_loop():
    global _prev_error, _igbt_warned, _stop_count, _igbt_high_count, induction_pwr
    while True:
        try:
            induction_read_status()
            ts = round(time.time())

            # ── 1. 錯誤碼變化偵測 ──────────────────────────────────────────
            if induction_error is not None:
                if induction_error != 0 and induction_error != _prev_error:
                    desc = f'E{induction_error:02X}'
                    _log_event('fault', f'電磁爐故障代碼 {desc}，已自動清除命令功率', level='critical')
                    _push_alert('critical', f'🚨 電磁爐故障: {desc}')
                    induction_pwr = 0
                elif induction_error == 0 and _prev_error != 0:
                    _log_event('recovered', f'故障解除（E{_prev_error:02X} → 0）', level='info')
                    _push_alert('info', f'✅ 故障解除')
                _prev_error = induction_error

            # ── 2. IGBT 溫度過高偵測（連續 2 次才觸發，防抖動） ──────────
            if induction_igbt_temp is not None:
                if induction_igbt_temp >= 70:
                    _igbt_high_count += 1
                else:
                    _igbt_high_count = 0
                if _igbt_high_count >= 2 and not _igbt_warned:
                    if induction_igbt_temp >= 80:
                        _log_event('igbt_critical', f'IGBT {induction_igbt_temp}C >= 80C, may auto-stop', level='critical')
                        _push_alert('critical', f'IGBT 過熱: {induction_igbt_temp}°C')
                    else:
                        _log_event('igbt_warn', f'IGBT {induction_igbt_temp}C >= 70C, check cooling', level='warn')
                        _push_alert('warn', f'IGBT 溫度偏高: {induction_igbt_temp}°C')
                    _igbt_warned = True
                elif induction_igbt_temp < 65:
                    _igbt_warned = False
                    _igbt_high_count = 0

            # ── 3. 意外停止偵測（有命令但電磁爐報錯） ──────────────────────
            if induction_pwr > 0 and induction_error is not None and induction_error != 0:
                _stop_count += 1
                if _stop_count == 2:   # 連續 2 次 (~10s) 確認
                    _log_event('auto_stop',
                               f'設定功率 {induction_pwr}% 但電磁爐報錯 E{induction_error:02X}，機器已自動保護停止',
                               level='critical')
                    _push_alert('critical', f'🛑 電磁爐自動停止（E{induction_error:02X}）')
            else:
                _stop_count = 0

            # ── 4. 將本次讀數追加到 session ───────────────────────────────
            _session_append_reading({
                'ts':       ts,
                'pt100':    round(current_temp, 1) if (current_temp is not None and -200 < current_temp < 400) else None,
                'igbt':     induction_igbt_temp,
                'cmd_pwr':  induction_pwr,
                'actual_pwr': induction_actual_pwr,
                'error':    induction_error,
            })

        except Exception as e:
            print(f'[induction_monitor] {e}')
        time.sleep(5)

threading.Thread(target=_induction_monitor_loop, daemon=True).start()


def _auto_temp_loop():
    global current_temp
    while True:
        try:
            t = temp_read()
            current_temp = t

            if auto_temp_on and t is not None:
                global _reached_boil
                cfg = auto_cfg
                if not _reached_boil:
                    if t >= cfg['temp_keep']:
                        _reached_boil = True
                        target = cfg['pwr_keep']
                    elif t >= cfg['temp_boost']:
                        target = cfg['pwr_high']
                    else:
                        target = cfg['pwr_low']
                else:
                    if t < cfg['temp_reheat']:
                        target = cfg['pwr_high']
                    else:
                        target = cfg['pwr_keep']

                if target != induction_pwr:
                    induction_set(target)

            # 記錄溫度曲線
            if t is not None:
                entry = {
                    'ts':   round(time.time()),
                    'temp': round(t, 1),
                    'pwr':  induction_pwr,
                    'igbt': induction_igbt_temp,
                    'apwr': induction_actual_pwr,
                }
                with temp_log_lock:
                    temp_log.append(entry)
                    if len(temp_log) > TEMP_LOG_MAX:
                        temp_log.pop(0)

            # ── 自動 session 管理（auto_temp 或手動功率 > 0 均觸發） ────
            global _was_auto_on
            is_heating_now = auto_temp_on or (induction_pwr > 0)
            if is_heating_now and not _was_auto_on:
                session_start_api()               # 加熱開始 → 自動建立 session
            elif not is_heating_now and _was_auto_on:
                session_stop_api()                # 加熱停止 → 自動結束 session
            _was_auto_on = is_heating_now

        except Exception as e:
            print(f'[auto_temp] {e}')

        time.sleep(2)


threading.Thread(target=_auto_temp_loop, daemon=True).start()


# ── Session 管理 ──────────────────────────────────────────────────────────────

def _save_session_locked(sess):
    """儲存 session（呼叫前必須持有 session_lock）"""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, sess['id'] + '.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(sess, f, ensure_ascii=False, indent=2)

def _log_event(etype, msg, level='info'):
    """記錄事件到當前 session，並自動儲存"""
    ts = round(time.time())
    event = {
        'ts':      ts,
        'dt':      datetime.datetime.now().strftime('%H:%M:%S'),
        'type':    etype,
        'level':   level,
        'msg':     msg,
        'pt100':   round(current_temp, 1) if current_temp is not None else None,
        'igbt':    induction_igbt_temp,
        'cmd_pwr': induction_pwr,
        'error':   induction_error,
    }
    with session_lock:
        if current_session and not current_session['ended']:
            current_session['events'].append(event)
            _save_session_locked(current_session)
    try:
        print(f'[event/{level}] {msg}')
    except Exception:
        pass

def _push_alert(level, msg):
    """推送即時警報供前端輪詢"""
    with alerts_lock:
        alerts.append({'ts': round(time.time()), 'level': level, 'msg': msg})
        if len(alerts) > 100:
            alerts.pop(0)

def _session_append_reading(reading):
    """每 5 秒追加一筆監測數據，每 60 筆自動存檔"""
    with session_lock:
        if not current_session or current_session['ended']:
            return
        current_session['readings'].append(reading)
        if len(current_session['readings']) % 60 == 0:
            _save_session_locked(current_session)

def session_start_api():
    global current_session
    ts  = int(time.time())
    sid = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    with session_lock:
        current_session = {
            'id':         sid,
            'start_ts':   ts,
            'start_dt':   datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'start_pt100': round(current_temp, 1) if current_temp is not None else None,
            'start_igbt':  induction_igbt_temp,
            'readings':   [],
            'events':     [],
            'ended':      False,
        }
        _save_session_locked(current_session)
    # 清除 alerts 舊資料
    with alerts_lock:
        alerts.clear()
    return sid

def session_stop_api():
    global current_session
    with session_lock:
        if current_session and not current_session['ended']:
            current_session['ended']  = True
            current_session['end_ts'] = int(time.time())
            current_session['end_dt'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save_session_locked(current_session)


# ── Flask 路由 ────────────────────────────────────────────────────────────────

# ── Settings 讀寫 ─────────────────────────────────────────────────────────────
SETTINGS_FILE = '/var/www/html/settings.json'

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_settings(data):
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route('/api/health')
def api_health():
    """各模組連線診斷"""
    elapsed = datetime.datetime.now() - START_TIME
    h, r = divmod(int(elapsed.total_seconds()), 3600)
    m, s = divmod(r, 60)
    uptime = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    result = {'uptime': uptime, 'serial_ok': False, 'devices': {}}

    def probe(addr, frame):
        try:
            with serial_lock:
                resp = _send(bytes(frame))
            return bool(resp and len(resp) >= 4)
        except Exception:
            return False

    try:
        get_ser()
        result['serial_ok'] = True

        # 溫度感測器 0x08
        t = temp_read()
        result['devices']['temp'] = {
            'name': '溫度感測器', 'addr': '0x08',
            'ok': t is not None,
            'value': f'{t:.1f}°C' if t is not None else None
        }

        # 繼電器 0x0B FC01H 讀線圈
        result['devices']['relay_0b'] = {
            'name': '繼電器一', 'addr': '0x0B',
            'ok': probe(0x0B, [0x0B, 0x01, 0x00, 0x00, 0x00, 0x01])
        }

        # 繼電器 0x0C FC01H 讀線圈
        result['devices']['relay_0c'] = {
            'name': '繼電器二', 'addr': '0x0C',
            'ok': probe(0x0C, [0x0C, 0x01, 0x00, 0x00, 0x00, 0x01])
        }

        # 電磁爐 0x01 FC03H 讀暫存器
        result['devices']['induction'] = {
            'name': '電磁爐', 'addr': '0x01',
            'ok': probe(0x01, [0x01, 0x03, 0x10, 0x00, 0x00, 0x01])
        }

    except Exception as e:
        result['error'] = str(e)

    return jsonify(result)


@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify(load_settings())


@app.route('/api/settings', methods=['POST'])
def api_settings_post():
    settings = load_settings()
    settings.update(request.json)
    save_settings(settings)
    return jsonify({'status': 'ok'})


@app.route('/')
def index():
    return render_template('index.html')








@app.route('/temp_curve')
def temp_curve():
    return render_template('temp_curve.html')


@app.route('/water_timer')
def water_timer():
    return render_template('water_timer.html')


@app.route('/api/status')
def api_status():
    """前端輪詢用：回傳目前所有狀態"""
    return jsonify({
        'temp':           current_temp,
        'induction_pwr':  induction_pwr,
        'auto_temp_on':   auto_temp_on,
        'auto_cfg':       auto_cfg,
        'fans':           fan_states,
        'relay_ok':            relay_ok,
        'induction_actual_pwr': induction_actual_pwr,
        'induction_igbt_temp':  induction_igbt_temp,
        'induction_error':      induction_error,
    })


@app.route('/api/induction', methods=['POST'])
def api_induction():
    global auto_temp_on
    pwr = int(request.json.get('power', 0))
    auto_temp_on = False    # 手動設定時自動關閉溫控
    induction_set(pwr)
    return jsonify({'status': 'ok', 'power': pwr})


@app.route('/api/up', methods=['POST'])
def api_up():
    ch = int(request.json.get('channel'))
    try:
        actuator_extend(ch)
        return jsonify({'status': 'ok'})
    except ValueError as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 400


@app.route('/api/down_and_off', methods=['POST'])
def api_down():
    ch          = int(request.json.get('channel'))
    travel_time = int(request.json.get('travel_time', 10))
    actuator_retract(ch, travel_time)
    return jsonify({'status': 'ok'})


@app.route('/api/fan', methods=['POST'])
def api_fan():
    fan = int(request.json.get('fan'))
    on  = bool(request.json.get('on'))
    mapping = FAN_MAP.get(fan)
    if not mapping:
        return jsonify({'status': 'error', 'msg': 'invalid fan'}), 400
    coil, addr = mapping
    coil_write(addr, coil, on)
    fan_states[fan] = on
    return jsonify({'status': 'ok', 'fan': fan, 'on': on})


@app.route('/api/auto_temp', methods=['POST'])
def api_auto_temp():
    global auto_temp_on, auto_cfg
    data = request.json
    if 'enabled' in data:
        global _reached_boil
        new_val = bool(data['enabled'])
        if new_val and not auto_temp_on:
            _reached_boil = False
        auto_temp_on = new_val
    for key in ['pwr_low', 'pwr_high', 'pwr_keep', 'temp_boost', 'temp_keep', 'temp_reheat']:
        if key in data:
            auto_cfg[key] = int(data[key])
    return jsonify({'status': 'ok', 'enabled': auto_temp_on, 'config': auto_cfg})


@app.route('/api/temp_log', methods=['GET'])
def api_temp_log():
    with temp_log_lock:
        data = list(temp_log)
    return jsonify(data)

@app.route('/api/temp_log/clear', methods=['POST'])
def api_temp_log_clear():
    with temp_log_lock:
        temp_log.clear()
    return jsonify({'status': 'ok'})


# ── 版本控制 ──────────────────────────────────────────────────────────────────
import shutil, hashlib, glob, re

VERSIONS_DIR = '/var/www/html/versions'
VERSIONED_FILES = [
    '/var/www/html/templates/index.html',
    '/var/www/html/app.py',
]

def _ensure_versions_dir():
    os.makedirs(VERSIONS_DIR, exist_ok=True)

def _version_manifest():
    mf = os.path.join(VERSIONS_DIR, 'manifest.json')
    if os.path.exists(mf):
        with open(mf, encoding='utf-8') as f:
            return json.load(f)
    return []

def _save_manifest(data):
    mf = os.path.join(VERSIONS_DIR, 'manifest.json')
    with open(mf, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route('/api/versions', methods=['GET'])
def api_versions_list():
    _ensure_versions_dir()
    return jsonify(_version_manifest())

@app.route('/api/versions/save', methods=['POST'])
def api_versions_save():
    _ensure_versions_dir()
    data = request.json or {}
    label = data.get('label', '未命名').strip() or '未命名'

    ts = int(time.time())
    vid = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    ver_dir = os.path.join(VERSIONS_DIR, vid)
    os.makedirs(ver_dir, exist_ok=True)

    saved = []
    for src in VERSIONED_FILES:
        if os.path.exists(src):
            fname = os.path.basename(src)
            dst = os.path.join(ver_dir, fname)
            shutil.copy2(src, dst)
            saved.append(fname)

    manifest = _version_manifest()
    manifest.insert(0, {
        'id': vid,
        'label': label,
        'timestamp': ts,
        'datetime': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'files': saved,
    })
    # keep max 20 versions
    for old in manifest[20:]:
        old_dir = os.path.join(VERSIONS_DIR, old['id'])
        if os.path.exists(old_dir):
            shutil.rmtree(old_dir)
    manifest = manifest[:20]
    _save_manifest(manifest)

    return jsonify({'status': 'ok', 'id': vid, 'label': label})

@app.route('/api/versions/restore', methods=['POST'])
def api_versions_restore():
    data = request.json or {}
    vid = data.get('id', '')
    if not vid or '/' in vid or '..' in vid:
        return jsonify({'status': 'error', 'msg': 'invalid id'}), 400

    ver_dir = os.path.join(VERSIONS_DIR, vid)
    if not os.path.exists(ver_dir):
        return jsonify({'status': 'error', 'msg': 'version not found'}), 404

    # backup current before restore
    backup_label = f'還原前備份（{datetime.datetime.now().strftime("%m/%d %H:%M")}）'

    restored = []
    for fname in os.listdir(ver_dir):
        src = os.path.join(ver_dir, fname)
        # find target path
        for orig in VERSIONED_FILES:
            if os.path.basename(orig) == fname:
                shutil.copy2(src, orig)
                restored.append(fname)
                break

    return jsonify({'status': 'ok', 'restored': restored})


@app.route('/api/session/start', methods=['POST'])
def api_session_start():
    sid = session_start_api()
    return jsonify({'status': 'ok', 'id': sid})

@app.route('/api/session/stop', methods=['POST'])
def api_session_stop():
    session_stop_api()
    return jsonify({'status': 'ok'})

@app.route('/api/session/current')
def api_session_current():
    with session_lock:
        if current_session and not current_session['ended']:
            return jsonify({
                'active':        True,
                'id':            current_session['id'],
                'start_ts':      current_session['start_ts'],
                'start_dt':      current_session['start_dt'],
                'reading_count': len(current_session['readings']),
                'events':        current_session['events'],
            })
    return jsonify({'active': False})

@app.route('/api/alerts')
def api_alerts_get():
    since = int(request.args.get('since', 0))
    with alerts_lock:
        result = [a for a in alerts if a['ts'] > since]
    return jsonify(result)

@app.route('/api/sessions')
def api_sessions_list():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(SESSIONS_DIR, '*.json')), reverse=True)[:30]
    result = []
    for fpath in files:
        try:
            with open(fpath, encoding='utf-8') as f:
                s = json.load(f)
            readings = s.get('readings', [])
            duration = 0
            if s.get('end_ts'):
                duration = s['end_ts'] - s['start_ts']
            elif readings:
                duration = readings[-1]['ts'] - s['start_ts']
            result.append({
                'id':            s['id'],
                'start_dt':      s.get('start_dt', ''),
                'reading_count': len(readings),
                'event_count':   len(s.get('events', [])),
                'events':        s.get('events', []),
                'ended':         s.get('ended', False),
                'duration':      duration,
                'has_fault':     any(e['level'] == 'critical' for e in s.get('events', [])),
                'start_pt100':   s.get('start_pt100'),
                'start_igbt':    s.get('start_igbt'),
            })
        except Exception:
            pass
    return jsonify(result)

@app.route('/api/sessions/<sid>')
def api_session_detail(sid):
    if not re.match(r'^\d{8}_\d{6}$', sid):
        return jsonify({'error': 'invalid id'}), 400
    path = os.path.join(SESSIONS_DIR, sid + '.json')
    if not os.path.exists(path):
        return jsonify({'error': 'not found'}), 404
    with open(path, encoding='utf-8') as f:
        return jsonify(json.load(f))



@app.route('/api/pi_info')
def api_pi_info():
    """標準化節點資訊端點，供 pi53 dashboard 一鍵查詢"""
    import subprocess as sp
    try:
        up = sp.check_output(['uptime', '-p'], timeout=3).decode().strip()
    except Exception:
        up = 'unknown'
    return jsonify({
        'name': 'pi52',
        'role': '煮麵自動化系統',
        'model': 'Raspberry Pi 5',
        'ip': {'local': '192.168.0.21', 'tailscale': '100.98.225.85'},
        'ui_url': 'http://100.98.225.85:5000',
        'services': [
            {'name': 'noodle-app', 'port': 5000, 'status': 'running'}
        ],
        'sensors': {
            'pt100': round(current_temp, 1) if current_temp is not None else None,
            'igbt': induction_igbt_temp,
        },
        'induction': {
            'cmd_pwr': induction_pwr,
            'actual_pwr': induction_actual_pwr,
            'error': induction_error,
            'auto': auto_temp_on,
        },
        'uptime': up,
        'updated': datetime.datetime.now().isoformat(),
    })

# ── UberEats 訂單紀錄 ─────────────────────────────────────────────────────────
import sqlite3 as _sq_orders

ORDERS_DB = '/var/www/html/orders.db'

def _orders_db():
    con = _sq_orders.connect(ORDERS_DB)
    con.row_factory = _sq_orders.Row
    return con

@app.route('/orders')
def page_orders():
    return render_template('orders.html')

@app.route('/api/orders')
def api_orders():
    try:
        page     = max(1, int(request.args.get('page', 1)))
        per_page = min(50, max(10, int(request.args.get('per_page', 20))))
        date_q   = request.args.get('date', '')
        tablet_q = request.args.get('tablet', '')
        con = _orders_db()
        where, params = [], []
        if date_q:
            where.append('received_at LIKE ?')
            params.append(date_q + '%')
        if tablet_q:
            where.append('tablet_ip = ?')
            params.append(tablet_q)
        w_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
        total = con.execute('SELECT COUNT(*) FROM orders ' + w_sql, params).fetchone()[0]
        offset = (page - 1) * per_page
        rows = con.execute(
            'SELECT id, received_at, tablet_ip, job_id, image_path, raw_size '
            'FROM orders ' + w_sql + ' ORDER BY id DESC LIMIT ? OFFSET ?',
            params + [per_page, offset]
        ).fetchall()
        tablets = [r[0] for r in con.execute(
            'SELECT DISTINCT tablet_ip FROM orders ORDER BY tablet_ip'
        ).fetchall()]
        con.close()
        return jsonify({'orders': [dict(r) for r in rows], 'total': total,
                        'page': page, 'per_page': per_page, 'tablets': tablets})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/orders/<int:oid>')
def api_order_detail(oid):
    try:
        con = _orders_db()
        row = con.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        con.close()
        if not row:
            return jsonify({'error': 'not found'}), 404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/orders/<int:oid>/reprint', methods=['POST'])
def api_order_reprint(oid):
    """把已存的 UberEats PNG 重新送到印表機"""
    import socket as _sock
    from PIL import Image
    import os, struct

    PRINT_HOST = '127.0.0.1'
    PRINT_PORT = 9200
    PAPER_W    = 576

    try:
        con = _orders_db()
        row = con.execute('SELECT image_path FROM orders WHERE id=?', (oid,)).fetchone()
        con.close()
        if not row or not row['image_path']:
            return jsonify({'ok': False, 'error': 'not found'}), 404

        img_file = os.path.join('/var/www/html/static', row['image_path'])
        if not os.path.exists(img_file):
            return jsonify({'ok': False, 'error': 'image file missing'}), 404

        img = Image.open(img_file).convert('L')
        # 縮放到紙張寬度
        if img.width != PAPER_W:
            h = int(img.height * PAPER_W / img.width)
            img = img.resize((PAPER_W, h), Image.LANCZOS)
        # 轉 1-bit
        bw = img.point(lambda p: 0 if p < 128 else 255, '1')
        w, h = bw.size
        width_bytes = (w + 7) // 8

        buf = bytearray()
        buf += b'@'           # ESC @ init
        buf += b'a'      # center
        # GS v 0 raster image
        buf += bytes([0x1D, 0x76, 0x30, 0x00,
                      width_bytes & 0xFF, (width_bytes >> 8) & 0xFF,
                      h & 0xFF,          (h >> 8) & 0xFF])
        pixels = list(bw.getdata())
        for y in range(h):
            byte_val = 0
            for x in range(w):
                if pixels[y * w + x] == 0:
                    byte_val |= (0x80 >> (x % 8))
                if x % 8 == 7:
                    buf.append(byte_val); byte_val = 0
            if w % 8:
                buf.append(byte_val)
        buf += bytes([0x1B, 0x64, 0x05])     # feed
        buf += bytes([0x1D, 0x56, 0x41, 0x00])  # cut

        s = _sock.create_connection((PRINT_HOST, PRINT_PORT), timeout=5)
        s.sendall(bytes(buf))
        s.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/orders/<int:oid>/meta')
def api_order_meta(oid):
    """OCR 讀取訂單圖片，提取單號/總金額，結果快取在 DB"""
    import re
    import os

    try:
        con = _orders_db()

        # 先查 cache（orders 表如果有 order_code 欄）
        row = con.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        if not row:
            con.close()
            return jsonify({'ok': False, 'error': 'not found'}), 404

        d = dict(row)

        # 若已快取（NULL = 未跑，'' 或有值 = 已跑，即使空字串也不重跑）
        if d.get('paid_amount') is not None:
            con.close()
            return jsonify({'ok': True, 'order_code': d.get('order_code') or '', 'paid_amount': d.get('paid_amount') or ''})

        # 沒快取 → OCR
        img_file = os.path.join('/var/www/html/static', d.get('image_path', ''))
        if not os.path.exists(img_file):
            con.close()
            return jsonify({'ok': False, 'error': 'image missing'}), 404

        try:
            import pytesseract
            from PIL import Image as _Image
            img = _Image.open(img_file)
            text = pytesseract.image_to_string(img, lang='chi_tra+eng', config='--psm 6 --oem 3', timeout=20)
        except Exception as e:
            con.close()
            return jsonify({'ok': False, 'error': f'ocr error: {e}'}), 500

        # 提取訂單代碼：只在首個含$的行之前搜尋，避免被品項英文干擾
        _hlines = []
        for _l in text.split('\n'):
            if re.search(r'\$[0-9]', _l):
                break
            _hlines.append(_l)
        _hdr = ' '.join(_hlines)
        code_m = re.search(r'[.。\s]+([A-Z0-9]{4,6})\b', _hdr) or re.search(r'\b([A-Z0-9]{4,6})\b', _hdr)
        order_code = code_m.group(1) if code_m else ''

        # 提取已付金額
        lines = text.split('\n')
        paid_amount = ''
        subtotal = ''
        for line in lines:
            m = re.search(r'([0-9,]+\.[0-9]+)', line)
            if m:
                if any(c in line for c in ['付', 'paid', 'Paid']):
                    paid_amount = m.group(1)
                elif any(c in line for c in ['小計', '小计', 'sub', 'Sub']):
                    subtotal = m.group(1)
        if not paid_amount:
            paid_amount = subtotal

        # 嘗試存回 DB（若欄位存在）
        try:
            con.execute(
                'UPDATE orders SET order_code=?, paid_amount=? WHERE id=?',
                (order_code, paid_amount, oid)
            )
            con.commit()
        except Exception:
            pass  # 欄位不存在時忽略
        con.close()

        return jsonify({'ok': True, 'order_code': order_code, 'paid_amount': paid_amount})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/print_receipt', methods=['POST'])
def print_receipt():
    import socket as _sock
    from PIL import Image, ImageDraw, ImageFont
    import textwrap

    # 透過 proxy.py 的現有 printer 連線送資料，避免多重 TCP 連線到印表機
    PRINT_HOST   = '127.0.0.1'
    PRINT_PORT   = 9200
    FONT_BOLD    = '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
    FONT_REGULAR = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
    PAPER_W      = 576

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'invalid json'}), 400

    order_number  = data.get('order_number', '')
    created_at    = data.get('created_at', '')
    payment       = data.get('payment_method', '')
    total         = float(data.get('total_amount') or 0)
    original_total = float(data.get('original_total') or 0)
    redeem_amount  = round(original_total - total) if original_total > total else 0
    cash_received = float(data.get('cash_received') or 0)
    change        = float(data.get('change_amount') or 0)
    queue_number    = data.get('queue_number')
    customer_phone  = (data.get('customer_phone') or '').strip()
    order_status    = data.get('status', 'completed')
    is_reservation  = bool(data.get('is_reservation'))
    pickup_time     = (data.get('pickup_time') or '').strip()
    items           = data.get('items', [])
    # 結構化欄位（pi53 新版送來），舊版回退到 note 解析
    spec_text     = (data.get('spec_text') or '').strip()
    user_note     = (data.get('user_note') or '').strip()
    if not spec_text and not user_note:
        legacy_note = (data.get('note') or '').strip()
        if '｜' in legacy_note:
            head, _, tail = legacy_note.partition('｜')
            spec_text, user_note = head.strip(), tail.strip()
        else:
            user_note = legacy_note
    PAY_LABEL     = {'cash': '現金', 'linepay': 'LINE Pay', 'jkopay': '街口支付'}
    pay_str       = PAY_LABEL.get(payment, payment)

    try:
        f_title  = ImageFont.truetype(FONT_BOLD, 96, index=0)   # 滷味
        f_sub    = ImageFont.truetype(FONT_BOLD, 48, index=0)   # 收銀收據
        f_queue  = ImageFont.truetype(FONT_BOLD, 76, index=0)   # 號碼牌（最醒目）
        f_spec   = ImageFont.truetype(FONT_BOLD, 62, index=0)   # 口味（廚房要看得清楚）
        f_large  = ImageFont.truetype(FONT_BOLD, 54, index=0)   # 合計
        f_normal = ImageFont.truetype(FONT_BOLD, 44, index=0)   # 品項、訂單、付款、找零
        f_small  = ImageFont.truetype(FONT_BOLD, 38, index=0)   # 時間、備註、分隔線
        f_disc   = ImageFont.truetype(FONT_BOLD, 36, index=0)   # 折扣標籤
        f_item   = ImageFont.truetype(FONT_BOLD, 35, index=0)   # 品項名稱與金額
    except Exception as e:
        return jsonify({'ok': False, 'error': f'font: {e}'}), 500

    rows = []
    def add(text, font, align='left', mt=0):
        rows.append((text, font, align, mt))
    def sep():
        add('- ' * 22, f_small, 'left', 6)

    def _w(s, font):
        return font.getbbox(s)[2] - font.getbbox(s)[0]

    def wrap_to_fit(text, font, max_w):
        """把長字串依字寬換行；若有空格，優先在空格處斷行（保留詞組完整）。"""
        if not text:
            return [text]
        out, line = [], ''
        # 先按空格切詞組；單一詞組超寬再退回逐字斷
        tokens = text.split(' ')
        for i, tok in enumerate(tokens):
            sep = '' if not line else ' '
            cand = line + sep + tok
            if _w(cand, font) <= max_w:
                line = cand
                continue
            # 加入後超寬：先把現有 line 收掉
            if line:
                out.append(line)
                line = ''
            # 詞組本身超寬 → 逐字斷
            if _w(tok, font) > max_w:
                cur = ''
                for ch in tok:
                    if _w(cur + ch, font) > max_w and cur:
                        out.append(cur)
                        cur = ch
                    else:
                        cur += ch
                line = cur
            else:
                line = tok
        if line:
            out.append(line)
        return out

    add('滷味', f_title, 'center', 16)
    add('收銀收據', f_sub, 'center', 8)
    sep()
    add(f'訂單  {order_number}', f_normal, 'left', 8)
    if queue_number:
        add(f'號碼牌  #{queue_number}', f_queue, 'left', 8)
    add(f'時間  {created_at}', f_small, 'left', 6)
    _src = '手機點餐' if str(order_number).startswith('C') else 'POS 收銀'
    add(f'來源  {_src}', f_small, 'left', 4)
    if customer_phone:
        add(f'電話  {customer_phone}', f_small, 'left', 4)
    if is_reservation and pickup_time:
        add(f'預約  {pickup_time}', f_small, 'left', 4)
    if spec_text:
        sep()
        add('【口味】', f_spec, 'center', 4)
        for raw_line in spec_text.split('\n'):
            for part in wrap_to_fit(raw_line.strip(), f_spec, PAPER_W - 40):
                if part:
                    add(part, f_spec, 'left', 4)
    sep()
    for it in items:
        name = it.get('product_name', '')
        qty  = it.get('quantity', 1)
        sub  = float(it.get('subtotal') or 0)
        dlbl = (it.get('discount_label') or '').strip()
        # 名稱與金額同行：('__lr__', 左文, 右文, font, margin_top)
        rows.append(('__lr__', f'{name} x{qty}', f'${sub:.0f}', f_item, 8))
        if dlbl:
            add(f'  ({dlbl})', f_disc, 'left', 0)
    sep()
    if redeem_amount > 0:
        add(f'原價  ${original_total:.0f}', f_normal, 'right', 6)
        add(f'點數折抵  -${redeem_amount:.0f}', f_normal, 'right', 4)
    add(f'合計  ${total:.0f}', f_large, 'right', 10)
    if order_status in ('pending', 'confirmed'):
        add('未結帳', f_normal, 'left', 8)
    else:
        add(f'已結帳  {pay_str}', f_normal, 'left', 8)
        if payment == 'cash' and cash_received:
            add(f'收款  ${cash_received:.0f}', f_normal, 'left', 4)
            add(f'找零  ${change:.0f}', f_normal, 'left', 4)
    if user_note:
        sep()
        add('備註', f_small, 'center', 4)
        for raw_line in user_note.split('\n'):
            for part in wrap_to_fit(raw_line.strip(), f_small, PAPER_W - 40):
                if part:
                    add(part, f_small, 'left', 4)
    add('', f_normal, 'left', 32)

    # ── 計算總高 ──
    total_h = 0
    for row in rows:
        if row[0] == '__lr__':
            _, ltxt, rtxt, font, mt = row
            rb = font.getbbox(rtxt); rw = rb[2] - rb[0]
            name_max_w = PAPER_W - rw - 48  # 左右各20 + 間距8
            total_h += mt
            if _w(ltxt, font) <= name_max_w:
                bb = font.getbbox(ltxt)
                total_h += (bb[3] - bb[1]) + 10
            else:
                for nl in wrap_to_fit(ltxt, font, name_max_w):
                    bb = font.getbbox(nl)
                    total_h += (bb[3] - bb[1]) + 10
        else:
            text, font, align, mt = row
            total_h += mt
            bb = font.getbbox(text)
            total_h += (bb[3] - bb[1]) + 10

    # ── 灰階渲染 → 1-bit ──
    img = Image.new('L', (PAPER_W, total_h), 255)
    draw = ImageDraw.Draw(img)
    y = 0
    for row in rows:
        if row[0] == '__lr__':
            _, ltxt, rtxt, font, mt = row
            y += mt
            rb  = font.getbbox(rtxt); rw = rb[2] - rb[0]
            name_max_w = PAPER_W - rw - 48
            if _w(ltxt, font) <= name_max_w:
                bb = font.getbbox(ltxt); h_t = bb[3] - bb[1]
                draw.text((20, y), ltxt, font=font, fill=0)
                draw.text((PAPER_W - rw - 20, y), rtxt, font=font, fill=0)
                y += h_t + 10
            else:
                name_lines = wrap_to_fit(ltxt, font, name_max_w)
                for i, nl in enumerate(name_lines):
                    bb = font.getbbox(nl); h_t = bb[3] - bb[1]
                    draw.text((20, y), nl, font=font, fill=0)
                    if i == len(name_lines) - 1:
                        draw.text((PAPER_W - rw - 20, y), rtxt, font=font, fill=0)
                    y += h_t + 10
        else:
            text, font, align, mt = row
            y += mt
            bb   = font.getbbox(text)
            w_t  = bb[2] - bb[0]
            h_t  = bb[3] - bb[1]
            x = (PAPER_W - w_t) // 2 if align == 'center' else (PAPER_W - w_t - 20 if align == 'right' else 20)
            draw.text((x, y), text, font=font, fill=0)
            y += h_t + 10

    img1 = img.point(lambda p: 0 if p < 180 else 255, '1')
    pixels = img1.load()

    # ── GS v 0 raster ──
    byte_w = (PAPER_W + 7) // 8
    buf = bytearray()
    buf += bytes([0x1B, 0x40])
    buf += bytes([0x1D, 0x76, 0x30, 0x00,
                  byte_w & 0xFF, (byte_w >> 8) & 0xFF,
                  total_h & 0xFF, (total_h >> 8) & 0xFF])
    for row in range(total_h):
        for cb in range(byte_w):
            bv = 0
            for bit in range(8):
                col = cb * 8 + bit
                if col < PAPER_W and pixels[col, row] == 0:
                    bv |= (0x80 >> bit)
            buf.append(bv)
    buf += bytes([0x1B, 0x64, 0x05])
    buf += bytes([0x1D, 0x56, 0x41, 0x00])

    # ── 號碼牌取號單（第二張）──
    def _make_ticket_buf():
        f_t_brand = ImageFont.truetype(FONT_BOLD, 72, index=0)
        f_t_label = ImageFont.truetype(FONT_BOLD, 44, index=0)
        f_t_num   = ImageFont.truetype(FONT_BOLD, 160, index=0)
        f_t_info  = ImageFont.truetype(FONT_BOLD, 38, index=0)

        trows = []
        def tadd(text, font, align='center', mt=0):
            trows.append((text, font, align, mt))
        def tsep():
            tadd('- ' * 22, f_t_info, 'left', 6)

        tadd('桶江軍', f_t_brand, 'center', 20)
        tadd('顧客號碼', f_t_label, 'center', 8)
        tsep()
        tadd(f'#{queue_number}' if queue_number else '#--', f_t_num, 'center', 10)
        tsep()
        tadd(f'訂單  {order_number}', f_t_info, 'left', 6)
        tadd(f'時間  {created_at}', f_t_info, 'left', 4)
        tadd('', f_t_info, 'left', 32)

        th = 0
        for text, font, align, mt in trows:
            th += mt
            bb = font.getbbox(text)
            th += (bb[3] - bb[1]) + 10

        timg = Image.new('L', (PAPER_W, th), 255)
        tdraw = ImageDraw.Draw(timg)
        y = 0
        for text, font, align, mt in trows:
            y += mt
            bb = font.getbbox(text)
            w_t = bb[2] - bb[0]
            h_t = bb[3] - bb[1]
            x = (PAPER_W - w_t) // 2 if align == 'center' else (PAPER_W - w_t - 20 if align == 'right' else 20)
            tdraw.text((x, y), text, font=font, fill=0)
            y += h_t + 10

        timg1 = timg.point(lambda p: 0 if p < 180 else 255, '1')
        tpix = timg1.load()
        byte_w2 = (PAPER_W + 7) // 8
        tbuf = bytearray()
        tbuf += bytes([0x1B, 0x40])
        tbuf += bytes([0x1D, 0x76, 0x30, 0x00,
                       byte_w2 & 0xFF, (byte_w2 >> 8) & 0xFF,
                       th & 0xFF, (th >> 8) & 0xFF])
        for row in range(th):
            for cb in range(byte_w2):
                bv = 0
                for bit in range(8):
                    col = cb * 8 + bit
                    if col < PAPER_W and tpix[col, row] == 0:
                        bv |= (0x80 >> bit)
                tbuf.append(bv)
        tbuf += bytes([0x1B, 0x64, 0x05])
        tbuf += bytes([0x1D, 0x56, 0x41, 0x00])
        return tbuf

    def _send_buf(data):
        """送出 ESC/POS buffer 到 proxy（9200），單次連線上限 ~64KB，分批送避免印表機 buffer overflow"""
        s = _sock.create_connection((PRINT_HOST, PRINT_PORT), timeout=5)
        s.sendall(bytes(data))
        s.close()

    try:
        _send_buf(buf)                          # 第一張：收銀收據
        if queue_number:
            import time as _time
            _time.sleep(0.8)                    # 等印表機處理完收據再送號碼牌
            _send_buf(_make_ticket_buf())        # 第二張：號碼牌
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ──── ePOS-Print HTTP 支援（for UberEats TM-m30III 第二台平板）────────────────

@app.route('/epson_eposdevice/getDeviceList.cgi', methods=['GET', 'POST', 'OPTIONS'])
def epos_device_list():
    """回傳印表機裝置清單，讓 UberEats app 識別 ePOS 印表機"""
    return jsonify([
        {"deviceId": "local_printer", "deviceType": "type_printer", "modelName": "TM-m30III"}
    ])

@app.route('/cgi-bin/epos/service.cgi', methods=['GET', 'POST', 'OPTIONS'])
def epos_service():
    """ePOS-Print SOAP proxy：轉發到真實印表機，回傳結果給平板"""
    import urllib.request as _req, ssl
    PRINTER_EPOS = 'https://192.168.1.124/cgi-bin/epos/service.cgi'
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    body = request.get_data()
    soap_action = request.headers.get('SOAPAction', '""')
    content_type = request.content_type or 'text/xml; charset=utf-8'

    req = _req.Request(PRINTER_EPOS, data=body,
                       headers={'Content-Type': content_type,
                                'SOAPAction': soap_action},
                       method='POST')
    try:
        with _req.urlopen(req, context=ctx, timeout=10) as resp:
            result = resp.read()
            resp_ct = resp.headers.get('Content-Type', 'text/xml; charset=utf-8')
        return result, 200, {'Content-Type': resp_ct,
                             'Access-Control-Allow-Origin': '*',
                             'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                             'Access-Control-Allow-Headers': 'Content-Type, SOAPAction'}
    except Exception as e:
        return f'<error>{e}</error>', 500



@app.route('/epson_eposdevice/getSystemPortList.cgi', methods=['GET', 'POST', 'OPTIONS'])
def epos_system_port_list():
    return app.response_class(
        response='{"WebSocket":8008,"SSLWebSocket":8043,"NativeSocket":8009,"SSLNativeSocket":8143}',
        status=200,
        mimetype='application/json'
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

