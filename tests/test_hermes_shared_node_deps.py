from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MCP_ROOT = REPO_ROOT / "src" / "hermes" / "mcp"
SHARED_PACKAGE_JSON = MCP_ROOT / "package.json"


def _load_dependencies(package_json: Path) -> dict[str, str]:
    """Load the `dependencies` mapping from a package.json file.

    Args:
        package_json: Path to the package.json file to read.

    Returns:
        Mapping of dependency name to version specifier string.
    """
    data = json.loads(package_json.read_text(encoding="utf-8"))
    deps: dict[str, str] = data.get("dependencies", {})
    return deps


def test_every_mcp_dependency_is_covered_by_shared_package_json() -> None:
    """Every MCP template's deps must appear in the shared package.json.

    src/hermes/mcp/package.json is baked into the Hermes image at
    /opt/node_modules and must be the union of every MCP template's own
    dependencies, with identical version specifiers, or a room's seeded
    server.mjs will fail its ESM node_modules walk-up resolution at runtime.
    """
    shared_deps = _load_dependencies(SHARED_PACKAGE_JSON)
    template_package_jsons = sorted(MCP_ROOT.glob("*/package.json"))
    assert template_package_jsons, "expected at least one MCP template package.json"

    for template_package_json in template_package_jsons:
        template_deps = _load_dependencies(template_package_json)
        for name, specifier in template_deps.items():
            assert name in shared_deps, (
                f"{template_package_json}: dependency {name!r} missing from {SHARED_PACKAGE_JSON}"
            )
            assert shared_deps[name] == specifier, (
                f"{template_package_json}: dependency {name!r} specifier "
                f"{specifier!r} does not match {SHARED_PACKAGE_JSON}'s "
                f"{shared_deps[name]!r}"
            )
