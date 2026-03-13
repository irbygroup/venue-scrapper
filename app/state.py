from typing import Optional

from app.browser import BrowserManager

bm: Optional[BrowserManager] = None


def get_bm() -> BrowserManager:
    assert bm is not None, "BrowserManager not initialized"
    return bm
