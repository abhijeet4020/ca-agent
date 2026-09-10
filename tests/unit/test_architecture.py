"""Enforces the module layering rules from docs/design/ARCHITECTURE.md.

ARCHITECTURE.md requires acyclic dependencies, higher layers depending only on lower ones, and
no dependencies between modules in the same layer. Those are easy to violate accidentally with
a single convenient import, so they are checked by parsing the real import graph rather than
left to review.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "ca_agent"
_PACKAGE = "ca_agent"

#: Lower number means lower layer. A module may import strictly lower layers only.
_LAYERS: dict[str, int] = {
    "core": 0,
    "config": 1,
    "storage": 1,
    "catalog": 2,
    "versioning": 2,
    "journal": 2,
    "readers": 3,
    "vision": 3,
    "chunking": 3,
    "docgen": 3,
    "gold": 3,
    "pipeline": 4,
    "cli": 5,
}


def _module_names() -> list[str]:
    return sorted(name for name in _LAYERS if (_SOURCE_ROOT / name).is_dir())


def _internal_imports(module: str) -> set[str]:
    """Every other ca_agent module this module imports, found by parsing its source."""
    imported: set[str] = set()
    for path in (_SOURCE_ROOT / module).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.update(_target_module(node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.update(_target_module(alias.name))
    return imported - {module}


def _target_module(dotted: str) -> set[str]:
    parts = dotted.split(".")
    if len(parts) >= 2 and parts[0] == _PACKAGE and parts[1] in _LAYERS:
        return {parts[1]}
    return set()


@pytest.mark.parametrize("module", _module_names())
def test_module_does_not_import_its_own_layer(module):
    # Arrange
    own_layer = _LAYERS[module]

    # Act
    same_layer = {name for name in _internal_imports(module) if _LAYERS[name] == own_layer}

    # Assert - ARCHITECTURE.md: modules in the same layer cannot depend on each other
    assert not same_layer, f"{module} imports same-layer module(s) {sorted(same_layer)}"


@pytest.mark.parametrize("module", _module_names())
def test_module_does_not_import_a_higher_layer(module):
    # Arrange
    own_layer = _LAYERS[module]

    # Act
    higher = {name for name in _internal_imports(module) if _LAYERS[name] > own_layer}

    # Assert
    assert not higher, f"{module} (layer {own_layer}) imports higher-layer module(s) {sorted(higher)}"


def test_import_graph_is_acyclic():
    # Arrange
    graph = {module: _internal_imports(module) for module in _module_names()}
    visiting: set[str] = set()
    settled: set[str] = set()

    def walk(node: str, trail: tuple[str, ...]) -> None:
        if node in settled:
            return
        if node in visiting:
            raise AssertionError(f"import cycle: {' -> '.join((*trail, node))}")
        visiting.add(node)
        for dependency in sorted(graph.get(node, ())):
            walk(dependency, (*trail, node))
        visiting.discard(node)
        settled.add(node)

    # Act / Assert
    for module in graph:
        walk(module, ())


def test_core_layer_has_no_internal_dependencies():
    # Arrange / Act / Assert - L0 must stay pure so it can be imported from anywhere
    assert _internal_imports("core") == set()


def test_every_module_directory_is_assigned_a_layer():
    # Arrange - a new module folder must be placed in the layer map deliberately
    on_disk = {
        entry.name
        for entry in _SOURCE_ROOT.iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    }

    # Act
    unassigned = on_disk - set(_LAYERS)

    # Assert
    assert not unassigned, f"module(s) {sorted(unassigned)} have no layer in ARCHITECTURE.md"


def test_every_source_file_has_a_purpose_comment():
    # Arrange - required by .agents/workingrules.md for every new source file
    missing: list[str] = []

    # Act
    for path in _SOURCE_ROOT.rglob("*.py"):
        if path.name == "__init__.py" and path.stat().st_size == 0:
            continue
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        if not docstring or not docstring.lstrip().startswith("Purpose:"):
            missing.append(str(path.relative_to(_SOURCE_ROOT)))

    # Assert
    assert not missing, f"source file(s) without a Purpose comment: {missing}"
