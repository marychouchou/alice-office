#!/usr/bin/env python3
"""Alice browser automation tool for OpenClaw.

The tool is deliberately conservative: it can browse, fill ride details, take
screenshots, and report the next required human confirmation. It must not press
final purchase/order/ride confirmation buttons unless an explicit higher-level
policy grants that step.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_DIR = Path(os.environ.get("ALICE_BROWSER_HOME", "/home/alice_gx10/.openclaw/browser")).expanduser()
PROFILE_DIR = Path(
    os.environ.get("ALICE_BROWSER_PROFILE", "/home/alice_gx10/snap/firefox/common/alice-openclaw-profile")
).expanduser()
SCREENSHOT_DIR = Path(os.environ.get("ALICE_BROWSER_SCREENSHOTS", str(BASE_DIR / "screenshots"))).expanduser()
SS_MAX_DAYS = int(os.environ.get("ALICE_BROWSER_SS_MAX_DAYS", "7"))
FIREFOX_BIN = os.environ.get("ALICE_FIREFOX_BIN", "")
GECKODRIVER_BIN = os.environ.get("ALICE_GECKODRIVER_BIN", "/snap/bin/geckodriver")
LOCK_PATH = Path(os.environ.get("ALICE_BROWSER_LOCK", str(BASE_DIR / "browser.lock"))).expanduser()
ECOMMERCE_INDEX_PATH = Path(
    os.environ.get("ALICE_ECOMMERCE_INDEX", "/home/alice_gx10/.openclaw/tools/browser/tw-ecommerce-index.json")
).expanduser()


@dataclass
class BrowserResult:
    ok: bool
    status: str
    url: str = ""
    title: str = ""
    screenshot: str = ""
    message: str = ""
    data: dict[str, Any] | None = None
    error: str = ""


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _prune_screenshots(max_age_days: int = SS_MAX_DAYS) -> int:
    if not SCREENSHOT_DIR.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in SCREENSHOT_DIR.glob("*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def sanitize_name(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return value[:64] or "page"


def screenshot_path(label: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _prune_screenshots()
    return SCREENSHOT_DIR / f"{int(time.time())}-{sanitize_name(label)}.png"


def make_driver(headless: bool = True) -> webdriver.Firefox:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    options = Options()
    if headless:
        options.add_argument("-headless")
    if FIREFOX_BIN:
        options.binary_location = FIREFOX_BIN
    options.add_argument("-profile")
    options.add_argument(str(PROFILE_DIR))
    options.set_preference("intl.accept_languages", "zh-TW,zh,en-US,en")
    options.set_preference("dom.webnotifications.enabled", False)
    options.set_preference("media.navigator.permission.disabled", True)
    service = Service(executable_path=GECKODRIVER_BIN)
    return webdriver.Firefox(service=service, options=options)


def finish(driver: webdriver.Firefox, status: str, message: str = "", label: str = "browser", data: dict[str, Any] | None = None) -> BrowserResult:
    path = screenshot_path(label)
    try:
        driver.save_screenshot(str(path))
    except Exception:
        path = Path("")
    return BrowserResult(
        ok=status not in {"error"},
        status=status,
        url=driver.current_url or "",
        title=driver.title or "",
        screenshot=str(path) if path else "",
        message=message,
        data=data or {},
    )


def visible_text(driver: webdriver.Firefox, max_chars: int = 2500) -> str:
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()[:max_chars]


def command_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    removed = _prune_screenshots(max_age_days=args.days)
    remaining = len(list(SCREENSHOT_DIR.glob("*.png"))) if SCREENSHOT_DIR.exists() else 0
    return {"ok": True, "removed": removed, "remaining": remaining,
            "screenshot_dir": str(SCREENSHOT_DIR), "max_age_days": args.days}


def command_health(args: argparse.Namespace) -> BrowserResult:
    driver = make_driver(headless=not args.headed)
    try:
        driver.set_page_load_timeout(args.timeout)
        driver.get("data:text/html;charset=utf-8,<title>Alice Browser OK</title><h1>Alice Browser OK</h1>")
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "h1")))
        return finish(driver, "ready", "Firefox + Selenium browser automation is ready.", "health")
    finally:
        driver.quit()


def command_open(args: argparse.Namespace) -> BrowserResult:
    driver = make_driver(headless=not args.headed)
    try:
        driver.set_page_load_timeout(args.timeout)
        driver.get(args.url)
        time.sleep(args.wait)
        return finish(driver, "opened", "Page opened.", "open", {"text": visible_text(driver)})
    finally:
        driver.quit()


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>()\"']+", text or "", flags=re.I)


FALLBACK_PLATFORM_SITES = {
    "shopee": ["shopee", "蝦皮"],
    "ruten": ["ruten", "露天"],
    "momo": ["momo"],
    "coupang": ["coupang", "酷澎"],
    "pchome": ["pchome", "pchome24h", "24h"],
    "yahoo": ["yahoo", "雅虎", "奇摩", "拍賣"],
    "official": ["官方", "官網", "official"],
}

FALLBACK_PLATFORM_SITE_QUERY = {
    "shopee": "site:shopee.tw",
    "ruten": "site:ruten.com.tw",
    "momo": "site:momoshop.com.tw",
    "coupang": "site:coupang.com",
    "pchome": "site:24h.pchome.com.tw OR site:shopping.pchome.com.tw",
    "yahoo": "site:tw.bid.yahoo.com OR site:tw.buy.yahoo.com",
    "official": "官方網站",
}


def load_ecommerce_index() -> dict[str, Any]:
    candidates = [
        ECOMMERCE_INDEX_PATH,
        Path(__file__).resolve().parents[1] / "shopping" / "tw-ecommerce-index.json",
        Path("/home/alice_gx10/agents/alice/scripts/shopping/tw-ecommerce-index.json"),
    ]
    for path in candidates:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def platform_maps() -> tuple[dict[str, list[str]], dict[str, str]]:
    index = load_ecommerce_index()
    aliases: dict[str, list[str]] = {}
    site_queries: dict[str, str] = {}
    for item in index.get("platforms", []) if isinstance(index, dict) else []:
        platform_id = item.get("id")
        if not platform_id:
            continue
        aliases[platform_id] = [str(platform_id), str(item.get("name", "")), *(item.get("aliases") or [])]
        if item.get("siteQuery"):
            site_queries[platform_id] = str(item["siteQuery"])
    if not aliases:
        aliases = FALLBACK_PLATFORM_SITES
    if not site_queries:
        site_queries = FALLBACK_PLATFORM_SITE_QUERY
    return aliases, site_queries


def category_platforms(instruction: str) -> list[str]:
    index = load_ecommerce_index()
    text = (instruction or "").lower()
    scored: list[tuple[int, list[str]]] = []
    for category in index.get("categories", []) if isinstance(index, dict) else []:
        score = 0
        for keyword in category.get("keywords", []):
            if str(keyword).lower() in text:
                score += 3
        if score:
            score += int(category.get("defaultPriority", 0)) // 20
            scored.append((score, list(category.get("platforms", []))))
    scored.sort(reverse=True, key=lambda item: item[0])
    merged: list[str] = []
    for _, platforms in scored[:2]:
        for platform in platforms:
            if platform not in merged:
                merged.append(platform)
    return merged[:12]


def detect_platforms(text: str) -> list[str]:
    normalized = (text or "").lower()
    platform_sites, _ = platform_maps()
    found = []
    for platform, aliases in platform_sites.items():
        if any(alias.lower() in normalized for alias in aliases):
            found.append(platform)
    return found


def build_search_url(instruction: str) -> str:
    text = re.sub(r"(查|搜尋|幫我|售價|價格|多少錢|連結|商品|網站|平台)", " ", instruction or "", flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    _, site_queries = platform_maps()
    platforms = []
    for platform in [*detect_platforms(instruction), *category_platforms(instruction)]:
        if platform not in platforms:
            platforms.append(platform)
    site_filter = " ".join(site_queries[p] for p in platforms if p in site_queries)
    if not text:
        text = instruction or "商品 售價"
    query = f"{text} {site_filter}".strip()
    return f"https://www.google.com/search?q={quote_plus(query)}"


def detect_shopping_state(driver: webdriver.Firefox) -> str:
    text = visible_text(driver, 4000).lower()
    url = (driver.current_url or "").lower()
    if "verify/traffic" in url or "頁面無法顯示" in text or "尚未登入" in text or "login" in url:
        return "requires_login_or_verification"
    if re.search(r"nt\$|\$|售價|價格|加入購物車|直接購買|buy now|add to cart", text, re.I):
        return "product_visible"
    return "opened"


def extract_prices(text: str) -> list[str]:
    prices = []
    for match in re.finditer(r"(?:NT\$|\$|＄)\s*[0-9][0-9,]*(?:\s*-\s*(?:NT\$|\$|＄)?\s*[0-9][0-9,]*)?", text):
        prices.append(match.group(0).strip())
    return list(dict.fromkeys(prices))[:8]


def command_shopping(args: argparse.Namespace) -> BrowserResult:
    urls = extract_urls(args.url or args.instruction or "")
    url = urls[0] if urls else build_search_url(args.instruction)
    planned_platforms = []
    for platform in [*detect_platforms(args.instruction or url), *category_platforms(args.instruction or url)]:
        if platform not in planned_platforms:
            planned_platforms.append(platform)
    if args.dry_run:
        return BrowserResult(
            ok=True,
            status="planned",
            url=url,
            message="Shopping browser plan built.",
            data={"inputUrl": url, "platforms": planned_platforms},
        )
    driver = make_driver(headless=not args.headed)
    try:
        driver.set_page_load_timeout(args.timeout)
        driver.get(url)
        time.sleep(args.wait)
        page_text = visible_text(driver, 3500)
        state = detect_shopping_state(driver)
        prices = extract_prices(page_text)
        message = "商品頁已開啟。"
        if state == "requires_login_or_verification":
            message = "商品頁已用瀏覽器開啟，但平台要求登入或流量驗證；不能用猜的判斷售價或是否有貨。"
        elif prices:
            message = "商品頁已開啟並抓到可能價格。"
        return finish(
            driver,
            state,
            message,
            "shopping",
            {
                "inputUrl": url,
                "finalUrl": driver.current_url or "",
                "prices": prices,
                "platforms": planned_platforms,
                "page_text": page_text[:1400],
            },
        )
    finally:
        driver.quit()


def parse_uber_instruction(text: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    patterns = [
        r"從\s*(?P<pickup>.+?)\s*(?:到|去)\s*(?P<dropoff>.+?)(?:$|，|,|。)",
        r"(?P<pickup>.+?)\s*(?:到|去)\s*(?P<dropoff>.+?)(?:叫|訂)?\s*(?:uber|Uber|UBER|優步)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            pickup = match.group("pickup").strip(" ，,。")
            dropoff = match.group("dropoff").strip(" ，,。")
            pickup = re.sub(r"^(?:幫我|我要|請|叫|訂|uber|Uber|優步)\s*", "", pickup).strip()
            dropoff = re.sub(r"\s*(?:單程|現在|馬上|左右)$", "", dropoff).strip()
            if pickup and dropoff:
                return pickup, dropoff
    return "", ""


def detect_uber_state(driver: webdriver.Firefox) -> str:
    text = visible_text(driver, 4000).lower()
    url = (driver.current_url or "").lower()
    if any(token in text for token in ["log in", "sign in", "登入", "phone number", "email"]) or "login" in url:
        return "requires_login"
    if any(token in text for token in ["where to", "destination", "目的地", "pickup", "上車"]):
        return "ready_for_input"
    if any(token in text for token in ["choose a ride", "confirm", "選擇車", "確認"]):
        return "ready_for_confirmation"
    return "opened"


def try_fill_active(driver: webdriver.Firefox, value: str) -> bool:
    try:
        element = driver.switch_to.active_element
        element.clear()
        element.send_keys(value)
        time.sleep(0.7)
        return True
    except Exception:
        return False


def click_text(driver: webdriver.Firefox, patterns: list[str]) -> bool:
    escaped = " || ".join(
        [f"txt.includes({json.dumps(pattern.lower())})" for pattern in patterns]
    )
    script = f"""
