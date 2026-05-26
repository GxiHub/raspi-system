# test_render.py — 對照 receipt_preview.html CSS 規格
# HTML 280px 寬 → 熱感紙 576px，scale ≈ 2.057x
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
PAPER_W   = 576
OUT       = '/tmp/receipt_test.png'

# ── 測試資料（模擬今天實際收據）──
order_number  = '20260518-0010'
queue_number  = 10
created_at    = '05/18 23:40'
is_mobile     = order_number.startswith('C')
spec_text     = '小天'
user_note     = ''
payment       = 'cash'
total         = 125
cash_received = 200
change        = 75
status        = 'completed'
customer_phone = ''
items = [
    {'name': '滷香嫩豆腐',    'qty': 1, 'price': 40,  'discount': ''},
    {'name': '鴨肉丸',        'qty': 1, 'price': 20,  'discount': ''},
    {'name': '高麗菜',        'qty': 1, 'price': 30,  'discount': ''},
    {'name': '滷香豬頭皮2包', 'qty': 1, 'price': 50,  'discount': ''},
    {'name': '水蓮菜',        'qty': 1, 'price': 30,  'discount': ''},
    {'name': '鮮木耳',        'qty': 1, 'price': 25,  'discount': ''},
    {'name': '蒸煮麵',        'qty': 1, 'price': 25,  'discount': ''},
    {'name': '北海貝',        'qty': 1, 'price': 25,  'discount': ''},
    {'name': '竹輪',          'qty': 1, 'price': 20,  'discount': ''},
]
PAY_LABEL = {'cash': '現金', 'linepay': 'LINE Pay', 'jkopay': '街口支付'}

# ── 字體（對照 receipt_preview.html CSS，scale ×2.057）──
# r-title:46px   → 96pt   .r-sub:23px    → 48pt
# r-queue:37px   → 76pt   .r-spec:30px   → 62pt
# r-total:26px   → 54pt   .r-order:21px  → 44pt
# r-item:21px    → 44pt   .r-time:18px   → 38pt
# r-disc:17px    → 36pt
f_title  = ImageFont.truetype(FONT_BOLD, 96, index=0)   # 滷味
f_sub    = ImageFont.truetype(FONT_BOLD, 48, index=0)   # 收銀收據
f_queue  = ImageFont.truetype(FONT_BOLD, 76, index=0)   # 號碼牌
f_spec   = ImageFont.truetype(FONT_BOLD, 62, index=0)   # 口味
f_large  = ImageFont.truetype(FONT_BOLD, 54, index=0)   # 合計
f_normal = ImageFont.truetype(FONT_BOLD, 44, index=0)   # 訂單/已結帳
f_item   = ImageFont.truetype(FONT_BOLD, 44, index=0)   # 品項（r-item 21px→44pt）
f_small  = ImageFont.truetype(FONT_BOLD, 38, index=0)   # 時間/來源/備註
f_disc   = ImageFont.truetype(FONT_BOLD, 36, index=0)   # 折扣標籤

rows = []
def add(text, font, align='left', mt=0):
    rows.append((text, font, align, mt))
def sep():
    add('- ' * 22, f_small, 'left', 6)
def _w(s, font):
    return font.getbbox(s)[2] - font.getbbox(s)[0]
def wrap_to_fit(text, font, max_w):
    if not text: return [text]
    out, line = [], ''
    for tok in text.split(' '):
        s = '' if not line else ' '
        cand = line + s + tok
        if _w(cand, font) <= max_w:
            line = cand; continue
        if line: out.append(line); line = ''
        if _w(tok, font) > max_w:
            cur = ''
            for ch in tok:
                if _w(cur+ch, font) > max_w and cur: out.append(cur); cur = ch
                else: cur += ch
            line = cur
        else:
            line = tok
    if line: out.append(line)
    return out

