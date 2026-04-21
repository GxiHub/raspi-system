#!/usr/bin/env python3
"""
UberEats 自動整合 v2 - 在 pi53 上跑
每 5 秒 SSH 進 pi52 抓新訂單 → OCR → 建到 luwei-manager DB
"""
import sqlite3, time, subprocess, re, os, json, tempfile
from datetime import datetime

PI52_HOST  = 'pi52@100.98.225.85'
LUWEI_DB   = '/home/pi53/luwei-manager/instance/luwei.db'
SEEN_FILE  = '/home/pi53/logs/ue_seen.json'
os.makedirs('/home/pi53/logs', exist_ok=True)

PRODUCT_MAP = {
    'Instant Noodles':               (80,  '王子麵'),
    'Sliced Pork':                   (3,   '豬肉片'),
    'Tempura':                       (94,  '甜不辣'),
    'Special Original Meatball':     (26,  '特級原味貢丸'),
    'Small Tofu Curd':               (69,  '小豆干'),
    'Bean Curd Sheet':               (102, '大豆皮'),
    'Pig Ear':                       (6,   '豬耳朵'),
    'Baby Corn':                     (42,  '小玉米'),
    'Popular Pig Blood Rice Cake':   (90,  '人氣豬血糕'),
    'Pork Shank':                    (5,   '豬腱肉'),
    'Green Pepper':                  (49,  '青椒'),
    'Pork Intestine':                (4,   '滷大腸'),
    'Chicken Heart':                 (14,  '雞心'),
    'Chicken Wing':                  (12,  '雞翅'),
    'Water Spinach':                 (97,  '空心菜'),
    'Cabbage':                       (41,  '高麗菜'),
    'Signature Duck Blood Jelly':    (84,  '招牌嫩鴨血'),
    'Duck Blood Jelly':              (84,  '招牌嫩鴨血'),
    'Tofu Curd':                     (69,  '小豆干'),
}

def ts():
    return datetime.now().strftime('%H:%M:%S')

def load_seen():
    try: return set(json.load(open(SEEN_FILE)))
    except: return set()

def save_seen(seen):
    json.dump(list(seen), open(SEEN_FILE, 'w'))

def ssh_get_new_orders(seen):
    """從 pi52 撈最新 10 筆，過濾掉已知的"""
    cmd = ['ssh', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=no',
           PI52_HOST,
           "python3 -c \""
           "import sqlite3,json;"
           "c=sqlite3.connect('/var/www/html/orders.db');"
           "rows=c.execute('SELECT id,received_at,tablet_ip,job_id,image_path FROM orders ORDER BY id DESC LIMIT 20').fetchall();"
           "print(json.dumps(rows));"
           "c.close()\""]
    try:
        out = subprocess.check_output(cmd, timeout=15).decode()
        rows = json.loads(out)
        return [r for r in rows if str(r[0]) not in seen]
    except Exception as e:
        print(f'[{ts()}] SSH 錯誤: {e}')
        return []

