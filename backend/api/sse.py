"""Перенесён в ``jobdesk_platform.sse``; здесь — алиас того же модуля для старых импортов."""

import sys

from jobdesk_platform import sse as _impl

sys.modules[__name__] = _impl
