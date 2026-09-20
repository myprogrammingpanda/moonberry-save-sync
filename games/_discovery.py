"""Auto-discovers GameAdapter subclasses under games/. Adding a new game
means dropping a new games/<id>.py file -- nothing here or elsewhere needs
to be edited to register it."""

import importlib
import inspect
import pkgutil

import games
from core.game_base import GameAdapter


def discover_adapters() -> dict[str, type]:
    adapters: dict[str, type] = {}
    for _, module_name, _ in pkgutil.iter_modules(games.__path__):
        if module_name.startswith("_"):
            continue
        module = importlib.import_module(f"games.{module_name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, GameAdapter) and obj is not GameAdapter and obj.__module__ == module.__name__:
                adapters[obj.game_id] = obj
    return adapters
