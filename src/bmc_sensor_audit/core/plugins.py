"""How a vertical's registration reaches a process that did not import it.

`register()` is a Python call. A consumer running the command line never makes
one, so without this the door exists and the entrance most people use does not
reach it.

Three ways in, all resolved before anything is read:

- an entry point in the group `bmc_sensor_audit.plugins` (this package declares
  one for the vertical it ships);
- `BMC_SENSOR_AUDIT_PLUGINS`, an `os.pathsep`-separated list of specs;
- `--plugin SPEC`, repeatable.

A spec is `module.path`, `module.path:callable`, or `path/to/file.py[:callable]`.
The callable defaults to `register`.

**A plugin that fails is a failure, not a skip.** A run that needed the kinds a
broken vertical would have registered must be refused by name, here, rather than
raising somewhere downstream about a vocabulary that was never supplied.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from .vocabulary import PluginError

ENTRY_POINT_GROUP = "bmc_sensor_audit.plugins"
ENVIRONMENT_VARIABLE = "BMC_SENSOR_AUDIT_PLUGINS"


@dataclass(frozen=True)
class Loaded:
    """One registration that happened, and where it came from."""

    origin: str
    spec: str
    said: str = ""


def _entry_points():
    from importlib.metadata import entry_points
    try:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    except TypeError:                                  # pragma: no cover
        return list(entry_points().get(ENTRY_POINT_GROUP, []))


def _call(register, spec: str, origin: str) -> Loaded:
    if not callable(register):
        raise PluginError(f"{origin}: {spec} is not callable")
    try:
        said = register()
    except Exception as error:                          # noqa: BLE001
        raise PluginError(
            f"{origin}: {spec} raised while registering: "
            f"{type(error).__name__}: {error}") from error
    return Loaded(origin=origin, spec=spec, said=str(said) if said else "")


def load_spec(spec: str, origin: str = "--plugin") -> Loaded:
    """Resolve one spec to a callable and call it."""
    target, _, attribute = spec.partition(":")
    attribute = attribute or "register"
    if target.endswith(".py"):
        path = Path(target)
        if not path.is_file():
            raise PluginError(f"{origin}: {target} is not a file")
        name = f"_bsa_plugin_{path.stem}"
        loader = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(loader)
        sys.modules[name] = module
        try:
            loader.loader.exec_module(module)
        except Exception as error:                      # noqa: BLE001
            raise PluginError(f"{origin}: {target} could not be imported: "
                              f"{type(error).__name__}: {error}") from error
    else:
        try:
            module = importlib.import_module(target)
        except Exception as error:                      # noqa: BLE001
            raise PluginError(f"{origin}: {target} could not be imported: "
                              f"{type(error).__name__}: {error}") from error
    if not hasattr(module, attribute):
        raise PluginError(f"{origin}: {target} has no {attribute!r}")
    return _call(getattr(module, attribute), spec, origin)


def load_all(explicit: Iterable[str] = (), *, entry_points: bool = True,
             environment: bool = True) -> List[Loaded]:
    """Load every vertical from every enabled source, and say what loaded.

    Precedence is deliberate and is the order below: entry points first, then
    the environment, then `--plugin`. Each registration REPLACES the last, so a
    later source wins -- which is what makes an explicit `--plugin` able to
    override whatever happens to be installed.

    TWO ENTRY POINTS ARE REFUSED, and that is the one case the ordering cannot
    resolve. Installing a second vertical is how a domain arrives, so it is not
    an error; but two of them offer two answers to *what kind of thing is this*
    and nothing in the installation says which was meant. Silently keeping
    whichever `importlib` happened to yield last would change every verdict of
    the other vertical's audit, with no line of output saying so. An explicit
    selection resolves it, so the refusal names how.
    """
    loaded: List[Loaded] = []
    from_entry_points: List[Loaded] = []
    if entry_points:
        for point in _entry_points():
            try:
                register = point.load()
            except Exception as error:                  # noqa: BLE001
                raise PluginError(
                    f"entry point {point.name!r} ({point.value}) could not be "
                    f"loaded: {type(error).__name__}: {error}") from error
            one = _call(register, point.value, f"entry point {point.name}")
            from_entry_points.append(one)
            loaded.append(one)
    resolved = bool(explicit) or (
        environment and os.environ.get(ENVIRONMENT_VARIABLE, "").strip())
    if len(from_entry_points) > 1 and not resolved:
        names = ", ".join(sorted(one.spec for one in from_entry_points))
        raise PluginError(
            f"{len(from_entry_points)} verticals are installed and offer a "
            f"vocabulary: {names}. Only one can be in force, and nothing here "
            f"says which you meant -- so this refuses rather than picking. "
            f"Choose with --plugin SPEC, or add --no-entry-points and name it, "
            f"or uninstall the one you did not mean")
    if environment:
        for spec in filter(None, os.environ.get(ENVIRONMENT_VARIABLE, "").split(os.pathsep)):
            loaded.append(load_spec(spec, ENVIRONMENT_VARIABLE))
    for spec in explicit:
        loaded.append(load_spec(spec))
    return loaded
