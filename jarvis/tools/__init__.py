from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..store import Store
from ..phone import Phone
from . import browser, google, knowledge, mailbox, pc, personal, system, web, whatsapp, youtube, instagram
from .registry import Registry


def _desktop_notify(title: str, text: str) -> None:
    from .system import notify

    notify(title, text[:200])


def build_registry(cfg: Config, store: Store) -> Registry:
    reg = Registry()
    pc.register(reg, cfg.data_dir)
    personal.register(reg, store, cfg.data_dir)
    knowledge.register(reg, store)
    web.register(reg)
    web.register_knowledge(reg)
    system.register(reg)
    google.register(reg, cfg.data_dir)
    mailbox.register(reg)
    youtube.register(reg, cfg.data_dir)
    instagram.register(reg, cfg.data_dir)
    reg.phone = Phone(cfg.data_dir)
    reg.phone.register(reg)
    reg.browser = browser.register(reg, cfg.data_dir, store)
    reg.whatsapp = whatsapp.register(reg, reg.browser)
    from .. import mcp, skills                      # imported here: they import this package
    reg.skills = skills.SkillBook(cfg.data_dir)
    skills.register(reg, reg.skills)
    reg.mcp_servers = mcp.load_servers(reg, cfg.data_dir)
    from .. import builder, content, grants
    reg.content, reg.routines = content.ContentQueue(store), content.Routines(store)
    content.register(reg, reg.content, reg.routines)
    from .. import publish, video
    reg.publisher = publish.Publisher(reg.content, cfg.data_dir)
    publish.register(reg, reg.publisher)
    reg.videos = video.Videos(cfg.data_dir, reg.content, reg.phone, _desktop_notify)
    video.register(reg, reg.videos, reg.content)
    reg.grants = grants.Grants(cfg.data_dir)
    grants.register(reg, reg.grants)
    reg.builds = builder.Builds(Path(builder.DEFAULT_PROJECTS), phone=reg.phone, notify=_desktop_notify)
    builder.register(reg, reg.builds)
    return reg
