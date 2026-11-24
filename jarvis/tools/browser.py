"""A browser the assistant drives while the user watches (for the ERP portal, job sites, etc).

It opens a real, visible Microsoft Edge window with its own saved profile in
data/browser_profile, so the user signs in to a site once, by hand, and stays
signed in. The assistant never sees or types passwords.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse

from ..security import check_url
from .registry import Registry

PAGE_CHARS = 8_000
# The sites the user signs in to by hand once. Addresses can be changed or added with
# browser_open(save_as=...); the saved ones win over these defaults.
DEFAULT_PORTALS = {
    "erp": "https://newerp.kluniversity.in/",
    "lms": "https://lms.kluniversity.in/",
    "outlook": "https://outlook.office.com/mail/",
}
SIGN_IN_HINTS = ("login", "signin", "sign-in", "sso", "adfs", "oauth", "microsoftonline", "/auth", "accounts.")


class Browser:
    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self._playwright = self._context = self._page = None
        self._tabs: dict[str, object] = {}
        self._cookie_file = profile_dir.parent / "browser_cookies.json"
        self._saved_at = 0.0

    # Sign-ins that the site marks "until the browser closes" would be lost every time Jay restarts.
    # They are written to a file next to the browser profile and given a two-week life, so the user
    # signs in to ERP or Outlook once, not after every restart. (The file holds login cookies: it
    # stays in Jay's data folder and is never read by any tool.)
    def save_cookies(self, force: bool = False) -> None:
        if self._context is None or (not force and time.time() - self._saved_at < 60):
            return
        try:
            cookies = self._context.cookies()
        except Exception:
            return
        horizon = time.time() + 14 * 86400
        for c in cookies:
            if c.get("expires", -1) in (-1, None) or c["expires"] < 0:
                c["expires"] = horizon
        try:
            self._cookie_file.write_text(json.dumps(cookies), encoding="utf-8")
            self._saved_at = time.time()
        except OSError:
            pass

    def _restore_cookies(self) -> None:
        try:
            cookies = json.loads(self._cookie_file.read_text(encoding="utf-8"))
            now = time.time()
            self._context.add_cookies([c for c in cookies if c.get("expires", now + 1) > now])
        except Exception:
            pass

    def tab(self, name: str):
        """A page of its own for one site (kept between calls), so sites don't replace each other."""
        page = self._tabs.get(name)
        if page is None or page.is_closed():
            self.page()                                   # makes sure the window is running
            page = self._context.new_page()
            page.set_default_timeout(20_000)
            self._tabs[name] = page
        self._page = page
        return page

    def needs_sign_in(self, page=None) -> bool:
        page = page or self.page()
        try:
            if page.query_selector("input[type=password], input[type=email], input[name*=user i], input[id*=user i]"):
                return True
            text = (page.inner_text("body") or "")[:3000].lower()
            if any(w in text for w in ("forgot password", "forgot your password", "sign in to your account")):
                return True
        except Exception:
            pass
        url = page.url.lower()
        return any(hint in url for hint in SIGN_IN_HINTS)

    def page(self):
        if self._page is not None and not self._page.is_closed():
            return self._page
        from playwright.sync_api import sync_playwright

        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir), channel="msedge", headless=False, no_viewport=True)
        self._tabs = {}
        self._restore_cookies()
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(20_000)
        return self._page

    def snapshot(self, limit: int = PAGE_CHARS) -> str:
        self.save_cookies()
        page = self.page()
        page.wait_for_load_state("domcontentloaded")
        text = " ".join(page.inner_text("body").split()) if page.query_selector("body") else ""
        more = f"\n[page continues: {len(text) - limit} more characters; use browser_read for all of it]" \
            if len(text) > limit else ""
        login = "\n[This page has a password field. Ask the user to sign in themselves in the " \
                "browser window, then continue when they say they are done.]" \
            if page.query_selector("input[type=password]") else ""
        return f"Title: {page.title()}\nURL: {page.url}\n\n{text[:limit]}{more}{login}"

    def close(self) -> None:
        self.save_cookies(force=True)
        for thing in (self._context, self._playwright):
            try:
                if thing is not None:
                    thing.close() if thing is self._context else thing.stop()
            except Exception:
                pass
        self._playwright = self._context = self._page = None
        self._tabs = {}


