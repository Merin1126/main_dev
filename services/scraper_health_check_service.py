"""Lightweight scraper smoke test used by the settings screen.

The check intentionally avoids downloading files or touching scraper database
state. It only verifies that the browser, network, and JACAR list-page
selectors used by the scraper are still healthy.
"""
from __future__ import annotations

import os
import traceback
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


@dataclass
class ScraperHealthCheckResult:
    ok: bool
    message: str
    log_path: str = ""
    url: str = ""
    rows_found: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ScraperHealthCheckService:
    """Run a non-destructive end-to-end smoke test for the JACAR scraper."""

    def __init__(self, *, project_root: str) -> None:
        self.project_root = os.path.abspath(project_root)
        self.log_dir = os.path.join(self.project_root, "Scraper_Logs")

    @staticmethod
    def _build_search_url() -> str:
        query = urlencode(
            {
                "kl0": "AND",
                "ks0": "kw_all",
                "kw0": "反帝國主義",
                "date_y_from": "1921",
                "date_y_to": "1927",
                "rows": "20",
                "sf": "seq_a",
            }
        )
        return f"https://www.jacar.archives.go.jp/aj/search?{query}"

    @classmethod
    def search_url(cls) -> str:
        return cls._build_search_url()

    @staticmethod
    def _new_driver() -> webdriver.Chrome:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"--user-agent={_BROWSER_USER_AGENT}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        return driver

    def _write_error_log(self, *, lines: list[str], exc: BaseException | None) -> str:
        os.makedirs(self.log_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(self.log_dir, f"scraper_healthcheck_{ts}.log")
        payload = list(lines)
        if exc is not None:
            payload.extend(["", "Traceback:", traceback.format_exc()])
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(payload).rstrip() + "\n")
        return log_path

    def write_manual_error_log(self, *, message: str, lines: list[str] | None = None) -> str:
        payload = [
            "HRS Scraper Health Check",
            f"Time: {datetime.now().isoformat(timespec='seconds')}",
            f"Project root: {self.project_root}",
            f"Search URL: {self.search_url()}",
            "",
            message,
        ]
        if lines:
            payload.extend(["", *lines])
        return self._write_error_log(lines=payload, exc=None)

    def run(self) -> ScraperHealthCheckResult:
        url = self._build_search_url()
        lines = [
            "HRS Scraper Health Check",
            f"Time: {datetime.now().isoformat(timespec='seconds')}",
            f"Project root: {self.project_root}",
            f"Search URL: {url}",
            "",
        ]
        driver = None
        try:
            lines.append("Step 1/3: checking JACAR HTTP reachability...")
            response = requests.get(url, headers={"User-Agent": _BROWSER_USER_AGENT}, timeout=15)
            lines.append(f"HTTP status: {response.status_code}")
            response.raise_for_status()

            lines.append("Step 2/3: starting headless Chrome via Selenium...")
            driver = self._new_driver()
            wait = WebDriverWait(driver, 15)

            lines.append("Step 3/3: loading search page and validating selectors...")
            driver.get(url)
            wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
            current_source = driver.page_source or ""
            if "403 Forbidden" in current_source:
                lines.append("Headless Chrome received 403 Forbidden from JACAR.")
                lines.append("")
                lines.append("Page source preview:")
                lines.append(current_source[:3000])
                raise RuntimeError("JACAR returned 403 Forbidden to Selenium/Chrome.")

            no_result_text = "該当する文書が見つかりませんでした"
            try:
                wait.until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "li.archive-result-table__body-item")
                    or (no_result_text in (d.page_source or ""))
                )
            except TimeoutException:
                page_preview = (driver.page_source or "")[:3000]
                lines.append("Timed out waiting for JACAR result rows or no-result text.")
                lines.append("")
                lines.append("Page source preview:")
                lines.append(page_preview)
                raise

            if no_result_text in (driver.page_source or ""):
                raise RuntimeError("JACAR search is reachable, but the smoke-test keyword returned no results.")

            rows = driver.find_elements(By.CSS_SELECTOR, "li.archive-result-table__body-item")
            if not rows:
                raise RuntimeError("JACAR result-row selector returned 0 rows.")

            first_row = rows[0]
            view_links = first_row.find_elements(By.CSS_SELECTOR, "a.result-image-link")
            if not view_links:
                raise RuntimeError("First result row has no view link selector: a.result-image-link")

            title = first_row.find_element(By.CSS_SELECTOR, "h3.result-header__title a").text.strip()
            ref_code = first_row.find_element(
                By.XPATH,
                ".//dt[contains(normalize-space(.), 'レファレンスコード')]/following-sibling::dd[1]",
            ).text.strip()
            if not title or not ref_code:
                raise RuntimeError("First result row is missing title or reference code.")

            return ScraperHealthCheckResult(
                ok=True,
                message=(
                    "Scraper 自检通过：JACAR 可访问，Selenium/Chrome 可启动，"
                    f"结果页选择器正常。命中 {len(rows)} 行，首条 Ref：{ref_code}。"
                ),
                url=url,
                rows_found=len(rows),
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            lines.append(f"Health check failed: {detail}")
            log_path = self._write_error_log(lines=lines, exc=exc)
            return ScraperHealthCheckResult(
                ok=False,
                message=f"Scraper 自检失败：{detail}。请检查网址：{url}",
                log_path=log_path,
                url=url,
            )
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
