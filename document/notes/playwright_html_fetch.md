# PlaywrightでD1-Law系HTMLを取得するメモ

D1-Lawの`opensearch`系URLは`curl -L`だとリダイレクトループになるケースがある。
Playwright (Chromium) ではHTML本文を取得できた。

## 検証済みURL例

- https://ops-jg.d1-law.com/opensearch/SrJbF01/init?jctcd=8A91CB43E1&houcd=H429901010014&no=1&totalCount=3&fromJsp=SrMj

## 事前準備

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

## 取得スニペット

```python
from playwright.sync_api import sync_playwright

url = "https://ops-jg.d1-law.com/opensearch/SrJbF01/init?jctcd=8A91CB43E1&houcd=H429901010014&no=1&totalCount=3&fromJsp=SrMj"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    text = page.inner_text("body")
    print(text[:500])
    browser.close()
```

## 備考

- HTML内に条文テキストが含まれているケースがある。
- 取得後は本文抽出（タグ除去、改行整形）を検討する。