def register(reg: Registry, data_dir: Path, store) -> Browser:
    browser = Browser(data_dir / "browser_profile")

    def portal_url(name: str) -> str | None:
        return store.get_meta(f"portal:{name}") or DEFAULT_PORTALS.get(name)

    # Sites Jay may open without asking: the user's portals and any site they approved before.
    # A page or an email could try to send Jay to an attacker's address with private details in the
    # link, so a site Jay hasn't opened before (or any address on this laptop or network) needs a yes.
    def _host(url: str) -> str:
        return (urlparse(str(url)).hostname or "").lower().rstrip(".")

    def known_hosts() -> set[str]:
        try:
            saved = set(json.loads(store.get_meta("browser_hosts") or "[]"))
        except ValueError:
            saved = set()
        portals = {_host(portal_url(n) or "") for n in ("erp", "lms", "outlook")}
        return saved | {h for h in portals if h}

    def remember_host(url: str) -> None:
        host = _host(url)
        if host and host not in known_hosts():
            try:
                saved = set(json.loads(store.get_meta("browser_hosts") or "[]"))
            except ValueError:
                saved = set()
            store.set_meta("browser_hosts", json.dumps(sorted(saved | {host})))

    def site_confirm(args: dict):
        url = str(args.get("url", ""))
        host = _host(url)
        if not host:
            return None                                 # the tool itself reports the bad address
        if host in known_hosts():
            return None
        local = check_url(url)
        if local and local.startswith("Blocked"):
            return f"WARNING, this address is on this laptop or your network: open {url}"
        return f"Open {url} (a site Jay hasn't opened before)"

    @reg.tool(
        "browser_open",
        "Open a web address in the assistant's browser window (visible to the user) and return "
        "the page's text. Use this for sites the user is signed in to, such as their college ERP "
        "portal or job sites; use web_search for general lookups. A short site name given as "
        "save_as is remembered, so 'open_portals' can open it later.",
        {"url": {"type": "string", "description": "Full http(s) address."},
         "save_as": {"type": "string", "description": "Optional short name to remember this site under."}},
        required=("url",),
        confirm=site_confirm,
    )
    def browser_open(url: str, save_as: str = "") -> str:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Only http and https addresses are allowed.")
        if save_as.strip():
            store.set_meta(f"portal:{save_as.strip().lower()}", url)
        browser.page().goto(url, wait_until="domcontentloaded")
        remember_host(url)                              # the user said yes (or it was already theirs)
        return browser.snapshot()

    @reg.tool(
        "open_portals",
        "Open the user's usual sites (college ERP, LMS, Outlook mail, or any name saved with "
        "browser_open save_as) each in its own tab of the assistant's browser, and report which "
        "ones are signed in and which need the user to sign in. Sign-ins are kept between runs.",
        {"names": {"type": "string", "description": "Comma separated site names; default 'erp,lms,outlook'."}},
    )
    def open_portals(names: str = "erp,lms,outlook") -> str:
        lines, first = [], None
        for name in [n.strip().lower() for n in names.split(",") if n.strip()]:
            url = portal_url(name)
            if not url:
                lines.append(f"{name}: no address saved. Ask the user for it and open it with save_as.")
                continue
            try:
                page = browser.tab(name)
                if name not in opened or page.url in ("about:blank", ""):
                    page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=6_000)   # let a sign-in redirect settle
                    except Exception:
                        pass
                opened.add(name)
                state = "NEEDS SIGN-IN" if browser.needs_sign_in(page) else "SIGNED IN"
                lines.append(f"{name.upper()}: {state} - {page.title()[:60]} ({page.url[:80]})")
                first = first or page
            except Exception as exc:
                if "Timeout" in type(exc).__name__ or "ERR_" in str(exc):
                    lines.append(f"{name.upper()}: UNREACHABLE - the site did not load (it may only be reachable "
                                 "on the campus network, or be down). Not a sign-in problem.")
                else:
                    lines.append(f"{name.upper()}: COULD NOT OPEN - {type(exc).__name__}: {str(exc)[:100]}")
        for stray in list(browser.page().context.pages):          # the empty tab the window starts with
            if stray.url in ("about:blank", "") and stray not in browser._tabs.values() and                     len(browser.page().context.pages) > 1:
                try:
                    stray.close()
                except Exception:
                    pass
        if first is not None:
            first.bring_to_front()
            browser._page = first
        return "\n".join(lines) or "Nothing to open."

    opened: set[str] = set()

    @reg.tool("browser_tabs", "List the tabs open in the assistant's browser (number, title, address).")
    def browser_tabs() -> str:
        pages = browser.page().context.pages
        current = browser.page()
        return "\n".join(f"{i + 1}{'*' if p is current else ''}. {p.title()[:60]} {p.url}"
                         for i, p in enumerate(pages)) or "No tabs."

    @reg.tool(
        "browser_switch",
        "Switch the assistant's browser to another open tab, by number or by part of its title or "
        "address, and return that page's text.",
        {"which": {"type": "string", "description": "Tab number, or text from its title or address."}},
        required=("which",),
    )
    def browser_switch(which: str) -> str:
        pages = browser.page().context.pages
        pick = None
        if which.strip().isdigit() and 1 <= int(which) <= len(pages):
            pick = pages[int(which) - 1]
        else:
            pick = next((p for p in pages if which.lower() in (p.title() + " " + p.url).lower()), None)
        if pick is None:
            return f"No open tab matches '{which}'."
        pick.bring_to_front()
        browser._page = pick
        return browser.snapshot()

    @reg.tool(
        "browser_read",
        "Return the full text of the page currently open in the assistant's browser. Use it "
        "after the user says they have signed in, or to read a long page in full.")
    def browser_read() -> str:
        return browser.snapshot(limit=1_000_000)

    @reg.tool(
        "browser_links",
        "List the links on the current browser page (text and address), to decide where to go next.",
        {"contains": {"type": "string", "description": "Only links whose text or address contains this."}},
    )
    def browser_links(contains: str = "") -> str:
        links = browser.page().eval_on_selector_all(
            "a[href]", "els => els.map(e => [(e.innerText || e.title || '').trim().replace(/\\s+/g, ' '), e.href])")
        needle = contains.lower()
        seen, lines = set(), []
        for label, href in links:
            if not href.startswith("http") or href in seen:
                continue
            if needle and needle not in label.lower() and needle not in href.lower():
                continue
            seen.add(href)
            lines.append(f"{label or '(no text)'} -> {href}")
            if len(lines) >= 100:
                lines.append("... stopped at 100 links; narrow it with 'contains'")
                break
        return "\n".join(lines) or "No matching links."

    @reg.tool(
        "browser_click",
        "Click a button, tab or link on the current browser page by its visible text, then "
        "return the resulting page. The user is asked to approve every click, because a click "
        "can submit a form.",
        {"text": {"type": "string", "description": "Visible text of the thing to click."}},
        required=("text",),
        confirm=lambda a: f"Click '{a['text']}' in the browser",
    )
    def browser_click(text: str) -> str:
        page = browser.page()
        for locator in (page.get_by_role("button", name=text), page.get_by_role("link", name=text),
                        page.get_by_role("tab", name=text), page.get_by_text(text)):
            if locator.count():
                locator.first.click()
                return browser.snapshot()
        return f"Nothing on the page matches '{text}'."

    @reg.tool(
        "browser_type",
        "Type text into a field on the current browser page, found by its label or placeholder. "
        "The user is asked to approve it. Never use this for passwords, card numbers or one-time "
        "codes: ask the user to enter those themselves.",
        {
            "field": {"type": "string", "description": "The field's label or placeholder text."},
            "text": {"type": "string"},
        },
        required=("field", "text"),
        confirm=lambda a: f"Type into '{a['field']}' in the browser: {a['text']}",
    )
    def browser_type(field: str, text: str) -> str:
        page = browser.page()
        for locator in (page.get_by_label(field), page.get_by_placeholder(field)):
            if locator.count():
                target = locator.first
                if (target.get_attribute("type") or "").lower() == "password":
                    return "Refused: that is a password field. Ask the user to type it themselves."
                target.fill(text)
                return f"Typed into '{field}'."
        return f"No field labelled '{field}' on the page."

    @reg.tool(
        "browser_press",
        "Press a key in the assistant's browser, e.g. 'Enter' to send a typed message or submit a "
        "search box, 'Escape', 'ArrowDown'. The user approves it, because Enter can send or submit.",
        {"key": {"type": "string", "description": "Key name such as Enter, Escape, Tab, ArrowDown."}},
        required=("key",),
        confirm=lambda a: f"Press {a['key']} in the browser (this may send or submit what is typed)",
    )
    def browser_press(key: str) -> str:
        browser.page().keyboard.press(key)
        return browser.snapshot()

    @reg.tool(
        "check_site",
        "Open a page the user wants watched (a dashboard, portal, results or notices page) and "
        "report what changed since you last checked it. Returns the new lines, or the whole page "
        "the first time. Use the same name each time for the same page.",
        {
            "name": {"type": "string", "description": "Short name for this page, e.g. 'erp notices'."},
            "url": {"type": "string", "description": "Full http(s) address."},
        },
        required=("name", "url"),
        confirm=site_confirm,
    )
    def check_site(name: str, url: str) -> str:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Only http and https addresses are allowed.")
        page = browser.page()
        page.goto(url, wait_until="domcontentloaded")
        remember_host(url)
        if page.query_selector("input[type=password]"):
            return browser.snapshot()          # includes the note asking the user to sign in
        return site_changes(store, name, page.title(), page.inner_text("body"))

    browser.remember_host = remember_host
    browser.site_confirm = site_confirm
    return browser


def site_changes(store, name: str, title: str, body_text: str) -> str:
    """Compare a page's text with the copy saved at the last check, and save the new copy."""
    lines = [" ".join(line.split()) for line in body_text.splitlines()]
    lines = [line for line in lines if line]
    key = f"site:{name.strip().lower()}"
    before = store.get_meta(key)
    store.set_meta(key, "\n".join(lines))
    if before is None:
        return f"First look at '{name}' ({title}):\n" + "\n".join(lines)[:PAGE_CHARS]
    old = set(before.split("\n"))
    new = [line for line in lines if line not in old]
    if not new:
        return f"No changes on '{name}' since the last check."
    return f"New on '{name}' since the last check:\n" + "\n".join(new)[:PAGE_CHARS]
