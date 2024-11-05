from __future__ import annotations

import abc
import dataclasses
import logging
from datetime import timedelta
from typing import Any, Generic, TypeVar

from django.conf import settings
from django.db.models import Q
from sentry_redis_tools.retrying_cluster import RetryingRedisCluster

from sentry.utils import metrics, redis
from sentry.utils.function_cache import cache_func_for_models
from sentry.utils.iterators import chunked
from sentry.workflow_engine.models import (
    DataCondition,
    DataConditionGroup,
    DataPacket,
    Detector,
    DetectorState,
)
from sentry.workflow_engine.types import DetectorGroupKey, DetectorPriorityLevel

logger = logging.getLogger(__name__)
REDIS_TTL = int(timedelta(days=7).total_seconds())


@dataclasses.dataclass(frozen=True)
class DetectorEvaluationResult:
    group_key: DetectorGroupKey
    is_active: bool
    priority: DetectorPriorityLevel
    data: Any


def process_detectors(
    data_packet: DataPacket, detectors: list[Detector]
) -> list[tuple[Detector, list[DetectorEvaluationResult]]]:
    results = []

    for detector in detectors:
        handler = detector.detector_handler

        if not handler:
            continue

        detector_results = handler.evaluate(data_packet)
        detector_group_keys = set()

        for result in detector_results:
            if result.group_key in detector_group_keys:
                # This shouldn't happen - log an error and continue on, but we should investigate this.
                logger.error(
                    "Duplicate detector state group keys found",
                    extra={
                        "detector_id": detector.id,
                        "group_key": result.group_key,
                    },
                )
            detector_group_keys.add(result.group_key)

        if detector_results:
            results.append((detector, detector_results))

    return results


def get_redis_client() -> RetryingRedisCluster:
    cluster_key = settings.SENTRY_WORKFLOW_ENGINE_REDIS_CLUSTER
    return redis.redis_clusters.get(cluster_key)  # type: ignore[return-value]


@dataclasses.dataclass(frozen=True)
class DetectorStateData:
    group_key: DetectorGroupKey
    active: bool
    status: DetectorPriorityLevel
    # Stateful detectors always process data packets in order. Once we confirm that a data packet has been fully
    # processed and all workflows have been done, this value will be used by the stateful detector to prevent
    # reprocessing
    dedupe_value: int
    # Stateful detectors allow various counts to be tracked. We need to update these after we process workflows, so
    # include the updates in the state.
    # This dictionary is in the format {counter_name: counter_value, ...}
    # If a counter value is `None` it means to unset the value
    counter_updates: dict[str, int | None]


T = TypeVar("T")


class DetectorHandler(abc.ABC, Generic[T]):
    def __init__(self, detector: Detector):
        self.detector = detector
        if detector.workflow_condition_group_id is not None:
            results = get_data_group_conditions_and_group(detector.workflow_condition_group_id)
            self.condition_group: DataConditionGroup | None = results[0]
            self.conditions: list[DataCondition] = results[1]
        else:
            self.condition_group = None
            self.conditions = []

    @abc.abstractmethod
    def evaluate(self, data_packet: DataPacket[T]) -> list[DetectorEvaluationResult]:
        pass


