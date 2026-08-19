"""将 diagrams.html 中的每张 Mermaid 图独立渲染为 PNG"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

DOCS = Path(__file__).parent

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await page.goto(f"file://{DOCS / 'diagrams.html'}", wait_until="networkidle")

        # 等所有 SVG 渲染完
        await page.wait_for_function("() => document.querySelectorAll('.mermaid svg').length >= 2")
        await asyncio.sleep(2)

        cards = await page.query_selector_all(".card")
        print(f"找到 {len(cards)} 个 card")

        names = ["diagram_state_machine", "diagram_architecture"]
        for i, card in enumerate(cards):
            path = DOCS / f"{names[i]}.png"
            await card.screenshot(path=str(path))
            print(f"  ✅ {path.name} ({path.stat().st_size // 1024} KB)")

        await browser.close()
        print("完成。")

asyncio.run(main())
