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
