"""TTL purge для hh.hh_listings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobdesk_hh.db import listings as hh_repo
from jobdesk_hh.db.models import HhListingRow


def test_purge_expired_listings(db_session):
    now = datetime.now(UTC)

    with db_session as session:
        session.add(
            HhListingRow(
                hh_id="old",
                name="Old",
                content_hash="abc",
                last_seen_at=now - timedelta(days=50),
            )
        )
        session.add(
            HhListingRow(
                hh_id="new",
                name="New",
                content_hash="def",
                last_seen_at=now - timedelta(days=1),
            )
        )
        session.commit()

        deleted = hh_repo.purge_expired_listings(session, now=now, ttl_days=40)
        session.commit()
        assert deleted == 1
        assert session.get(HhListingRow, "old") is None
        assert session.get(HhListingRow, "new") is not None
