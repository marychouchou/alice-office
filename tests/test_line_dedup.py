from __future__ import annotations

from alice_office_router.line_dedup import EventDeduplicator


def test_first_occurrence_is_not_duplicate() -> None:
    """An event ID seen for the first time is not flagged as a duplicate."""
    dedup = EventDeduplicator()
    assert dedup.is_duplicate("evt_1") is False


def test_repeated_event_id_is_duplicate() -> None:
    """The same event ID seen twice is flagged as a duplicate the second time."""
    dedup = EventDeduplicator()
    dedup.is_duplicate("evt_1")
    assert dedup.is_duplicate("evt_1") is True


def test_different_event_ids_are_not_duplicates_of_each_other() -> None:
    dedup = EventDeduplicator()
    dedup.is_duplicate("evt_1")
    assert dedup.is_duplicate("evt_2") is False


def test_empty_event_id_is_never_duplicate_and_not_recorded() -> None:
    """LINE events without a webhookEventId should never be skipped as duplicates."""
    dedup = EventDeduplicator()
    assert dedup.is_duplicate("") is False
    assert dedup.is_duplicate("") is False


def test_evicts_oldest_entries_when_over_capacity() -> None:
    """Once max_size is exceeded, older entries are pruned rather than growing unbounded."""
    dedup = EventDeduplicator(max_size=10)
    for i in range(15):
        dedup.is_duplicate(f"evt_{i}")

    assert len(dedup._seen) < 15


def test_most_recent_event_survives_eviction() -> None:
    dedup = EventDeduplicator(max_size=10)
    for i in range(15):
        dedup.is_duplicate(f"evt_{i}")

    assert dedup.is_duplicate("evt_14") is True
