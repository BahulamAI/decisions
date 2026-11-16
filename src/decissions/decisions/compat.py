"""Preserve the package's Python 3.10 support."""
try:
    from enum import StrEnum
except ImportError:  # Python 3.10
    from enum import Enum
    class StrEnum(str, Enum):
        def __str__(self):
            return self.value
