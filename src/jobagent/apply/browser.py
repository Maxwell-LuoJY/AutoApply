"""Playwright session with a dedicated, persistent browser profile (kept under data/, separate from your Chrome)."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .. import config


class BrowserSession:
    def __init__(self, profile_dir: Path, headless: bool = True):
        self.profile_dir, self.headless = profile_dir, headless
        self._pw = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> "BrowserSession":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        opts = dict(
            user_data_dir=str(self.profile_dir), headless=self.headless,
            viewport={"width": 1280, "height": 900}, locale="en-US",
            timezone_id=config.settings()["applying"].get("timezone") or "America/Chicago",
        )
        if not self.headless:
            opts["channel"] = "chrome"  # your installed Google Chrome, with this separate profile
        self.context = self._pw.chromium.launch_persistent_context(**opts)
        self.context.set_default_timeout(15000)
        return self

    def new_page(self) -> Page:
        return self.context.new_page()

    def __exit__(self, *exc) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()
