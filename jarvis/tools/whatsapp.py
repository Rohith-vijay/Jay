"""WhatsApp, through WhatsApp Web in the assistant's own browser window.

The user links the browser once by scanning the QR code. Listing chats reads only
the chat list, so it sends no read receipts; opening a chat does. Sending always
goes through an approval card showing the recipient and the exact text.
"""
from __future__ import annotations

import time

from .browser import Browser
from .registry import Registry

URL = "https://web.whatsapp.com"
CHAT_ROW = '#pane-side [role="row"]'
UNREAD = 'span[aria-label*="unread"]'
COMPOSER = 'footer div[contenteditable="true"]'
NOT_LINKED = ("WhatsApp is not linked in the assistant's browser yet. Ask the user to scan the QR code "
              "now showing in that window (phone: WhatsApp > Linked devices > Link a device), then try again.")


class WhatsApp:
    def __init__(self, browser: Browser):
        self.browser = browser

    def page(self):
        page = self.browser.tab("whatsapp")          # its own tab, so other sites keep their pages
        if "web.whatsapp.com" not in page.url:
            page.goto(URL, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("#pane-side", timeout=25_000)
        except Exception:
            raise RuntimeError(NOT_LINKED) from None
        page.wait_for_timeout(1500)      # let the chat list settle
        return page

    def chats(self, page) -> list[dict]:
        """Every chat row currently in the list: name, unread count, and the row's preview text."""
        return page.eval_on_selector_all(CHAT_ROW, """rows => rows.map(row => {
            const title = row.querySelector('span[title]');
            const badge = row.querySelector('span[aria-label*="unread"]');
            const count = badge ? parseInt(badge.getAttribute('aria-label')) || 1 : 0;
            return {name: title ? title.getAttribute('title') : '', unread: count,
                    preview: row.innerText.split('\\n').map(s => s.trim()).filter(Boolean).join(' | ')};
        })""")

    def open_chat(self, page, name: str) -> str:
        """Open the chat called `name`. Returns the exact chat name opened; raises if unsure."""
        wanted = name.strip().lower()

        def candidates() -> list[str]:
            names = [c["name"] for c in self.chats(page) if c["name"]]
            exact = [n for n in names if n.lower() == wanted]
            return exact or [n for n in names if wanted in n.lower()]

        found = candidates()
        if not found:                                   # not in the visible list: use the search box
            search = page.get_by_role("textbox").first
            search.click()
            page.keyboard.insert_text(name)
            page.wait_for_timeout(2500)
            found = candidates()
            if not found:
                page.keyboard.press("Escape")
                raise ValueError(f"No WhatsApp chat matches '{name}'. Ask the user for the exact chat name.")
        if len(set(found)) > 1:
            raise ValueError(f"'{name}' matches several chats: {', '.join(sorted(set(found)))}. "
                             "Ask the user which one.")
        target = found[0]
        for row in page.query_selector_all(CHAT_ROW):
            title = row.query_selector("span[title]")
            if title and title.get_attribute("title") == target:
                row.click()
                break
        page.wait_for_selector(COMPOSER, timeout=10_000)
        header = page.query_selector("#main header")
        if header is None or target not in header.inner_text():
            raise RuntimeError(f"Could not confirm that the open chat is '{target}'; nothing was typed.")
        return target

    def messages(self, page, limit: int = 20) -> list[str]:
        rows = page.eval_on_selector_all(
            '#main [role="row"]',
            "rows => rows.map(r => r.innerText.split('\\n').map(s => s.trim()).filter(Boolean).join(' | '))")
        return [r for r in rows if r][-limit:]

    def type_message(self, page, text: str) -> None:
        box = page.query_selector(COMPOSER)
        box.click()
        lines = text.replace("\r\n", "\n").split("\n")
        for i, line in enumerate(lines):
            if i:
                page.keyboard.press("Shift+Enter")     # a plain Enter would send early
            page.keyboard.insert_text(line)

    def composer_text(self, page) -> str:
        box = page.query_selector(COMPOSER)
        return box.inner_text().strip() if box else ""


def register(reg: Registry, browser: Browser) -> WhatsApp:
    wa = WhatsApp(browser)

    @reg.tool(
        "whatsapp_unread",
        "List the user's WhatsApp chats that have unread messages: who, how many, and the latest "
        "message preview. Reads only the chat list, so nobody sees a 'read' tick. Start here when "
        "the user asks about their messages.",
        {"include_read": {"type": "boolean", "description": "Also list recent chats with nothing unread."}},
    )
    def whatsapp_unread(include_read: bool = False) -> str:
        chats = [c for c in wa.chats(wa.page()) if c["name"] and (include_read or c["unread"])]
        if not chats:
            return "No unread WhatsApp chats."
        return "\n".join(f"{c['name']} ({c['unread']} unread): {c['preview']}" for c in chats)

    @reg.tool(
        "whatsapp_read",
        "Open one WhatsApp chat and return its latest messages. This marks the chat as read on "
        "WhatsApp (the sender sees read ticks), so open a chat only when the preview isn't enough "
        "or the user wants a reply written.",
        {"chat": {"type": "string", "description": "Chat name exactly as listed by whatsapp_unread."}},
        required=("chat",),
    )
    def whatsapp_read(chat: str) -> str:
        page = wa.page()
        opened = wa.open_chat(page, chat)
        page.wait_for_timeout(1200)
        return f"Latest messages in '{opened}' (oldest first):\n" + "\n".join(wa.messages(page))

    @reg.tool(
        "whatsapp_send",
        "Send a WhatsApp message to one chat. The user is shown the recipient and the exact text "
        "and must approve before it is sent. Write it the way the user writes to that person.",
        {
            "chat": {"type": "string", "description": "Chat name exactly as listed by whatsapp_unread."},
            "message": {"type": "string"},
        },
        required=("chat", "message"),
        confirm=lambda a: f"Send on WhatsApp to {a['chat']}:\n{a['message']}",
    )
    def whatsapp_send(chat: str, message: str) -> str:
        if not message.strip():
            raise ValueError("The message is empty.")
        page = wa.page()
        opened = wa.open_chat(page, chat)
        wa.type_message(page, message)
        page.keyboard.press("Enter")
        time.sleep(1.5)
        if wa.composer_text(page):
            return f"The message was typed in '{opened}' but may not have sent; ask the user to check."
        return f"Sent to '{opened}'."

    return wa
