"""AgentDock compatibility surface.

The implementation is split into focused modules, while this package keeps the
historic ``import agentdock`` surface intact.  The small namespace bridge is
intentional: older callers and tests patch symbols on ``agentdock`` directly,
so those patches must remain visible to the extracted modules during the
transition.
"""

import sys
from importlib import import_module
from types import ModuleType


MODULE_NAMES = (
    "config",
    "schemas",
    "db",
    "git_ops",
    "preflight",
    "codex",
    "orchestrator",
    "tasks",
    "mission",
    "timeline",
    "api",
    "app",
)

_MODULES = [import_module(f".{name}", __name__) for name in MODULE_NAMES]
_MODULE_BY_NAME = dict(zip(MODULE_NAMES, _MODULES))

_SKIP_SYMBOLS = {
    "__builtins__",
    "__cached__",
    "__file__",
    "__loader__",
    "__package__",
    "__spec__",
}


def _collect_symbols():
    symbols = {}
    for module in _MODULES:
        for name, value in vars(module).items():
            if name in _SKIP_SYMBOLS or name.startswith("__"):
                continue
            symbols.setdefault(name, value)
    return symbols


_SYMBOLS = _collect_symbols()
_NAMESPACE_NAMES = set(_SYMBOLS)


class _FacadeModule(ModuleType):
    """Keep legacy facade monkeypatches in sync with extracted modules."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in _NAMESPACE_NAMES:
            for module in _MODULES:
                module.__dict__[name] = value


sys.modules[__name__].__class__ = _FacadeModule

for _module_name, _module in _MODULE_BY_NAME.items():
    globals()[_module_name] = _module

# Install the shared symbol table in every extracted module.  Functions keep
# their owning module's globals, but cross-layer calls retain the old flat
# namespace until each boundary can be tightened independently.
for _module in _MODULES:
    _module.__dict__.update(_SYMBOLS)
globals().update(_SYMBOLS)

__all__ = sorted(name for name in _SYMBOLS if not name.startswith("_"))
