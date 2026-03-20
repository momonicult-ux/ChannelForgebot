"""
prompts — Prompt template loader.

Loads all .txt files from this directory at import time.
Injects {base_rules} from _base.txt into any prompt that references it.
Exposes get() for name-based access.

Registry keys:
  "post"           → prompts/post.txt
  "rewrite.shorter" → prompts/rewrite/shorter.txt
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent
_registry: dict[str, str] = {}


def _load_all() -> None:
    """Read every .txt file and populate the registry."""
    # Load base rules first
    base_path = _PROMPTS_DIR / "_base.txt"
    base_rules = base_path.read_text(encoding="utf-8").strip() if base_path.exists() else ""

    for txt_file in sorted(_PROMPTS_DIR.rglob("*.txt")):
        if txt_file.name.startswith("_"):
            continue  # Skip _base.txt etc.

        # Build registry key: "post", "rewrite.shorter", "rewrite.meme"
        relative = txt_file.relative_to(_PROMPTS_DIR).with_suffix("")
        key = str(relative).replace("/", ".").replace("\\", ".")

        text = txt_file.read_text(encoding="utf-8").strip()

        # Inject base rules if placeholder present
        if "{base_rules}" in text:
            text = text.replace("{base_rules}", base_rules)

        _registry[key] = text

    logger.info("Prompts loaded: %d templates from %s", len(_registry), _PROMPTS_DIR)


def get(name: str) -> str:
    """
    Retrieve a prompt by registry key.

    Args:
        name: e.g. "post", "rewrite.shorter", "audit"

    Returns:
        The prompt text with base_rules already injected.

    Raises:
        KeyError: if the prompt name is not found.
    """
    if name not in _registry:
        available = ", ".join(sorted(_registry.keys()))
        raise KeyError(f"Prompt '{name}' not found. Available: {available}")
    return _registry[name]


def get_with_vars(name: str, **kwargs: str) -> str:
    """
    Retrieve a prompt and fill in placeholders.

    Use for prompts that contain {posts}, {original_post}, {analysis} etc.
    """
    return get(name).format(**kwargs)


def list_prompts() -> list[str]:
    """Return all registered prompt names."""
    return sorted(_registry.keys())


# Load on import
_load_all()
