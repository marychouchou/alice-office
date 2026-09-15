"""Write-once seeding of repo templates and deployment secrets into a room.

Every ``ensure_*_seed(room_id, config)`` function here copies something from
a repo-local source (HERMES_TEMPLATES_DIR, or a deployment secret drop
location) into that room's own ``data/<room_id>/`` directory exactly once:
if the destination already exists, the call is a no-op. This lets a room
freely edit its own seeded copy afterward — a repo template update, or a
container recreation, never silently overwrites a room's customization.

This module only moves files around; it never imports docker (see AGENTS.md
Growth Discipline — "docker SDK 只允許在 container_manager.py import") and
never renders config.yaml (that stays in container_manager.py, since
_format_mcp_section needs CONTAINER_MCP_DIR from that module and moving it
here would create a circular import).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Final

import yaml

from alice_office_router.config import Settings

logger = logging.getLogger(__name__)

# Filenames/patterns _seed_templates never copies from a template into a
# room: node_modules/package-lock.json are shared via /opt/node_modules (see
# container_manager.CONTAINER_MCP_DIR) rather than duplicated per room;
# __pycache__/*.pyc are build artifacts; .env is handled explicitly (seeded
# from .env.example, see _seed_templates' seed_dotenv) rather than copied
# verbatim, since a real .env sitting in a dev checkout of the template must
# never leak into a room's seeded copy.
_SEED_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".env", "node_modules", "package-lock.json"
)

# Filename of the agent persona template, both under HERMES_TEMPLATES_DIR
# (repo path: src/hermes/SOUL.md) and inside a room's own data dir. Hermes
# only ever reads $HERMES_HOME/SOUL.md (HERMES_HOME = CONTAINER_DATA_DIR for
# every room's container) — never the working directory — so this is a
# single file copied verbatim, not a str.format() template like
# config.template.yml: there is no per-room dynamic value worth injecting
# yet (a room's LINE display name isn't known at container-creation time),
# and a static copy sidesteps Markdown content that happens to contain a
# literal "{...}" breaking str.format().
SOUL_FILENAME: Final = "SOUL.md"


def _seed_templates(
    templates_root: Path,
    dest_root: Path,
    *,
    seed_dotenv: bool,
    skip: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Copy each template subdirectory into dest_root, once per name.

    Write-once: a name already present under dest_root is left completely
    untouched, so a room's own edits to its seeded copy — or a room created
    before a template was added/changed — never get silently overwritten.
    Mirrors how container_manager._ensure_config_yaml treats config.yaml.

    Args:
        templates_root: Directory holding one subdirectory per template
            (e.g. HERMES_TEMPLATES_DIR/mcp or HERMES_TEMPLATES_DIR/plugin).
        dest_root: Room-local destination directory (e.g.
            DATA_DIR/<room_id>/mcp or DATA_DIR/<room_id>/plugins).
        seed_dotenv: When True, a template's .env.example (if present) is
            also seeded as a sibling .env in the destination — for MCP
            servers that load their own secrets from a .env file next to
            their source.
        skip: Template directory names to skip entirely (e.g. Google-gated
            MCPs when Google OAuth isn't configured for this deployment).
    """
    if not templates_root.is_dir():
        return
    dest_root.mkdir(parents=True, exist_ok=True)
    for template_dir in sorted(templates_root.iterdir()):
        if not template_dir.is_dir() or template_dir.name in skip:
            continue
        dest_dir = dest_root / template_dir.name
        if dest_dir.exists():
            continue
        shutil.copytree(template_dir, dest_dir, ignore=_SEED_IGNORE)
        if seed_dotenv:
            env_example = dest_dir / ".env.example"
            env_path = dest_dir / ".env"
            if env_example.exists() and not env_path.exists():
                shutil.copyfile(env_example, env_path)
        logger.info(f"Seeded template [{template_dir.name}] into {dest_dir}")


def _google_gated_template_names(mcp_templates_root: Path) -> frozenset[str]:
    """Find MCP template names whose manifest requires Google OAuth.

    Args:
        mcp_templates_root: HERMES_TEMPLATES_DIR/mcp — directory holding one
            subdirectory per MCP template.

    Returns:
        Frozen set of template directory names with `requires_google_oauth:
        true` in their mcp.manifest.yaml. A missing/malformed manifest is
        tolerated (not skipped) — this is only used to decide what to skip
        seeding, never to fail room creation.
    """
    gated: set[str] = set()
    if not mcp_templates_root.is_dir():
        return frozenset(gated)
    for template_dir in mcp_templates_root.iterdir():
        manifest_path = template_dir / "mcp.manifest.yaml"
        if not template_dir.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.error(f"Failed to read manifest {manifest_path}: {exc}")
            continue
        if isinstance(manifest, dict) and manifest.get("requires_google_oauth"):
            gated.add(template_dir.name)
    return frozenset(gated)


