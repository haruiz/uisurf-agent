from playwright.async_api import Page

class WebpageTextUtilsPlaywright:
    async def get_all_webpage_text(self, page: Page, n_lines: int = 50) -> str:
        text = await page.evaluate("() => document.body.innerText")
        return "\n".join(text.split("\n")[:n_lines])

    async def get_visible_text(self, page: Page) -> str:
        # Simplified visible text extraction
        return await page.evaluate("() => document.body.innerText")

    async def get_page_markdown(self, page: Page, max_tokens: int = -1) -> str:
        # Mock markdown conversion
        text = await page.evaluate("() => document.body.innerText")
        return f"# {await page.title()}\n\n{text}"
