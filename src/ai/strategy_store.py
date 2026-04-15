"""Save and load AI-generated strategy source files."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime


class StrategyStore:
    """Manages a directory of saved AI-generated strategy .py files.

    Maintains an index.json with metadata (class name, description, timestamp).
    """

    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self._index_path = os.path.join(store_dir, "index.json")
        self._index = self._load_index()

    def _load_index(self) -> list[dict]:
        if os.path.exists(self._index_path):
            with open(self._index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def _save_index(self) -> None:
        with open(self._index_path, "w", encoding="utf-8") as f:
            json.dump(self._index, f, indent=2, ensure_ascii=False)

    def _to_filename(self, class_name: str) -> str:
        """Convert PascalCase class name to snake_case filename."""
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()
        return name + ".py"

    def save(self, class_name: str, source: str, description: str = "") -> str:
        """Save strategy source code and update index. Returns the file path."""
        filename = self._to_filename(class_name)
        filepath = os.path.join(self.store_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(source)

        # Update or add index entry
        entry = {
            "class_name": class_name,
            "filename": filename,
            "description": description,
            "saved_at": datetime.now().isoformat(),
        }
        self._index = [e for e in self._index if e["class_name"] != class_name]
        self._index.append(entry)
        self._save_index()

        return filepath

    def list_strategies(self) -> list[dict]:
        """Return list of saved strategy metadata dicts."""
        return list(self._index)

    def load_source(self, class_name: str) -> str | None:
        """Load the source code for a saved strategy by class name."""
        for entry in self._index:
            if entry["class_name"] == class_name:
                filepath = os.path.join(self.store_dir, entry["filename"])
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        return f.read()
        return None

    def delete(self, class_name: str) -> None:
        """Delete a saved strategy file and remove from index."""
        for entry in self._index:
            if entry["class_name"] == class_name:
                filepath = os.path.join(self.store_dir, entry["filename"])
                if os.path.exists(filepath):
                    os.remove(filepath)
                break
        self._index = [e for e in self._index if e["class_name"] != class_name]
        self._save_index()

    # ── External file import ──

    def preview_external_file(self, src_path: str) -> dict:
        """Validate an external .py file without copying it.

        Returns a dict describing the strategy:
        {"class_name": str, "docstring": str, "kline_minute": int|None,
         "exists": bool (True if class_name is already in the index)}

        Raises ValueError on any validation failure with a user-readable
        message.
        """
        if not os.path.isfile(src_path):
            raise ValueError(f"File not found: {src_path}")
        if not src_path.endswith(".py"):
            raise ValueError("File must be a .py Python file")

        try:
            with open(src_path, "r", encoding="utf-8") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError) as e:
            raise ValueError(f"Cannot read file: {e}") from e

        # Reuse the AI code validator (syntax, allowed imports, no
        # forbidden builtins, has a class definition).
        from .code_sandbox import validate_code, load_strategy_from_source
        errors = validate_code(source)
        if errors:
            raise ValueError("Validation failed:\n  - " + "\n  - ".join(errors))

        # Actually load to verify it's a BacktestStrategy subclass and
        # instantiates without error.
        try:
            cls = load_strategy_from_source(source)
        except Exception as e:
            raise ValueError(f"Could not load strategy class: {e}") from e

        try:
            cls()  # smoke-test default ctor
        except Exception as e:
            raise ValueError(f"Strategy instantiation failed: {e}") from e

        class_name = cls.__name__
        docstring = (cls.__doc__ or "").strip()
        kline_minute = getattr(cls, "kline_minute", None)
        exists = any(e["class_name"] == class_name for e in self._index)

        return {
            "class_name": class_name,
            "docstring": docstring,
            "kline_minute": kline_minute,
            "exists": exists,
            "source": source,  # cached to avoid re-reading on commit
        }

    def import_external_file(
        self,
        src_path: str,
        description: str = "",
        overwrite: bool = False,
    ) -> tuple[str, str]:
        """Validate, copy, and register an external .py strategy file.

        Returns ``(class_name, dest_filename)``.

        Raises:
            ValueError: validation failed (syntax, imports, no class)
            FileExistsError: class_name already in index and ``overwrite=False``
        """
        info = self.preview_external_file(src_path)
        class_name = info["class_name"]

        if info["exists"] and not overwrite:
            raise FileExistsError(
                f"Strategy '{class_name}' already exists in the index. "
                f"Pass overwrite=True to replace it.")

        # Reuse save() which writes the file and updates the index.
        # save() already replaces existing entries with the same class_name.
        self.save(class_name, info["source"], description=description)
        return class_name, self._to_filename(class_name)
