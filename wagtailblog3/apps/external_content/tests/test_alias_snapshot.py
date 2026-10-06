"""Alias 比较必须遵循 Revision 持久化编码，同时拒绝非法和类型篡改。"""

import json
from datetime import datetime, timezone

from django.core.serializers.json import DjangoJSONEncoder
from django.test import SimpleTestCase

from external_content.copying import _alias_snapshot_key
from external_content.exceptions import PublicationRevisionInvalidError


class AliasSnapshotEncodingTests(SimpleTestCase):
    def test_memory_datetime_matches_persisted_revision_json(self) -> None:
        snapshot = {
            "go_live_at": datetime(2026, 10, 7, 8, 0, 0, 123456, tzinfo=timezone.utc),
            "enabled": True,
        }
        persisted = json.loads(json.dumps(snapshot, cls=DjangoJSONEncoder))
        self.assertEqual(_alias_snapshot_key(snapshot), _alias_snapshot_key(persisted))

    def test_boolean_and_integer_snapshots_are_not_equivalent(self) -> None:
        self.assertNotEqual(
            _alias_snapshot_key({"enabled": True}),
            _alias_snapshot_key({"enabled": 1}),
        )

    def test_illegal_values_fail_closed(self) -> None:
        for value in (float("nan"), float("inf"), object()):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(PublicationRevisionInvalidError):
                    _alias_snapshot_key({"value": value})
