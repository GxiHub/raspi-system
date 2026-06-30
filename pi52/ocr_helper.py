#!/usr/bin/env python3
"""OCR helper v3: GPT-4o Vision 解析 UberEats 訂單圖片"""
import sys, json, base64, os, re

def load_key():
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_file):
        for line in open(env_file):
            if line.startswith('OPENAI_API_KEY='):
                return line.strip().split('=', 1)[1]
    return os.environ.get('OPENAI_API_KEY', '')

def parse_with_gpt(img_path):
    from openai import OpenAI

    with open(img_path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()

    client = OpenAI(api_key=load_key())

    MENU = """【肉類】豬腱肉、滷大腸、人氣半筋半肉、古早味香腸、雞心、豬皮、豬耳朵、雞翅、鴨胗、嫩土雞肝、QQ土雞皮、特製豬肝片、滷香雞腳、嫩土雞屁股、吮指土雞脖子、必吃排骨酥、德國煙燻香腸、滷香豬頭皮、豬肉片
【豆類】滷香嫩豆腐、小豆干、綜合滷香嫩豆腐/鴨血、手工凍豆腐、大豆皮、蘭花干、炸豆包、生豆包、大溪豆干、百頁嫩豆腐
【小菜】海帶、人氣豬血糕、招牌嫩鴨血、滷麵腸、活力滷蛋白、南部甜不辣、油條、竹輪、芋條、甜不辣、黑輪、大顆干貝燒、Q彈蒟蒻片、高雄黑輪、韓式魚板
【火鍋料】龍蝦沙拉、章魚燒丸子、墨魚花枝丸、特級原味貢丸、特級香菇貢丸、芋角鮮肉丸、北海貝、黃金脆魚蛋、鮮脆鱈魚丸、韓式年糕、鑫鑫腸、素腰花、蒟蒻絲、水晶餃、魚餃、鴨肉丸、好吃糯米腸、鳥蛋
【日式火鍋料】京之干貝、長蟹味棒、京之蟹角
【蔬菜】高麗菜、花椰菜、白蘿蔔、水蓮菜、娃娃菜、大陸妹、空心菜、洋蔥、營養地瓜葉、脆口豆芽菜、鮮香菇、絲瓜、滿滿植化素節瓜、脆脆杏鮑菇、小黃瓜、青椒、小玉米、金針菇、鮮木耳、白花椰菜、鮮脆四季豆、滷香茄子、滷秋葵、紅蘿蔔
【麵條】王子麵、想念麵疙瘩、非吃不可韓式Q冬粉、超好吃粄條、韓式Q麵、手工刀削麵、日式烏龍麵、蒸煮麵、鍋燒意麵、細冬粉、寬冬粉、日曬QQ麵、維力滷味麵、雞絲麵"""

    prompt = """請仔細閱讀這張 UberEats 收據圖片，回傳 JSON：

{
  "ue_num": "黑色標題區右側的號碼（如 13487 或 BC5E3，純數字也要取）",
  "customer": "客人姓名",
  "items": [{"qty": 數量, "name": "品名", "price": 單價}],
  "total": 已付金額
}

規則：
- 品名直接抄圖片上的中文，去掉英文部分
- 不要自行替換或猜測品名，看到什麼寫什麼
- $0 的辣度/備註選項也要列入
- price 是該行金額除以數量（單價）
- total 取「已付金額」
- 只回傳 JSON"""

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{b64}",
                    "detail": "high"
                }}
            ]
        }],
        max_tokens=800
    )

    text = resp.choices[0].message.content.strip()
    # 去掉 markdown code block
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

# 已知熱感紙字形模糊導致的固定錯誤修正
OCR_FIXES = {
    '玉子麵': '王子麵',
    '活力蔬菜白': '活力滷蛋白',
    '活力漸蛋白': '活力滷蛋白',
    '活力嫩蛋白': '活力滷蛋白',
    '脆脆得捲': '脆脆杏鮑菇',
    '豬蹄肉': '豬腱肉',
}

if __name__ == "__main__":
    img_path = sys.argv[1]
    try:
        result = parse_with_gpt(img_path)
        for item in result.get('items', []):
            item['en'] = ''
            item['raw'] = item.get('name', '')
            item['name'] = OCR_FIXES.get(item['name'], item['name'])
        print(json.dumps(result, ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"error": str(e), "items": [], "total": None, "ue_num": "", "customer": ""}))
