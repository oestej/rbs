"""What a working draft inherits from the schedule it was edited from.

An input edit can leave earlier work outside the current rules: a vacation over
a half-day, a moved academic half-day, a clinic closure. That work stays in a
working draft, hidden wherever it no longer applies, so undoing the input edit
brings it back. The next solve treats it as it treats any out-of-date schedule:
unlocked work is only a preference, and a lock it cannot honor fails that solve
and names the rule. Work an edit adds is still held to every rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from rbs.models.schedule import AssignedAttendingWork, AssignedClinic, Assignment, Schedule


def _block_key(assignment: Assignment) -> tuple:
    return (
        assignment.resident_id,
        assignment.rotation_id,
        assignment.elective,
        assignment.block_start_week or assignment.start_week,
        assignment.block_duration_weeks or len(assignment.weeks),
    )


def _session_key(assignment: Assignment, slot: AssignedClinic) -> tuple:
    return (assignment.resident_id, slot.week, slot.weekday, slot.session, slot.admin)


@dataclass(frozen=True, slots=True)
class InheritedWork:
    """Blocks, clinic sessions, and attending half-days an earlier schedule held."""

    blocks: frozenset[tuple]
    session_sites: dict[tuple, str | None]
    attending_half_days: frozenset[tuple]

    @classmethod
    def of(cls, schedule: Schedule) -> InheritedWork:
        return cls(
            blocks=frozenset(_block_key(assignment) for assignment in schedule.assignments),
            session_sites={
                _session_key(assignment, slot): slot.site
                for assignment in schedule.assignments
                for slot in assignment.clinic_slots
            },
            attending_half_days=frozenset(item.key for item in schedule.attending_work),
        )

    def holds_block(self, assignment: Assignment) -> bool:
        return _block_key(assignment) in self.blocks

    def holds_session(self, assignment: Assignment, slot: AssignedClinic) -> bool:
        """Whether this session was already there, at this site or now without one.

        Removing a clinic clears the site from its sessions. Moving a session
        to a different site by hand is new work.
        """
        key = _session_key(assignment, slot)
        return key in self.session_sites and slot.site in (None, self.session_sites[key])

    def holds_attending_work(self, item: AssignedAttendingWork) -> bool:
        return item.key in self.attending_half_days


__all__ = ["InheritedWork"]
