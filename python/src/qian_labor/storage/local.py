from pathlib import Path
from typing import BinaryIO


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("invalid storage key")
        return path

    def put(self, stream: BinaryIO, storage_key: str) -> None:
        self.save_bytes(stream.read(), storage_key)

    def save_bytes(self, content: bytes, storage_key: str) -> None:
        path = self._path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def read_bytes(self, storage_key: str) -> bytes:
        return self._path(storage_key).read_bytes()

    def open(self, storage_key: str) -> BinaryIO:
        return self._path(storage_key).open("rb")

    def delete(self, storage_key: str) -> None:
        self._path(storage_key).unlink(missing_ok=True)

    def exists(self, storage_key: str) -> bool:
        return self._path(storage_key).exists()
