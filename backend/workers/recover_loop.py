"""Перенесён в ``jobdesk_platform.workers.recover_loop``; здесь — алиас того же модуля для старых импортов."""

import sys

from jobdesk_platform.workers import recover_loop as _impl

sys.modules[__name__] = _impl
