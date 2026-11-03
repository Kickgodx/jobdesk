"""Перенесён в ``jobdesk_platform.workers.liveness``; здесь — алиас того же модуля для старых импортов."""

import sys

from jobdesk_platform.workers import liveness as _impl

sys.modules[__name__] = _impl
