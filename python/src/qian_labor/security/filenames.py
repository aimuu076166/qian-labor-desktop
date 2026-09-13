"""Display-only filename privacy; private bytes and storage keys are unchanged."""
from pathlib import PurePath

from qian_labor.security.masking import mask_sensitive
from qian_labor.security.uploads import UploadRejected


def validate_filename(filename: str) -> None:
    if (not filename or filename in {'.', '..'} or PurePath(filename).name != filename
            or '\\' in filename or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
        raise UploadRejected('DESKTOP_IMPORT_FILENAME_INVALID')


def display_filename(filename: str) -> str:
    # Legacy rows may predate safe-name validation. GET never rewrites those rows.
    name = filename.replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name if ord(c) >= 32 and ord(c) != 127)
    # A dot can separate identifier digits, so masking must see the whole basename.
    return mask_sensitive(name) if name else '未命名材料'


def display_location(location: dict) -> dict:
    return {key: display_filename(value) if key in {'file_name', 'filename', 'original_filename'} and isinstance(value, str)
            else value for key, value in location.items()}