class StatefulDetectorHandler(DetectorHandler[T], abc.ABC):
    def __init__(self, detector: Detector):
        super().__init__(detector)
        self.dedupe_updates: dict[DetectorGroupKey, int] = {}
        self.counter_updates: dict[DetectorGroupKey, dict[str, int | None]] = {}
        self.state_updates: dict[DetectorGroupKey, tuple[bool, DetectorPriorityLevel]] = {}

    @property
    @abc.abstractmethod
    def counter_names(self) -> list[str]:
        """
        The names of counters that this detector is going to keep track of.
        """
        pass

    @abc.abstractmethod
    def get_dedupe_value(self, data_packet: DataPacket[T]) -> int:
        """
        Extracts the deduplication value from a passed data packet.
        TODO: This might belong on the `DataPacket` instead.
        """
        pass

    @abc.abstractmethod
    def get_group_key_values(self, data_packet: DataPacket[T]) -> dict[str, int]:
        """
        Extracts the values for all the group keys that exist in the given data packet,
        and returns then as a dict keyed by group_key.
        """
        pass

    def get_state_data(
        self, group_keys: list[DetectorGroupKey]
    ) -> dict[DetectorGroupKey, DetectorStateData]:
        """
        Fetches state data associated with this detector for the associated `group_keys`.
        Returns a dict keyed by each group_key with the fetched `DetectorStateData`.
        If data isn't currently stored, falls back to default values.
        """
        group_key_detectors = self.bulk_get_detector_state(group_keys)
        dedupe_keys = [self.build_dedupe_value_key(gk) for gk in group_keys]
        pipeline = get_redis_client().pipeline()
        for dk in dedupe_keys:
            pipeline.get(dk)
        group_key_dedupe_values = {
            gk: int(dv) if dv else 0 for gk, dv in zip(group_keys, pipeline.execute())
        }
        pipeline.reset()
        counter_updates = {}
        if self.counter_names:
            counter_keys = [
                self.build_counter_value_key(gk, name)
                for gk in group_keys
                for name in self.counter_names
            ]
            for ck in counter_keys:
                pipeline.get(ck)
            vals = [int(val) if val is not None else val for val in pipeline.execute()]
            counter_updates = {
                gk: dict(zip(self.counter_names, values))
                for gk, values in zip(group_keys, chunked(vals, len(self.counter_names)))
            }

        results = {}
        for gk in group_keys:
            detector_state = group_key_detectors.get(gk)
            results[gk] = DetectorStateData(
                group_key=gk,
                active=detector_state.active if detector_state else False,
                status=(
                    DetectorPriorityLevel(int(detector_state.state))
                    if detector_state
                    else DetectorPriorityLevel.OK
                ),
                dedupe_value=group_key_dedupe_values[gk],
                counter_updates=counter_updates[gk],
            )
        return results

    def evaluate(self, data_packet: DataPacket[T]) -> list[DetectorEvaluationResult]:
        """
        Evaluates a given data packet and returns a list of `DetectorEvaluationResult`.
        There will be one result for each group key result in the packet, unless the
        evaluation is skipped due to various rules.
        """
        dedupe_value = self.get_dedupe_value(data_packet)
        group_values = self.get_group_key_values(data_packet)
        all_state_data = self.get_state_data(list(group_values.keys()))
        results = []
        for group_key, group_value in group_values.items():
            result = self.evaluate_group_key_value(
                group_key, group_value, all_state_data[group_key], dedupe_value
            )
            if result:
                results.append(result)
        return results

    def evaluate_group_key_value(
        self,
        group_key: DetectorGroupKey,
        value: int,
        state_data: DetectorStateData,
        dedupe_value: int,
    ) -> DetectorEvaluationResult | None:
        """
        Evaluates a value associated with a given `group_key` and returns a `DetectorEvaluationResult` with the results
        and any state changes that need to be made.

        Checks that we haven't already processed this datapacket for this group_key, and skips evaluation if we have.
        """
        if dedupe_value <= state_data.dedupe_value:
            # TODO: Does it actually make more sense to just do this at the data packet level rather than the group
            # key level?
            metrics.incr("workflow_engine.detector.skipping_already_processed_update")
            return None

        self.enqueue_dedupe_update(group_key, dedupe_value)

        if not self.condition_group:
            metrics.incr("workflow_engine.detector.skipping_invalid_condition_group")
            return None

        status = DetectorPriorityLevel.OK

        for condition in self.conditions:
            # TODO: We need to handle tracking consecutive evaluations before emitting a result here. We're able to
            # store these in `DetectorStateData.counter_updates`, but we don't have anywhere to set the required
            # thresholds at the moment. Probably should be a field on the Detector? Could also be on the condition
            # level, but usually we want to set this at a higher level.
            evaluation = condition.evaluate_value(value)
            if evaluation is not None:
                status = max(status, evaluation)

        is_active = status != DetectorPriorityLevel.OK

        # TODO: We'll increment and change these later, but for now they don't change so just pass an empty dict
        self.enqueue_counter_update(group_key, {})

        if state_data.active != is_active or state_data.status != status:
            self.enqueue_state_update(group_key, is_active, status)
            return DetectorEvaluationResult(
                group_key,
                is_active,
                status,
                {},
            )
        return None

    def enqueue_dedupe_update(self, group_key: DetectorGroupKey, dedupe_value: int):
        self.dedupe_updates[group_key] = dedupe_value

    def enqueue_counter_update(
        self, group_key: DetectorGroupKey, counter_updates: dict[str, int | None]
    ):
        self.counter_updates[group_key] = counter_updates

    def enqueue_state_update(
        self, group_key: DetectorGroupKey, is_active: bool, priority: DetectorPriorityLevel
    ):
        self.state_updates[group_key] = (is_active, priority)

    def build_dedupe_value_key(self, group_key: DetectorGroupKey) -> str:
        if group_key is None:
            group_key = ""
        return f"{self.detector.id}:{group_key}:dedupe_value"

    def build_counter_value_key(self, group_key: DetectorGroupKey, counter_name: str) -> str:
        if group_key is None:
            group_key = ""
        return f"{self.detector.id}:{group_key}:{counter_name}"

    def bulk_get_detector_state(
        self, group_keys: list[DetectorGroupKey]
    ) -> dict[DetectorGroupKey, DetectorState]:
        """
        Bulk fetches detector state for the passed `group_keys`. Returns a dict keyed by each
        `group_key` with the fetched `DetectorStateData`.

        If there's no `DetectorState` row for a `detector`/`group_key` pair then we'll exclude
        the group_key from the returned dict.
        """
        # TODO: Cache this query (or individual fetches, then bulk fetch anything missing)
        query_filter = Q(
            detector_group_key__in=[group_key for group_key in group_keys if group_key is not None]
        )
        if None in group_keys:
            query_filter |= Q(detector_group_key__isnull=True)

        return {
            detector_state.detector_group_key: detector_state
            for detector_state in self.detector.detectorstate_set.filter(query_filter)
        }

    def commit_state_updates(self):
        self._bulk_commit_detector_state()
        self._bulk_commit_redis_state()

    def _bulk_commit_redis_state(self):
        pipeline = get_redis_client().pipeline()
        if self.dedupe_updates:
            for group_key, dedupe_value in self.dedupe_updates.items():
                pipeline.set(self.build_dedupe_value_key(group_key), dedupe_value, ex=REDIS_TTL)

        if self.counter_updates:
            for group_key, counter_updates in self.counter_updates.items():
                for counter_name, counter_value in counter_updates.items():
                    key_name = self.build_counter_value_key(group_key, counter_name)
                    if counter_value is None:
                        pipeline.delete(key_name)
                    else:
                        pipeline.set(key_name, counter_value, ex=REDIS_TTL)

        pipeline.execute()
        self.dedupe_updates.clear()
        self.counter_updates.clear()

    def _bulk_commit_detector_state(self):
        # TODO: We should already have these loaded from earlier, figure out how to cache and reuse
        detector_state_lookup = self.bulk_get_detector_state(
            [update for update in self.state_updates.keys()]
        )
        created_detector_states = []
        updated_detector_states = []
        for group_key, (active, priority) in self.state_updates.items():
            detector_state = detector_state_lookup.get(group_key)
            if not detector_state:
                created_detector_states.append(
                    DetectorState(
                        detector_group_key=group_key,
                        detector=self.detector,
                        active=active,
                        state=priority,
                    )
                )
            elif active != detector_state.active or priority != detector_state.state:
                detector_state.active = active
                detector_state.state = priority
                updated_detector_states.append(detector_state)

        if created_detector_states:
            DetectorState.objects.bulk_create(created_detector_states)

        if updated_detector_states:
            DetectorState.objects.bulk_update(updated_detector_states, ["active", "state"])
        self.state_updates.clear()


@cache_func_for_models(
    [
        (DataConditionGroup, lambda group: (group.id,)),
        (DataCondition, lambda condition: (condition.condition_group_id,)),
    ],
    # There shouldn't be stampedes to fetch this data, and we might update multiple `DataConditionGroup`s at the same
    # time, so we'd prefer to avoid re-fetching this many times. Just bust the cache and re-fetch lazily.
    recalculate=False,
)
def get_data_group_conditions_and_group(
    data_condition_group_id: int,
) -> tuple[DataConditionGroup | None, list[DataCondition]]:
    try:
        group = DataConditionGroup.objects.get(id=data_condition_group_id)
        conditions = list(group.datacondition_set.all())
    except DataConditionGroup.DoesNotExist:
        group = None
        conditions = []
    return group, conditions
