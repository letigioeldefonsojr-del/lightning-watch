"""Quick diagnostic: does panahon.gov.ph serve its real page to a VISIBLE
(non-headless) Chromium, where it apparently doesn't to a headless one?

Run this directly:  python test_headed.py

A browser window should pop up and navigate to panahon.gov.ph. Leave it
open until this prints its results and asks you to press Enter -- take a
quick look at the window itself too (does it show the real map site, or
something blank/broken/an image?) since that's useful info even beyond
what gets printed.
"""
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False)
    try:
        page = browser.new_page()
        resp = page.goto("https://panahon.gov.ph/", wait_until="domcontentloaded", timeout=40000)
        print()
        print("status:", resp.status if resp else None)
        print("content-type:", resp.headers.get("content-type") if resp else None)
        print("title:", page.title())
        has_meta = page.evaluate('!!document.querySelector(\'meta[name="csrf-token"]\')')
        print("has csrf-token meta tag:", has_meta)
        if has_meta:
            token = page.evaluate('document.querySelector(\'meta[name="csrf-token"]\').getAttribute("content")')
            print("token (first 12 chars):", token[:12] + "...")
        print()
        input("Take a look at the browser window, then press Enter here to close it...")
    finally:
        browser.close()
