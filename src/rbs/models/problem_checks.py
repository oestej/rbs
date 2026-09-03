"""Integrity validators for SolverProblem (mixin)."""

from __future__ import annotations

from datetime import date

from rbs.models.case_blocks import ManualClinicBlock, ResidentRotationOverride
from rbs.models.enums import RotationKind, Session
from rbs.models.special import SpecialRotation


class SolverIntegrityMixin:
    """The ``_check_*`` validators run by ``SolverProblem.check_integrity``."""

    def _check_special_rotations(
        self,
        resident_ids: set[str],
        first_day: date,
        last_day: date,
    ) -> None:
        occupied: dict[tuple[str, date, Session], SpecialRotation] = {}
        for special in self.special_rotations:
            unknown = set(special.resident_ids) - resident_ids
            if unknown:
                raise ValueError(
                    f"special rotation {special.id!r} references unknown resident(s): "
                    + ", ".join(sorted(unknown))
                )
            if special.start_date < first_day or special.end_date > last_day:
                raise ValueError(
                    f"special rotation {special.id!r} is outside academic year "
                    f"{first_day.isoformat()}..{last_day.isoformat()}"
                )
            sessions = tuple(Session) if special.session is None else (special.session,)
            for resident_id in special.resident_ids:
                for calendar_day in special.dates():
                    for session in sessions:
                        key = resident_id, calendar_day, session
                        previous = occupied.get(key)
                        if previous is not None:
                            raise ValueError(
                                f"special rotations {previous.name!r} and {special.name!r} "
                                f"overlap for {resident_id} on {calendar_day.isoformat()} "
                                f"{session.value}"
                            )
                        occupied[key] = special

    def _check_locks(self, rotation_ids: set[str]) -> None:
        by_id = self.residents_by_id
        seen: dict[tuple[str, int], tuple[str, bool]] = {}
        for lock in self.locks:
            if lock.resident_id not in by_id:
                raise ValueError(f"lock references unknown resident {lock.resident_id!r}")
            if lock.rotation_id not in rotation_ids:
                raise ValueError(f"lock references unknown rotation {lock.rotation_id!r}")
            resident = by_id[lock.resident_id]
            if lock.grouping_exempt:
                if (
                    lock.elective
                    or self.rotation_group_for(
                        resident.pgy,
                        lock.rotation_id,
                    )
                    is None
                ):
                    raise ValueError(
                        "a grouping-exempt lock must target a grouped Mandatory rotation"
                    )
            allowed = self.rotation_ids_for_pgy(resident.pgy)
            if lock.rotation_id not in allowed:
                raise ValueError(
                    f"lock: {self.training_level_label(resident.pgy, compact=True)} "
                    f"{resident.id} cannot take {lock.rotation_id!r}"
                )
            if lock.elective and not (
                self.is_elective_option(lock.rotation_id)
                or self.is_elective_fallback_rotation(
                    lock.rotation_id,
                    resident.pgy,
                )
            ):
                raise ValueError(
                    f"lock: {lock.rotation_id!r} is not an eligible Elective rotation "
                    "or Clinic fallback"
                )
            available = self.available_weeks(
                resident.pgy,
                lock.rotation_id,
                elective=lock.elective,
            )
            if len(lock.weeks) > available:
                raise ValueError(
                    f"lock: {resident.id} has {available} weeks of {lock.rotation_id}, "
                    f"but {len(lock.weeks)} weeks were locked"
                )
            rotation = self.rotation(lock.rotation_id)
            vacation_overlap = set(lock.weeks) & self.resident_scheduling_vacation_weeks(
                resident.id
            )
            block_durations = self.block_durations_for_pgy(
                resident.pgy,
                lock.rotation_id,
                elective=lock.elective,
            )
            if lock.exact_block:
                duration = len(lock.weeks)
                if duration not in block_durations:
                    configured = ", ".join(str(item) for item in sorted(block_durations))
                    raise ValueError(
                        f"lock: {lock.rotation_id} has no {duration}-week block for "
                        f"{self.training_level_label(resident.pgy, compact=True)}; "
                        f"configured duration(s): {configured}"
                    )
                if (lock.weeks[0] - 1) % self.calendar.block_start_alignment:
                    raise ValueError(f"lock: exact block start week {lock.weeks[0]} is not aligned")
            exact_config = (
                rotation.block_config(resident.pgy, len(lock.weeks)) if lock.exact_block else None
            )
            vacationable = (
                exact_config.vacation.allowed
                if exact_config is not None
                else any(
                    rotation.block_config(resident.pgy, duration).vacation.allowed
                    for duration in block_durations
                )
            )
            if vacation_overlap and not vacationable:
                raise ValueError(
                    f"lock: {resident.id} week(s) {sorted(vacation_overlap)} are vacation "
                    f"but {lock.rotation_id} is not vacationable"
                )
            if exact_config is not None:
                vacation_maximum = exact_config.vacation.max_weeks_per_block
                if vacation_maximum is not None and len(vacation_overlap) > vacation_maximum:
                    raise ValueError(
                        f"lock: {resident.id} exact {lock.rotation_id} block exceeds "
                        "its vacation limit"
                    )
            placement_rule = rotation.pgy_rule(resident.pgy)
            earliest = placement_rule.earliest_start_week
            if earliest is not None and min(lock.weeks) < earliest:
                raise ValueError(
                    f"lock: {lock.rotation_id} cannot start before its earliest "
                    f"block (week {earliest}) for {resident.id}"
                )
            for week in lock.weeks:
                if week > self.calendar.weeks:
                    raise ValueError(
                        f"lock: week {week} exceeds calendar of {self.calendar.weeks} weeks"
                    )
                key = (lock.resident_id, week)
                assignment_key = (lock.rotation_id, lock.elective)
                previous = seen.get(key)
                if previous is not None and previous != assignment_key:
                    raise ValueError(
                        f"lock: {lock.resident_id} week {week} is pinned to both "
                        f"{previous[0]!r} and {lock.rotation_id!r}"
                    )
                seen[key] = assignment_key

    def _check_manual_clinic_blocks(self, rotation_ids: set[str]) -> None:
        residents = self.residents_by_id
        consumed: dict[tuple[str, str, int], int] = {}
        occupied: dict[tuple[str, int], ManualClinicBlock] = {}

        for manual in self.manual_clinic_blocks:
            resident = residents.get(manual.resident_id)
            if resident is None:
                raise ValueError(
                    f"manual clinic block references unknown resident {manual.resident_id!r}"
                )
            if manual.rotation_id not in rotation_ids:
                raise ValueError(
                    f"manual clinic block references unknown rotation {manual.rotation_id!r}"
                )
            if manual.replaces_rotation_id not in rotation_ids:
                raise ValueError(
                    f"manual clinic block replaces unknown rotation {manual.replaces_rotation_id!r}"
                )
            if manual.rotation_id == manual.replaces_rotation_id:
                raise ValueError("manual clinic block must replace a different rotation")
            replacement_rotation = self.rotation(manual.replaces_rotation_id)
            if replacement_rotation.kind is not RotationKind.ELECTIVE:
                raise ValueError("manual Clinic blocks must replace Elective blocks")

            clinic_rotation = self.rotation(manual.rotation_id)
            if clinic_rotation.kind is not RotationKind.CLINIC:
                raise ValueError(
                    f"manual clinic block rotation {manual.rotation_id!r} is not Clinic"
                )
            try:
                clinic_config = clinic_rotation.block_config(
                    resident.pgy,
                    manual.duration_weeks,
                )
            except KeyError as exc:
                raise ValueError(
                    f"manual clinic block: {clinic_rotation.code} does not allow "
                    f"{manual.duration_weeks}-week blocks for "
                    f"{self.training_level_label(resident.pgy, compact=True)}"
                ) from exc

            if (manual.start_week - 1) % self.calendar.block_start_alignment:
                raise ValueError(
                    f"manual clinic block start week {manual.start_week} is not aligned"
                )
            end_week = manual.start_week + manual.duration_weeks - 1
            if end_week > self.calendar.weeks:
                raise ValueError(
                    f"manual clinic block ending week {end_week} exceeds calendar of "
                    f"{self.calendar.weeks} weeks"
                )

            clinic_rule = clinic_rotation.pgy_rule(resident.pgy)
            if (
                clinic_rule.earliest_start_week is not None
                and manual.start_week < clinic_rule.earliest_start_week
            ):
                raise ValueError(
                    "manual clinic block cannot start before its earliest block "
                    f"(week {clinic_rule.earliest_start_week}) for {resident.id}"
                )
            vacation_overlap = set(
                range(manual.start_week, end_week + 1)
            ) & self.resident_scheduling_vacation_weeks(resident.id)
            if vacation_overlap and not clinic_config.vacation.allowed:
                raise ValueError(
                    f"manual clinic block for {resident.id} overlaps vacation week(s) "
                    f"{sorted(vacation_overlap)}"
                )
            vacation_maximum = clinic_config.vacation.max_weeks_per_block
            if vacation_maximum is not None and len(vacation_overlap) > vacation_maximum:
                raise ValueError(
                    f"manual clinic block for {resident.id} exceeds its vacation limit"
                )

            curriculum = self.curriculum_for(resident.pgy)
            matching_requirement = next(
                (
                    block
                    for block in curriculum.blocks
                    if block.rotation_id == manual.replaces_rotation_id
                    and block.duration_weeks == manual.duration_weeks
                ),
                None,
            )
            if matching_requirement is None:
                raise ValueError(
                    "manual clinic block: "
                    f"{self.training_level_label(resident.pgy, compact=True)} has no direct "
                    f"{manual.duration_weeks}-week {manual.replaces_rotation_id!r} "
                    "requirement to replace"
                )
            consumption_key = (
                resident.id,
                manual.replaces_rotation_id,
                manual.duration_weeks,
            )
            consumed[consumption_key] = consumed.get(consumption_key, 0) + 1
            if consumed[consumption_key] > matching_requirement.count:
                raise ValueError(
                    f"manual clinic blocks replace more {manual.replaces_rotation_id!r} "
                    f"blocks than {resident.id} has"
                )

            for week in range(manual.start_week, end_week + 1):
                occupied_key = (resident.id, week)
                previous = occupied.get(occupied_key)
                if previous is not None:
                    raise ValueError(
                        f"manual clinic blocks overlap for {resident.id} in week {week}"
                    )
                occupied[occupied_key] = manual

    def _check_resident_rotation_overrides(self, rotation_ids: set[str]) -> None:
        residents = self.residents_by_id
        for override in self.resident_rotation_overrides:
            resident = residents.get(override.resident_id)
            if resident is None:
                raise ValueError(
                    "resident rotation override references unknown resident "
                    f"{override.resident_id!r}"
                )
            if override.rotation_id not in rotation_ids:
                raise ValueError(
                    "resident rotation override references unknown rotation "
                    f"{override.rotation_id!r}"
                )
            if override.replaces_rotation_id not in rotation_ids:
                raise ValueError(
                    "resident rotation override replaces unknown rotation "
                    f"{override.replaces_rotation_id!r}"
                )
            rotation = self.rotation(override.rotation_id)
            if rotation.kind is not RotationKind.STANDARD:
                raise ValueError("resident rotation overrides can only add Mandatory rotations")
            replacement = self.rotation(override.replaces_rotation_id)
            if replacement.kind is not RotationKind.ELECTIVE:
                raise ValueError(
                    "resident Mandatory rotation overrides must replace Elective blocks"
                )
            try:
                rotation.block_config(resident.pgy, override.duration_weeks)
            except KeyError as exc:
                raise ValueError(
                    f"resident rotation override: {rotation.code} does not allow "
                    f"{override.duration_weeks}-week blocks for "
                    f"{self.training_level_label(resident.pgy, compact=True)}"
                ) from exc
            if not any(
                block.rotation_id == override.replaces_rotation_id
                and block.duration_weeks == override.duration_weeks
                for block in self.curriculum_for(resident.pgy).blocks
            ):
                raise ValueError(
                    "resident rotation override: "
                    f"{self.training_level_label(resident.pgy, compact=True)} has no direct "
                    f"{override.duration_weeks}-week Elective block to replace"
                )

    def _check_resident_rotation_override_groups(self) -> None:
        """Require linked resident extras to contain one block from every group member."""
        bundles: dict[tuple[str, str], list[ResidentRotationOverride]] = {}
        for override in self.resident_rotation_overrides:
            if override.group_instance_id is not None:
                bundles.setdefault(
                    (override.resident_id, override.group_instance_id),
                    [],
                ).append(override)
        for (resident_id, instance_id), overrides in bundles.items():
            if resident_id not in self.residents_by_id:
                # The ordinary override validator reports the more direct error.
                continue
            if any(override.resident_id != resident_id for override in overrides):
                raise ValueError(
                    f"resident override group {instance_id!r} has inconsistent residents"
                )
            resident = self.residents_by_id[resident_id]
            member_ids = {override.rotation_id for override in overrides}
            if len(member_ids) != len(overrides):
                raise ValueError(f"resident override group {instance_id!r} repeats a rotation")
            matching = [
                group
                for group in self.rotation_groups
                if group.pgy == resident.pgy and member_ids <= set(group.rotation_ids)
            ]
            if len(matching) != 1 or member_ids != set(matching[0].rotation_ids):
                raise ValueError(
                    f"resident override group {instance_id!r} must contain exactly one "
                    "block from every member of one configured rotation group"
                )

    def _check_resident_replacement_inventory(self) -> None:
        residents = self.residents_by_id
        consumed: dict[tuple[str, str, int], int] = {}
        replacements = [
            (
                block.resident_id,
                block.replaces_rotation_id,
                block.duration_weeks,
            )
            for block in self.manual_clinic_blocks
        ]
        replacements.extend(
            (
                override.resident_id,
                override.replaces_rotation_id,
                override.duration_weeks,
            )
            for override in self.resident_rotation_overrides
        )
        for resident_id, rotation_id, duration_weeks in replacements:
            key = (resident_id, rotation_id, duration_weeks)
            consumed[key] = consumed.get(key, 0) + 1
        for (resident_id, rotation_id, duration_weeks), used in consumed.items():
            resident = residents[resident_id]
            available = sum(
                block.count
                for block in self.curriculum_for(resident.pgy).blocks
                if block.rotation_id == rotation_id and block.duration_weeks == duration_weeks
            )
            if used > available:
                raise ValueError(
                    f"resident overrides replace {used} {duration_weeks}-week "
                    f"{rotation_id!r} blocks for {resident_id}, but only "
                    f"{available} are available"
                )