def ensure_mcp_seed(room_id: str, config: Settings) -> None:
    """Seed every MCP server template into a room's data dir, once.

    After this runs, data/<room_id>/mcp/<name>/ is the room's own editable
    copy of that MCP's source — the room may freely modify it (a container
    restart is required for Hermes to pick up changes; it has no hot-reload).
    Repo template updates never reach a room that already has a seeded copy.
    Templates requiring Google OAuth are skipped when this deployment has no
    Google OAuth configured (see Settings.google_oauth_enabled) — a room
    created while disabled never gets those MCPs seeded (write-once means
    enabling Google later won't retroactively add them to existing rooms).

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing the templates and data directories.
    """
    mcp_templates_root = config.HERMES_TEMPLATES_DIR / "mcp"
    skip = (
        frozenset()
        if config.google_oauth_enabled
        else _google_gated_template_names(mcp_templates_root)
    )
    _seed_templates(
        mcp_templates_root,
        config.DATA_DIR / room_id / "mcp",
        seed_dotenv=True,
        skip=skip,
    )


def ensure_plugin_seed(room_id: str, config: Settings) -> None:
    """Seed every plugin template into a room's data dir, once.

    Same write-once semantics as ensure_mcp_seed. The plugin's own
    executable dependencies (sympy, pymupdf, etc.) resolve via Hermes's
    Python venv baked into the image, not anything seeded here — only the
    plugin's own source (tools.py, scripts/, ...) is per-room.

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing the templates and data directories.
    """
    _seed_templates(
        config.HERMES_TEMPLATES_DIR / "plugin",
        config.DATA_DIR / room_id / "plugins",
        seed_dotenv=False,
    )


def ensure_soul_seed(room_id: str, config: Settings) -> None:
    """Copy this deployment's default agent persona into a room, once.

    Hermes reads its persona from $HERMES_HOME/SOUL.md — the highest-
    priority layer of its own prompt stack, above tool guidance, skills,
    AGENTS.md/.hermes.md-style context files, and platform prompts — and
    HERMES_HOME is this room's own bind-mounted data dir, so this is
    effectively a per-room persona. If this seed never runs (or the room
    predates it), Hermes writes its own generic default SOUL.md on first
    boot and — like every write-once seed here — never overwrites it
    afterward, so seeding late never takes effect; see _create_container's
    call order for why this must run before the container starts.

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing the templates and data directories.
    """
    dest = config.DATA_DIR / room_id / SOUL_FILENAME
    if dest.exists():
        return
    template = config.HERMES_TEMPLATES_DIR / SOUL_FILENAME
    if not template.exists():
        logger.error(
            f"Missing SOUL.md template at {template}; "
            f"room [{room_id}] will get Hermes's default persona"
        )
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, dest)
    logger.info(f"Seeded SOUL.md into room [{room_id}]")


def _copy_atomically(src: Path, dest: Path) -> None:
    """Copy `src` to `dest` so that `dest` is never observable half-written.

    ensure_google_seed can run concurrently from a container warm-up worker
    thread and from /oauth/start on the event loop, and the latter reads the
    file straight after seeding; a plain copyfile truncates `dest` first, so
    that read could see an empty file. Writing to a temp file in the same
    directory and renaming over `dest` makes each writer's result appear
    whole, and two racing writers simply install identical content.

    Args:
        src: The file to copy.
        dest: Where to put it; its parent directory must exist.
    """
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp, src.open("rb") as source:
            shutil.copyfileobj(source, tmp)
        tmp_path.replace(dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def ensure_google_seed(room_id: str, config: Settings) -> None:
    """Copy this deployment's GCP OAuth client credentials into a room, once.

    Mirrors ensure_mcp_seed/ensure_plugin_seed's write-once semantics, but
    the "template" here is deployment secrets (config.google_dir, the
    operator's one-time drop location — see README「Google Workspace 整合」)
    rather than versioned source under HERMES_TEMPLATES_DIR. Once copied, a
    room's own data/<room_id>/google/ is never touched again by this repo —
    deleting data/<room_id>/ wipes this room's Google authorization (both
    its tokens.json and its credential copies) along with everything else,
    by design.

    Called from both container_manager._create_container (so a fresh
    container's bind mount has something to see) and
    google_oauth.oauth_start: a room's very first message is usually gated,
    and the gate-blocked turn only *starts* the container in the background
    (core._warm_container) — the user can click the auth link before that
    warm-up has created the data dir, or after it failed — so the OAuth
    routes must be able to seed a room's google/ dir on demand, not only at
    container-creation time.

    No-op when this deployment has no Google OAuth configured.

    Args:
        room_id: Unique identifier for the chatroom.
        config: Application settings containing the seed source and
            per-room data directory.
    """
    if not config.google_oauth_enabled:
        return
    dest_dir = config.room_google_dir(room_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in (config.google_web_creds_path, config.google_installed_creds_path):
        dest = dest_dir / src.name
        if src.exists() and not dest.exists():
            _copy_atomically(src, dest)
            logger.info(f"Seeded Google credential [{src.name}] into room [{room_id}]")
