"""Перенесён в ``jobdesk_platform.workers.healthcheck``; здесь — алиас для старых импортов.

``python -m backend.workers.healthcheck`` (healthcheck в docker-compose) по-прежнему работает.
"""

import sys

from jobdesk_platform.workers import healthcheck as _impl

if __name__ == "__main__":
    sys.exit(_impl.main())

sys.modules[__name__] = _impl
