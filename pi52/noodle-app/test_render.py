import sys
sys.path.insert(0, '/home/pi52/raspi-system/pi52/noodle-app')

# 直接 import 渲染邏輯，模擬一張有換行備注的收據
from PIL import Image, ImageDraw, ImageFont

FONT_BOLD    = '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
PAPER_W      = 576
OUT          = '/tmp/receipt_test.png'

user_note  = '都不要辣 普通醬料可以加\n麵都分開\n5/15 16:45拿餐'
spec_text  = '清淡口味'
order_number = 'C0514060'
queue_number  = 60
created_at   = '05/14 21:48'

f_title  = ImageFont.truetype(FONT_BOLD, 96, index=0)
f_sub    = ImageFont.truetype(FONT_BOLD, 48, index=0)
f_queue  = ImageFont.truetype(FONT_BOLD, 76, index=0)
f_spec   = ImageFont.truetype(FONT_BOLD, 62, index=0)
f_large  = ImageFont.truetype(FONT_BOLD, 54, index=0)
f_normal = ImageFont.truetype(FONT_BOLD, 44, index=0)
f_small  = ImageFont.truetype(FONT_BOLD, 38, index=0)

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
    tokens = text.split(' ')
    for i, tok in enumerate(tokens):
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

add('滷味', f_title, 'center', 16)
add('收銀收據', f_sub, 'center', 8)
sep()
add(f'訂單  {order_number}', f_normal, 'left', 8)
add(f'號碼牌  #{queue_number}', f_queue, 'left', 8)
add(f'時間  {created_at}', f_small, 'left', 6)
if spec_text:
    sep()
    add('【口味】', f_spec, 'center', 4)
    for raw_line in spec_text.split('\n'):
        for part in wrap_to_fit(raw_line.strip(), f_spec, PAPER_W - 40):
            if part: add(part, f_spec, 'left', 4)
sep()
rows.append(('__lr__', '招牌嫩鴨血 x1', '$30', f_normal, 8))
sep()
add(f'合計  $305', f_large, 'right', 10)
add(f'付款  現金', f_normal, 'left', 8)
if user_note:
    sep()
    add('備註', f_small, 'center', 4)
    for raw_line in user_note.split('\n'):
        for part in wrap_to_fit(raw_line.strip(), f_small, PAPER_W - 40):
            if part: add(part, f_small, 'center', 4)
add('', f_normal, 'left', 32)

total_h = 0
for row in rows:
    if row[0] == '__lr__':
        _, ltxt, rtxt, font, mt = row
        total_h += mt
        bb = font.getbbox(ltxt); total_h += (bb[3]-bb[1]) + 10
    else:
        text, font, align, mt = row
        total_h += mt
        bb = font.getbbox(text); total_h += (bb[3]-bb[1]) + 10

img = Image.new('L', (PAPER_W, total_h), 255)
draw = ImageDraw.Draw(img)
y = 0
for row in rows:
    if row[0] == '__lr__':
        _, ltxt, rtxt, font, mt = row
        y += mt
        bb = font.getbbox(ltxt); h_t = bb[3]-bb[1]
        draw.text((20, y), ltxt, font=font, fill=0)
        rb = font.getbbox(rtxt); rw = rb[2]-rb[0]
        draw.text((PAPER_W-rw-20, y), rtxt, font=font, fill=0)
        y += h_t + 10
    else:
        text, font, align, mt = row
        y += mt
        bb = font.getbbox(text); w_t = bb[2]-bb[0]; h_t = bb[3]-bb[1]
        x = (PAPER_W-w_t)//2 if align=='center' else (PAPER_W-w_t-20 if align=='right' else 20)
        draw.text((x, y), text, font=font, fill=0)
        y += h_t + 10

img.save(OUT)
print(f'OK saved to {OUT}, size={PAPER_W}x{total_h}')