const nodes = Array.from(document.querySelectorAll('button, a, input, [role="button"], [aria-label], div, span'));
for (const el of nodes) {{
  const txt = ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').toLowerCase();
  if ({escaped}) {{
    el.scrollIntoView({{block:'center', inline:'center'}});
    el.click();
    return true;
  }}
}}
return false;
"""
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False


def command_uber(args: argparse.Namespace) -> BrowserResult:
    pickup = args.pickup or ""
    dropoff = args.dropoff or ""
    if args.instruction and (not pickup or not dropoff):
        parsed_pickup, parsed_dropoff = parse_uber_instruction(args.instruction)
        pickup = pickup or parsed_pickup
        dropoff = dropoff or parsed_dropoff
    if not pickup or not dropoff:
        return BrowserResult(
            ok=False,
            status="needs_details",
            message="需要出發地和目的地，例如：叫 Uber 從公司到台中高鐵站。",
            data={"pickup": pickup, "dropoff": dropoff},
        )

    driver = make_driver(headless=not args.headed)
    try:
        driver.set_page_load_timeout(args.timeout)
        driver.get("https://m.uber.com/go/home")
        time.sleep(args.wait)
        state = detect_uber_state(driver)
        filled = False
        if state != "requires_login":
            if click_text(driver, ["pickup", "上車", "出發", "enter pickup", "pickup location"]):
                filled = try_fill_active(driver, pickup) or filled
                time.sleep(0.8)
            if click_text(driver, ["where to", "destination", "目的地", "去哪", "dropoff"]):
                filled = try_fill_active(driver, dropoff) or filled
                time.sleep(1.2)
            state = detect_uber_state(driver)
        message_by_state = {
            "requires_login": "Uber 已開啟，但需要先登入或完成驗證。登入完成後 Alice 才能繼續填地址與抓價格。",
            "ready_for_input": "Uber 已開啟，正在等待地址輸入或頁面欄位尚未完全可操作。",
            "ready_for_confirmation": "Uber 已進到可確認階段。依安全規則，最後叫車前必須回 LINE 請你確認。",
            "opened": "Uber 已開啟。若頁面未顯示地址欄，可能需要登入或 Uber 改版。",
        }
        return finish(
            driver,
            state,
            message_by_state.get(state, "Uber browser task finished."),
            "uber",
            {"pickup": pickup, "dropoff": dropoff, "filled": filled, "page_text": visible_text(driver, 1200)},
        )
    finally:
        driver.quit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alice browser automation")
    parser.add_argument("--headed", action="store_true", help="Run with visible browser when a display is available")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--wait", type=float, default=4)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")

    p = sub.add_parser("cleanup")
    p.add_argument("--days", type=int, default=SS_MAX_DAYS,
                   help=f"Delete screenshots older than this many days (default: {SS_MAX_DAYS})")

    p = sub.add_parser("open")
    p.add_argument("--url", required=True)

    p = sub.add_parser("shopping")
    p.add_argument("--instruction", default="")
    p.add_argument("--url", default="")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("uber")
    p.add_argument("--instruction", default="")
    p.add_argument("--pickup", default="")
    p.add_argument("--dropoff", default="")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            if args.cmd == "cleanup":
                print_json(command_cleanup(args))
                return 0
            elif args.cmd == "health":
                result = command_health(args)
            elif args.cmd == "open":
                result = command_open(args)
            elif args.cmd == "shopping":
                result = command_shopping(args)
            elif args.cmd == "uber":
                result = command_uber(args)
            else:
                raise ValueError(f"unknown command: {args.cmd}")
        print_json(asdict(result))
        return 0 if result.ok else 2
    except WebDriverException as exc:
        print_json(asdict(BrowserResult(ok=False, status="error", error=str(exc)[:1200])))
        return 1
    except Exception as exc:
        print_json(asdict(BrowserResult(ok=False, status="error", error=f"{type(exc).__name__}: {exc}")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
