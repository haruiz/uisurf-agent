import asyncio

from playwright.async_api import Page


ANIMATION_BOOTSTRAP_JS = """
(() => {
  if (window.__adkwsAnim) return;
  const root = document.createElement('div');
  root.id = '__adkws_anim_root';
  root.style.position = 'fixed';
  root.style.left = '0';
  root.style.top = '0';
  root.style.width = '100vw';
  root.style.height = '100vh';
  root.style.pointerEvents = 'none';
  root.style.zIndex = '2147483647';

  const cursor = document.createElement('div');
  cursor.id = '__adkws_cursor';
  cursor.style.position = 'fixed';
  cursor.style.width = '16px';
  cursor.style.height = '16px';
  cursor.style.marginLeft = '-8px';
  cursor.style.marginTop = '-8px';
  cursor.style.border = '2px solid #00c2ff';
  cursor.style.borderRadius = '50%';
  cursor.style.background = 'rgba(0,194,255,0.2)';
  cursor.style.boxShadow = '0 0 10px rgba(0,194,255,0.8)';
  cursor.style.transition = 'transform 120ms ease-out, opacity 120ms ease-out';
  cursor.style.opacity = '0';

  root.appendChild(cursor);
  document.documentElement.appendChild(root);

  window.__adkwsAnim = {
    cursor,
    root,
    setCursor(x, y) {
      cursor.style.left = `${x}px`;
      cursor.style.top = `${y}px`;
      cursor.style.opacity = '1';
    },
    pulseAt(x, y, color = '#00c2ff') {
      const ring = document.createElement('div');
      ring.style.position = 'fixed';
      ring.style.left = `${x}px`;
      ring.style.top = `${y}px`;
      ring.style.width = '14px';
      ring.style.height = '14px';
      ring.style.marginLeft = '-7px';
      ring.style.marginTop = '-7px';
      ring.style.border = `2px solid ${color}`;
      ring.style.borderRadius = '50%';
      ring.style.opacity = '0.95';
      ring.style.transform = 'scale(1)';
      ring.style.transition = 'transform 360ms ease-out, opacity 360ms ease-out';
      root.appendChild(ring);
      requestAnimationFrame(() => {
        ring.style.transform = 'scale(4.2)';
        ring.style.opacity = '0';
      });
      setTimeout(() => ring.remove(), 420);
    }
  };
})();
"""


class AnimationUtilsPlaywright:
    def __init__(self):
        self.last_cursor_position = (0, 0)

    async def add_cursor_box(self, page: Page, identifier: str):
        await self._ensure_animation_layer(page)

    async def remove_cursor_box(self, page: Page, identifier: str):
        await page.evaluate(
            """() => {
                if (!window.__adkwsAnim || !window.__adkwsAnim.cursor) return;
                window.__adkwsAnim.cursor.style.opacity = '0';
            }"""
        )

    async def _ensure_animation_layer(self, page: Page) -> None:
        await page.evaluate(ANIMATION_BOOTSTRAP_JS)

    async def gradual_cursor_animation(
        self, page: Page, start_x: float, start_y: float, end_x: float, end_y: float
    ):
        await self._ensure_animation_layer(page)
        steps = 10
        for i in range(1, steps + 1):
            curr_x = start_x + (end_x - start_x) * (i / steps)
            curr_y = start_y + (end_y - start_y) * (i / steps)
            await page.evaluate(
                "({x, y}) => window.__adkwsAnim && window.__adkwsAnim.setCursor(x, y)",
                {"x": curr_x, "y": curr_y},
            )
            await page.mouse.move(curr_x, curr_y)
            self.last_cursor_position = (curr_x, curr_y)
            await asyncio.sleep(0.008)

    async def click_ripple(self, page: Page, x: float, y: float) -> None:
        await self._ensure_animation_layer(page)
        await page.evaluate(
            "({x, y}) => window.__adkwsAnim && window.__adkwsAnim.pulseAt(x, y, '#00c2ff')",
            {"x": x, "y": y},
        )

    async def type_pulse(self, page: Page, x: float, y: float) -> None:
        await self._ensure_animation_layer(page)
        await page.evaluate(
            "({x, y}) => window.__adkwsAnim && window.__adkwsAnim.pulseAt(x, y, '#ffb703')",
            {"x": x, "y": y},
        )

    async def cleanup_animations(self, page: Page):
        await page.evaluate(
            """() => {
                const root = document.getElementById('__adkws_anim_root');
                if (root) root.remove();
                delete window.__adkwsAnim;
            }"""
        )
