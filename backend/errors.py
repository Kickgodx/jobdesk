"""Перенесён в ``jobdesk_platform.errors``; здесь — алиас того же модуля для старых импортов."""

import sys

from jobdesk_platform import errors as _impl

sys.modules[__name__] = _impl
