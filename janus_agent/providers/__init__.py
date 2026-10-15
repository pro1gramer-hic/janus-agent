"""Provider registry.

Every module in this package is imported, and every Provider subclass it defines is
registered under its `name`. Adding a provider means adding one file here.
"""

import importlib
import pkgutil

from janus_agent.config import ModelConfig
from janus_agent.llm.base import Provider


class UnknownProvider(Exception):
    pass


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def available() -> dict:
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")
    return {cls.name: cls for cls in _subclasses(Provider) if getattr(cls, "name", None)}


def create(config: ModelConfig) -> Provider:
    providers = available()
    if config.provider not in providers:
        raise UnknownProvider(f"unknown provider '{config.provider}'. "
                              f"Available: {', '.join(sorted(providers))}")
    return providers[config.provider](config, config.resolve_api_key())