# ── 組合收據內容（對照 receipt_preview.html HTML 結構）──
add('滷味', f_title, 'center', 16)
add('收銀收據', f_sub, 'center', 8)
sep()
add(f'訂單　{order_number}', f_normal, 'left', 8)
if queue_number:
    add(f'號碼牌　#{queue_number}', f_queue, 'left', 8)
add(f'時間　{created_at}', f_small, 'left', 6)
src = '手機點餐' if is_mobile else 'POS 收銀'
add(f'來源　{src}', f_small, 'left', 4)
if customer_phone:
    add(f'電話　{customer_phone}', f_small, 'left', 4)
if spec_text:
    sep()
    add('【口味】', f_spec, 'center', 4)
    for line in spec_text.split('\n'):
        for part in wrap_to_fit(line.strip(), f_spec, PAPER_W - 40):
            if part: add(part, f_spec, 'left', 4)
sep()
for it in items:
    rows.append(('__lr__', f"{it['name']} x{it['qty']}", f"${it['price']}", f_item, 6))
    if it['discount']:
        add(f"  ({it['discount']})", f_disc, 'left', 0)
sep()
add(f'合計　${total}', f_large, 'right', 10)
if status in ('pending', 'confirmed'):
    add('未結帳', f_normal, 'left', 8)
else:
    add(f'已結帳　{PAY_LABEL.get(payment, payment)}', f_normal, 'left', 8)
    if payment == 'cash' and cash_received:
        add(f'收款　${cash_received}', f_normal, 'left', 4)
        add(f'找零　${change}', f_normal, 'left', 4)
if user_note:
    sep()
    add('備註', f_small, 'center', 4)
    for line in user_note.split('\n'):
        for part in wrap_to_fit(line.strip(), f_small, PAPER_W - 40):
            if part: add(part, f_small, 'center', 4)
add('', f_normal, 'left', 32)

# ── 計算高度 ──
total_h = 0
for row in rows:
    if row[0] == '__lr__':
        _, ltxt, rtxt, font, mt = row
        rb = font.getbbox(rtxt); rw = rb[2]-rb[0]
        name_max_w = PAPER_W - rw - 48
        total_h += mt
        if _w(ltxt, font) <= name_max_w:
            bb = font.getbbox(ltxt); total_h += (bb[3]-bb[1]) + 10
        else:
            for nl in wrap_to_fit(ltxt, font, name_max_w):
                bb = font.getbbox(nl); total_h += (bb[3]-bb[1]) + 10
    else:
        text, font, align, mt = row
        total_h += mt
        bb = font.getbbox(text); total_h += (bb[3]-bb[1]) + 10

# ── 繪製 ──
img = Image.new('L', (PAPER_W, total_h), 255)
draw = ImageDraw.Draw(img)
y = 0
for row in rows:
    if row[0] == '__lr__':
        _, ltxt, rtxt, font, mt = row
        y += mt
        rb = font.getbbox(rtxt); rw = rb[2]-rb[0]
        name_max_w = PAPER_W - rw - 48
        draw.text((PAPER_W-rw-20, y), rtxt, font=font, fill=0)
        if _w(ltxt, font) <= name_max_w:
            bb = font.getbbox(ltxt); draw.text((20, y), ltxt, font=font, fill=0)
            y += (bb[3]-bb[1]) + 10
        else:
            for nl in wrap_to_fit(ltxt, font, name_max_w):
                bb = font.getbbox(nl); draw.text((20, y), nl, font=font, fill=0)
                y += (bb[3]-bb[1]) + 10
    else:
        text, font, align, mt = row
        y += mt
        bb = font.getbbox(text); w_t = bb[2]-bb[0]; h_t = bb[3]-bb[1]
        x = (PAPER_W-w_t)//2 if align=='center' else (PAPER_W-w_t-20 if align=='right' else 20)
        draw.text((x, y), text, font=font, fill=0)
        y += h_t + 10

img.save(OUT)
print(f'OK saved to {OUT}, size={PAPER_W}x{total_h}')
