#!/usr/bin/env python3
"""
UberEats Printer Proxy
- ENPC UDP 3289: 讓平板自動發現 Pi52 為 Epson TM-m30II
- TCP 9100: 接收列印資料，透過常駐連線轉發到真實印表機
- 解析 ESC/POS 訂單內容，存入 SQLite
"""
import socket, threading, time, struct, zlib, sqlite3 as _sq, os as _os, datetime as _dt
try:
    from PIL import Image as _Img
except ImportError:
    _Img = None


ORDERS_DB      = '/var/www/html/orders.db'
ORDERS_IMG_DIR = '/var/www/html/static/orders'

def _init_orders_db():
    con = _sq.connect(ORDERS_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS orders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT    NOT NULL,
        tablet_ip   TEXT,
        job_id      TEXT,
        image_path  TEXT,
        raw_size    INTEGER,
        created_ts  INTEGER
    )""")
    con.commit(); con.close()

_init_orders_db()

def parse_and_save_order(raw_data: bytes, tablet_ip: str):
    """解析 ESC/POS 點陣圖，渲染 PNG，存入 SQLite"""
    if _Img is None:
        return
    images = []
    i, n = 0, len(raw_data)
    while i < n - 20:
        if raw_data[i]==0x1D and raw_data[i+1]==0x38 and raw_data[i+2]==0x4C:
            block_len = struct.unpack_from('<I', raw_data, i+3)[0]
            if i+7+block_len > n: i += 1; continue
            block = raw_data[i+7:i+7+block_len]
            if len(block)>=11 and block[0]==0x30 and block[1]==0x70:
                width  = block[6] + block[7]*256
                height = block[8] + block[9]*256
                if 0 < width <= 4096 and 0 < height <= 10000:
                    try:
                        pixels_raw = zlib.decompress(block[10:])
                        row_b = (width+7)//8
                        img = _Img.new('L', (width, height), 255)
                        px  = img.load()
                        for y in range(min(height, len(pixels_raw)//row_b)):
                            for bx in range(row_b):
                                byte = pixels_raw[y*row_b+bx]
                                for bit in range(8):
                                    x = bx*8+bit
                                    if x < width:
                                        px[x,y] = 0 if (byte>>(7-bit))&1 else 255
                        images.append(img)
                    except Exception as e:
                        print(f'[{ts()}][訂單] 圖像解壓失敗: {e}')
            i += 7 + block_len
        else:
            i += 1

    if not images:
        return

    job_id = ''
    j = 0
    while j < n-10:
        if raw_data[j]==0x1D and raw_data[j+1]==0x28 and raw_data[j+2]==0x48:
            pL, pH = raw_data[j+3], raw_data[j+4]
            chunk  = raw_data[j+5:j+5+pL+pH*256]
            job_id = chunk.decode('ascii', errors='replace').strip('\x00')
            break
        j += 1

    total_h  = sum(im.size[1] for im in images)
    max_w    = max(im.size[0] for im in images)
    combined = _Img.new('L', (max_w, total_h), 255)
    y_off = 0
    for im in images:
        combined.paste(im, (0, y_off)); y_off += im.size[1]

    now   = _dt.datetime.now()
    fname = f'order_{now.strftime("%Y%m%d_%H%M%S")}.png'
    combined.save(_os.path.join(ORDERS_IMG_DIR, fname))

    con = _sq.connect(ORDERS_DB)
    con.execute(
        'INSERT INTO orders(received_at,tablet_ip,job_id,image_path,raw_size,created_ts) VALUES(?,?,?,?,?,?)',
        (now.strftime('%Y-%m-%d %H:%M:%S'), tablet_ip, job_id,
         f'orders/{fname}', len(raw_data), int(time.time()))
    )
    con.commit(); con.close()
    print(f'[{ts()}][訂單] 已儲存 job_id={job_id} → {fname}')

MY_IP        = "192.168.1.109"
MY_MAC       = "2ccf676b0c89"
PRINTER_IP   = "192.168.1.124"
PRINTER_PORT = 9100
TCP_PORT     = 9100
NETMASK      = bytes([0xff, 0xff, 0xff, 0x00])
GATEWAY      = bytes([0xc0, 0xa8, 0x00, 0x01])

holding_ip   = "0.0.0.0"
holding_lock = threading.Lock()

_printer_sock = [None]
_printer_lock = threading.RLock()
_printer_send_lock = threading.Lock()   # 序列化實際 sendall，防止 ESC @ reset 互搶
_tablet_sock  = [None]
_tablet_lock  = threading.Lock()

def ts():
    return time.strftime('%H:%M:%S')

# ── ESC/POS 解析器 ─────────────────────────────────────────────────────────────

def parse_escpos(data: bytes) -> str:
    """從 ESC/POS 二進位資料中萃取可讀文字"""
    result = []
    i = 0
    n = len(data)

    while i < n:
        b = data[i]

        if b == 0x1B:  # ESC
            if i + 1 >= n: break
            c = data[i + 1]
            if   c == 0x40:              i += 2   # ESC @ init
            elif c == 0x21:              i += 3   # ESC ! print mode
            elif c == 0x24:              i += 4   # ESC $ abs position
            elif c == 0x2D:              i += 3   # ESC - underline
            elif c == 0x33:              i += 3   # ESC 3 line spacing
            elif c == 0x3D:              i += 3   # ESC = select printer
            elif c == 0x41:              i += 3   # ESC A line spacing
            elif c == 0x44:              # ESC D tab stops (null-terminated)
                i += 2
                while i < n and data[i] != 0: i += 1
                i += 1
            elif c == 0x45:              i += 3   # ESC E bold
            elif c == 0x47:              i += 3   # ESC G double-strike
            elif c == 0x4A:              i += 3   # ESC J feed dots
            elif c == 0x4D:              i += 3   # ESC M font
            elif c == 0x52:              i += 3   # ESC R charset
            elif c == 0x54:              i += 3   # ESC T print area
            elif c == 0x57:              i += 8   # ESC W print area (page mode)
            elif c == 0x5C:              i += 4   # ESC \ rel position
            elif c == 0x61:              i += 3   # ESC a align
            elif c == 0x64:              i += 3   # ESC d feed lines
            elif c == 0x69:              i += 2   # ESC i cut
            elif c == 0x6D:              i += 2   # ESC m partial cut
            elif c == 0x70:              i += 5   # ESC p pulse
            elif c == 0x72:              i += 3   # ESC r color
            elif c == 0x74:              i += 3   # ESC t code page
            else:                        i += 2

        elif b == 0x1D:  # GS
            if i + 1 >= n: break
            c = data[i + 1]
            if c == 0x21:                i += 3   # GS ! char size
            elif c == 0x24:              i += 4   # GS $ abs position
            elif c == 0x38:              # GS 8 (extended)
                if i + 2 < n and data[i + 2] == 0x4C:  # GS 8 L raster image
                    if i + 6 < n:
                        length = struct.unpack_from('<I', data, i + 3)[0]
                        i += 7 + length
                    else: i += 3
                else: i += 3
            elif c == 0x42:              i += 3   # GS B reverse
            elif c == 0x48:              i += 3   # GS H barcode HRI
            elif c == 0x50:              i += 4   # GS P motion unit
            elif c == 0x54:              i += 3   # GS T print direction
            elif c == 0x56:              # GS V cut
                if i + 2 < n and data[i + 2] in (0x41, 0x42): i += 4
                else: i += 3
            elif c == 0x57:              i += 6   # GS W print area width
            elif c == 0x5C:              i += 4   # GS \ rel position
            elif c == 0x61:              i += 3   # GS a ASB enable
            elif c == 0x68:              i += 3   # GS h barcode height
            elif c == 0x6B:              # GS k barcode (variable)
                if i + 2 < n:
                    t2 = data[i + 2]
                    if t2 <= 6:   # null-terminated
                        i += 3
                        while i < n and data[i] != 0: i += 1
                        i += 1
                    else:         # length-prefixed
                        i += 3 + (data[i + 3] if i + 3 < n else 0) + 1
                else: i += 2
            elif c == 0x76:              # GS v 0 raster image (legacy)
                if i + 6 < n:
                    xb = data[i + 3] + data[i + 4] * 256
                    yl = data[i + 5] + data[i + 6] * 256
                    i += 7 + xb * yl
                else: i += 3
            elif c == 0x77:              i += 3   # GS w barcode width
            else:                        i += 2

        elif b == 0x1C:  # FS (Kanji/Chinese)
            if i + 1 >= n: break
            c = data[i + 1]
            if   c == 0x26: i += 2   # FS & Kanji on
            elif c == 0x2E: i += 2   # FS . Kanji off
            elif c == 0x21: i += 3   # FS ! char mode
            elif c == 0x2D: i += 3   # FS - underline
            elif c == 0x43: i += 3   # FS C Kanji code
            elif c == 0x53: i += 4   # FS S spacing
            elif c == 0x57: i += 3   # FS W double width
            else:           i += 2

        elif b == 0x10:  # DLE
            if i + 1 < n and data[i + 1] == 0x04: i += 3   # DLE EOT
            elif i + 1 < n and data[i + 1] == 0x14: i += 4  # DLE DC4
            else: i += 2

        elif b == 0x12:  i += 2   # DC2
        elif b == 0x0A:            # LF
            result.append('\n'); i += 1
        elif b == 0x0D:  i += 1   # CR
        elif 0x20 <= b <= 0x7E:   # printable ASCII
            result.append(chr(b)); i += 1
        elif b >= 0x80:            # multi-byte UTF-8 (Chinese)
            if b >= 0xF0 and i + 3 < n:
                try:
                    result.append(data[i:i+4].decode('utf-8')); i += 4; continue
                except: pass
            if b >= 0xE0 and i + 2 < n and (data[i+1] & 0xC0) == 0x80 and (data[i+2] & 0xC0) == 0x80:
                try:
                    result.append(data[i:i+3].decode('utf-8')); i += 3; continue
                except: pass
            if b >= 0xC0 and i + 1 < n and (data[i+1] & 0xC0) == 0x80:
                try:
                    result.append(data[i:i+2].decode('utf-8')); i += 2; continue
                except: pass
            i += 1
        else:
            i += 1

    lines = [l.strip() for l in ''.join(result).split('\n') if l.strip()]
    return '\n'.join(lines)

# ── ENPC helpers ─────────────────────────────────────────────────────────────

def make_enpc(t, f, p):
    return b'EPSON' + t.encode() + bytes.fromhex(f) + struct.pack('>I', len(p)) + p

# ── UDP 3289 listener ────────────────────────────────────────────────────────

_udp_sock_ref  = [None]
_udp_sock_lock = threading.Lock()

def udp_send(data, addr):
    with _udp_sock_lock:
        try:
            if _udp_sock_ref[0]:
                _udp_sock_ref[0].sendto(data, addr)
        except Exception as e:
            print(f"[{ts()}][UDP] 送出失敗：{e}")

def handle_enpc(data, addr, sock):
    global holding_ip
    if addr[0] == MY_IP:  # 忽略自己送出去的廣播（避免 UDP 無限迴圈）
        return
    if len(data) < 14 or data[:5] != b'EPSON':
        return
    func = data[6:10].hex()

    if func == '00000000':
        pl = bytes.fromhex(
            '55422d45454145303833454e534e'
            '000000000000000000000000000000000000'
            '0001ffff15000200' + MY_MAC + '0000000100000001')
        udp_send(make_enpc('q', '00000000', pl), addr)

    elif func == '03000000':
        model = b'TM-m30II\x00'
        pl = bytearray(133)
        pl[0:5] = bytes([0, 5, 1, 2, 1])
        pl[5:5 + len(model)] = model
        udp_send(make_enpc('q', '03000000', bytes(pl)), addr)

    elif func == '03000015':  # print status
        udp_send(make_enpc("q", "03000015", bytes(4)), addr)
        print(f"[{ts()}][UDP] PRINT_STATUS -> {addr[0]}")

    elif func == '03000016':
        # 印表機狀態查詢，回傳 OK（否則平板連上 TCP 後立刻送 FIN）
        udp_send(make_enpc("q", "03000016", bytes(4)), addr)
        print(f"[{ts()}][UDP] STATUS_CHECK -> {addr[0]}")

    elif func == '03000017':
        # 回傳實際 holding IP：已連線時回自己的 IP，空閒時回 0.0.0.0
        with holding_lock:
            h_ip = holding_ip
        payload = socket.inet_aton(h_ip) if h_ip != "0.0.0.0" else bytes(4)
        udp_send(make_enpc("q", "03000017", payload), addr)
        print(f"[{ts()}][UDP] WHO_IS_HOLDING={h_ip} -> {addr[0]}")

    elif func == '00000010':
        ip_b  = socket.inet_aton(MY_IP)
        mac_b = bytes.fromhex(MY_MAC)
        pl = b'\x01' + mac_b + b'\x00\x04' + ip_b + NETMASK + GATEWAY + b'\x80\x7c'
        udp_send(make_enpc('q', '00000010', pl), addr)

    elif func == '03000010':
        pl = bytes([0x0e, 0x14, 0x00, 0x00, 0x0f, 0xff, 0xff, 0xff, 0xff, 0x39, 0x41, 0x40, 0x00])
        udp_send(make_enpc('q', '03000010', pl), addr)

    else:
        print(f"[{ts()}][UDP] 未知 ENPC func={func} 來自 {addr[0]} data={data[:20].hex()}")

def udp_3289():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(('', 3289))
    with _udp_sock_lock:
        _udp_sock_ref[0] = s
    print(f"[{ts()}] UDP 3289 就緒（印表機探索）")
    while True:
        try:
            data, addr = s.recvfrom(1024)
            handle_enpc(data, addr, s)
        except Exception as e:
            print(f"[{ts()}][UDP] 錯誤：{e}")

# ── 常駐印表機連線 ─────────────────────────────────────────────────────────────

BROADCAST_IP = "192.168.1.255"

def broadcast_presence():
    """印表機連線成功後廣播，讓平板重新感知印表機上線（解決 proxy 重啟後平板不重連問題）"""
    def _do():
        # 等 UDP socket 就緒
        for _ in range(20):
            with _udp_sock_lock:
                if _udp_sock_ref[0]:
                    break
            time.sleep(0.5)

        # 廣播 3 次（間隔 3 秒），確保平板收到
        for i in range(3):
            # DISCOVER 回應：宣告印表機存在
            pl_disc = bytes.fromhex(
                '55422d45454145303833454e534e'
                '000000000000000000000000000000000000'
                '0001ffff15000200' + MY_MAC + '0000000100000001')
            udp_send(make_enpc('q', '00000000', pl_disc), (BROADCAST_IP, 3289))

            # DEVICE_NAME 廣播：宣告型號為 TM-m30II
            model = b'TM-m30II\x00'
            pl_name = bytearray(133)
            pl_name[0:5] = bytes([0, 5, 1, 2, 1])
            pl_name[5:5 + len(model)] = model
            udp_send(make_enpc('q', '03000000', bytes(pl_name)), (BROADCAST_IP, 3289))

            print(f"[{ts()}][BROADCAST] 廣播印表機上線通知 ({i+1}/3)")
            if i < 2:
                time.sleep(3)

    threading.Thread(target=_do, daemon=True).start()

def printer_loop():
    """保持與真實印表機的常駐 TCP 連線，自動重連；收到的資料轉發給平板"""
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
            s.connect((PRINTER_IP, PRINTER_PORT))
            with _printer_lock:
                _printer_sock[0] = s
            print(f"[{ts()}][PRINTER] 已連到 {PRINTER_IP}:{PRINTER_PORT}（常駐）")
            broadcast_presence()  # 主動通知所有平板印表機已上線

            while True:
                data = s.recv(4096)
                if not data:
                    print(f"[{ts()}][PRINTER] 印表機關閉連線")
                    break
                # 過濾 DLE_EOT 心跳回應（避免轉發給平板造成 connect-storm）
                if len(data) <= 4 and data[0:1] == bytes([0x10]):
                    continue
                print(f"[{ts()}][印表機→平板] {len(data)} bytes: {data[:40].hex()}")
                with _tablet_lock:
                    t = _tablet_sock[0]
                if t:
                    try:
                        t.sendall(data)
                    except Exception as e:
                        print(f"[{ts()}][印表機→平板] 送出失敗：{e}")

        except Exception as e:
            print(f"[{ts()}][PRINTER] {e}")
        finally:
            with _printer_lock:
                _printer_sock[0] = None
        print(f"[{ts()}][PRINTER] 5 秒後重連...")
        time.sleep(5)

# ── TCP 9100 proxy ────────────────────────────────────────────────────────────

def handle_conn(conn, addr):
    global holding_ip
    print(f"[{ts()}][TCP] 連線來自 {addr[0]}")

    with holding_lock:
        holding_ip = addr[0]
    print(f"[{ts()}][TCP] WHO_IS_HOLDING 設為 {addr[0]}")

    # 等待印表機連線（最多 15 秒），避免重連空窗期漏掉第一單
    for _ in range(15):
        with _printer_lock:
            printer = _printer_sock[0]
        if printer:
            break
        time.sleep(1)
    if not printer:
        print(f"[{ts()}][TCP] 印表機未連線，拒絕平板連線")
        conn.close()
        with holding_lock:
            if holding_ip == addr[0]:
                holding_ip = "0.0.0.0"
        return

    with _tablet_lock:
        _tablet_sock[0] = conn

    job_buffer = bytearray()

    try:
        while True:
            conn.settimeout(300)  # 5 分鐘無資料才超時
            try:
                data = conn.recv(4096)
            except OSError as recv_err:
                print(f"[{ts()}][TCP] recv 錯誤 {addr[0]}: {recv_err}")
                break
            if not data:
                print(f"[{ts()}][TCP] 平板送出 FIN（正常關閉） {addr[0]}")
                break
            job_buffer.extend(data)
            print(f"[{ts()}][平板→印表機] {len(data)} bytes: {data[:40].hex()}")
            with _printer_lock:
                p = _printer_sock[0]
            if p:
                try:
                    with _printer_send_lock:      # 防止與本機列印 ESC @ 互 reset
                        p.sendall(data)
                except Exception as e:
                    print(f"[{ts()}][平板→印表機] 送出失敗：{e}")
    except OSError:
        pass
    except Exception as e:
        print(f"[{ts()}][平板→印表機] 結束：{e}")
    finally:
        # TCP 連線關閉時儲存完整訂單圖片（避免裁紙指令在 binary 中誤判導致截斷）
        if len(job_buffer) > 20:
            snapshot = bytes(job_buffer)
            tablet   = addr[0]
            threading.Thread(target=parse_and_save_order,
                             args=(snapshot, tablet), daemon=True).start()
            text = parse_escpos(snapshot)
            if text:
                print(f"[{ts()}][訂單內容]\n{text}\n{'─'*40}")

        print(f"[{ts()}][TCP] 平板斷線 {addr[0]}")
        with _tablet_lock:
            if _tablet_sock[0] is conn:
                _tablet_sock[0] = None
        try:
            conn.close()
        except:
            pass
        with holding_lock:
            if holding_ip == addr[0]:
                holding_ip = "0.0.0.0"
                print(f"[{ts()}][TCP] WHO_IS_HOLDING 釋放（印表機連線保持）")

def tcp_9100():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', TCP_PORT))
    s.listen(5)
    print(f"[{ts()}] TCP 9100 就緒（接收列印資料）")
    while True:
        try:
            conn, addr = s.accept()
            threading.Thread(target=handle_conn, args=(conn, addr), daemon=True).start()
        except Exception as e:
            print(f"[{ts()}][TCP] Accept 錯誤：{e}")



# ── 印表機心跳（每 60 秒送 DLE EOT，防止 90 秒 idle 斷線）──────────────────
def printer_heartbeat():
    DLE_EOT = bytes([0x10, 0x04, 0x01])
    while True:
        time.sleep(60)
        with _printer_lock:
            p = _printer_sock[0]
        if p:
            try:
                p.sendall(DLE_EOT)
            except Exception:
                pass  # printer_loop 會偵測斷線並重連

# ── 本機列印注入 port 9200 ──────────────────────────────────────────────────
def local_print_server():
    """127.0.0.1:9200 — 接收來自 Flask 的 ESC/POS 資料，透過現有 printer socket 送出"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', 9200))
    s.listen(3)
    print(f"[{ts()}] 本機列印注入 port 9200 就緒")
    while True:
        try:
            conn, addr = s.accept()
            threading.Thread(target=_handle_local_print, args=(conn,), daemon=True).start()
        except Exception as e:
            print(f"[{ts()}][9200] Accept 錯誤：{e}")