def download_image(remote_path):
    tmp = tempfile.mktemp(suffix='.png')
    cmd = ['scp', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=no',
           f'{PI52_HOST}:/var/www/html/static/{remote_path}', tmp]
    try:
        subprocess.check_call(cmd, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return tmp
    except Exception as e:
        print(f'[{ts()}] SCP 錯誤: {e}')
        return None

def ocr_on_pi52(remote_img_path):
    """在 pi52 上跑 OCR helper，回傳 parsed dict"""
    cmd = ['ssh', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=no',
           PI52_HOST, f'python3 /home/pi52/ocr_helper.py /var/www/html/static/{remote_img_path}']
    try:
        out = subprocess.check_output(cmd, timeout=30)
        text = out.decode('utf-8', errors='replace')
        return json.loads(text)
    except Exception as e:
        print(f'[{ts()}] OCR on pi52 失敗: {e}')
        return {'items': [], 'total': None, 'ue_num': ''}

def ocr_image(img_path):
    return ''  # 不再使用本地 OCR

def parse_order(text):
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    items, total, ue_order_num = [], None, ''

    for i, line in enumerate(lines):
        # UberEats 訂單號（如 A48BE）
        if not ue_order_num:
            m = re.search(r'\b([A-Z][A-Z0-9]{4,7})\b', line)
            if m and 'Uber' not in line and 'Eats' not in line:
                ue_order_num = m.group(1)

        # 品項行：N x 名稱 $price 或 N x 名稱
        m = re.match(r'(\d+)\s*[xX×]\s+(.+?)(?:\s+\$?([\d,]+\.?\d*))?$', line)
        if m:
            qty = int(m.group(1))
            name_raw = m.group(2).strip()
            price_str = m.group(3)
            price = float(price_str.replace(',','')) if price_str else None

            # 英文品名
            en_m = re.search(r'[A-Z][a-zA-Z\s]{3,}', name_raw)
            en_name = en_m.group().strip() if en_m else ''

            # 跨行英文品名（如 Special\nOriginal Meatball）
            if not price and i+1 < len(lines):
                next_l = lines[i+1]
                if re.match(r'^[A-Z]', next_l) and '$' not in next_l and 'x ' not in next_l:
                    en_name = (en_name + ' ' + next_l).strip()

            items.append({'qty': qty, 'en_name': en_name, 'price': price, 'raw': name_raw})

        # 已付金額
        if re.search(r'已付|付金額', line):
            pm = re.search(r'\$?([\d,]+)\.?\d*', line)
            if pm: total = float(pm.group(1).replace(',',''))

    return {'items': items, 'total': total, 'ue_order_num': ue_order_num}


def map_items(raw_items):
    result = []
    for it in raw_items:
        en = it.get("en", it.get("en_name", ""))
        prod = None
        for key, val in PRODUCT_MAP.items():
            if key.lower() in en.lower() or en.lower() in key.lower():
                prod = val
                break
        result.append({
            "id": prod[0] if prod else None,
            "name": prod[1] if prod else it.get("raw", en)[:20],
            "price": it.get("price") or 0,
            "qty": it.get("qty", 1)
        })
    return result

def map_and_create(order_data, row):
    # 避免重複建立同一筆 UberEats 訂單
    ue_num = order_data.get('ue_order_num') or order_data.get('ue_num') or row[3]
    if ue_num:
        check_conn = sqlite3.connect(LUWEI_DB, timeout=5)
        existing = check_conn.execute(
            "SELECT id FROM orders WHERE order_number=?", (f'UE-{ue_num}',)
        ).fetchone()
        check_conn.close()
        if existing:
            print(f'[{ts()}] 跳過重複訂單 UE-{ue_num} (已存在 id={existing[0]})')
            return f'UE-{ue_num}', existing[0]

    mapped_items = []
    for it in order_data['items']:
        en = it.get('en_name', it.get('en', ''))
        prod = None
        for key, val in PRODUCT_MAP.items():
            if key.lower() in en.lower() or en.lower() in key.lower():
                prod = val; break
        mapped_items.append({
            'id': prod[0] if prod else None,
            'name': prod[1] if prod else it['raw'][:20],
            'price': it['price'] or 0,
            'qty': it['qty']
        })

    conn = sqlite3.connect(LUWEI_DB, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    today = datetime.now().strftime('%Y-%m-%d')
    now = datetime.now()

    max_q = conn.execute(
        'SELECT MAX(queue_number) FROM orders WHERE DATE(created_at)=?', (today,)
    ).fetchone()[0] or 0
    queue = max_q + 1

    ue_num = order_data.get('ue_order_num') or order_data.get('ue_num') or row[3]
    order_num = f'UE-{ue_num}' if ue_num else f'UE{now.strftime("%m%d")}{queue:03d}'
    total = order_data['total'] or sum(it['price']*it['qty'] for it in mapped_items)
    original = sum(it['price']*it['qty'] for it in mapped_items)
    note = f'UberEats 外送 | 訂單號：{ue_num} | 平板：{row[2]}'

    conn.execute('''INSERT INTO orders
      (order_number,total_amount,original_total,payment_method,status,note,
       queue_number,kitchen_status,created_at,customer_phone)
      VALUES (?,?,?,?,?,?,?,?,?,?)''',
      (order_num, total, original, 'ubereats', 'completed',
       note, queue, 'picking', now.strftime('%Y-%m-%d %H:%M:%S'), order_data.get('customer') or None))
    order_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    for it in mapped_items:
        if it['id']:
            conn.execute('''INSERT INTO order_items
              (order_id,product_id,product_name,unit_price,quantity,subtotal)
              VALUES (?,?,?,?,?,?)''',
              (order_id, it['id'], it['name'], it['price'], it['qty'], it['price']*it['qty']))

    conn.commit(); conn.close()
    return order_num, order_id

# ── Main ──
seen = load_seen()
print(f'[{ts()}] UberEats 自動整合 v2 啟動（pi53）, 已知 {len(seen)} 筆')

while True:
    try:
        new_rows = ssh_get_new_orders(seen)
        for row in new_rows:
            rid = str(row[0])
            seen.add(rid); save_seen(seen)
            print(f'[{ts()}] 新訂單 id={row[0]} job={row[3]} from={row[2]}')

            data = ocr_on_pi52(row[4])
            ue_num = data.get("ue_num", "")
            data["ue_order_num"] = ue_num
            data["customer"] = data.get("customer", "")
            items_count = len(data.get("items",[]))
            print(f"[{ts()}] 品項: {items_count} 金額: {data.get("total")} UE#: {ue_num}")

            if data.get("items"):
                num, oid = map_and_create(data, row)
                print(f"[{ts()}] ✅ {num} (id={oid}) 已建立")
            else:
                print(f"[{ts()}] ⚠️  無品項，略過")
    except Exception as e:
        print(f'[{ts()}] 主迴圈錯誤: {e}')
    time.sleep(5)