def _handle_local_print(conn):
    try:
        buf = bytearray()
        conn.settimeout(5)
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
        conn.close()
        if not buf:
            return
        with _printer_lock:
            p = _printer_sock[0]
        if p:
            try:
                with _printer_send_lock:          # 防止多份收據 ESC @ 互 reset
                    p.sendall(bytes(buf))
                print(f"[{ts()}][本機列印] 送出 {len(buf)} bytes 透過現有連線")
            except Exception as e:
                print(f"[{ts()}][本機列印] 送出失敗：{e}")
        else:
            print(f"[{ts()}][本機列印] 印表機未連線，略過")
    except Exception as e:
        print(f"[{ts()}][本機列印] 錯誤：{e}")


# ── WebSocket / NativeSocket relay（port 8008 / 8009 → 真實印表機）────────────
def _handle_relay(conn, addr, remote_port):
    print(f"[{ts()}][relay:{remote_port}] 連線來自 {addr[0]}")
    try:
        remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote.connect((PRINTER_IP, remote_port))
        stop = threading.Event()
        def fwd(src, dst, label):
            try:
                while not stop.is_set():
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            stop.set()
        t1 = threading.Thread(target=fwd, args=(conn, remote, 'tablet→printer'), daemon=True)
        t2 = threading.Thread(target=fwd, args=(remote, conn, 'printer→tablet'), daemon=True)
        t1.start(); t2.start()
        stop.wait()
    except Exception as e:
        print(f"[{ts()}][relay:{remote_port}] 錯誤：{e}")
    finally:
        try: conn.close()
        except: pass
        try: remote.close()
        except: pass
    print(f"[{ts()}][relay:{remote_port}] 斷線 {addr[0]}")

def relay_server(local_port, remote_port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', local_port))
    s.listen(5)
    print(f"[{ts()}] relay port {local_port} → {PRINTER_IP}:{remote_port} 就緒")
    while True:
        try:
            conn, addr = s.accept()
            threading.Thread(target=_handle_relay, args=(conn, addr, remote_port), daemon=True).start()
        except Exception as e:
            print(f"[{ts()}][relay:{local_port}] Accept 錯誤：{e}")

# ── Main ─────────────────────────────────────────────────────────────────────

print("=" * 50)
print("  UberEats Printer Proxy")
print(f"  Mac IP    : {MY_IP}")
print(f"  印表機 IP : {PRINTER_IP}:{PRINTER_PORT}")
print("=" * 50)

threading.Thread(target=printer_loop, daemon=True).start()
threading.Thread(target=udp_3289,     daemon=True).start()
threading.Thread(target=tcp_9100,     daemon=True).start()
threading.Thread(target=local_print_server, daemon=True).start()
threading.Thread(target=relay_server, args=(8008, 8008), daemon=True).start()
threading.Thread(target=relay_server, args=(8009, 8009), daemon=True).start()
threading.Thread(target=printer_heartbeat,  daemon=True).start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[停止]")
