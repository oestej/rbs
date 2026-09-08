from collections.abc import Mapping
from functools import cached_property
from typing import Any, Self

from pydantic import BaseModel, ConfigDict


def _cached_property_names(cls: type) -> frozenset[str]:
    return frozenset(
        name
        for klass in cls.__mro__
        for name, attribute in vars(klass).items()
        if isinstance(attribute, cached_property)
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def revised(self, **updates: Any) -> Self:
        """Return a fully revalidated copy with ``updates`` applied.

        Pydantic's ``model_copy(update=...)`` intentionally skips validation.
        That is useful for internal construction, but it is unsafe at an edit
        boundary where two individually valid fields can form an invalid
        combination. Domain operations should use this helper for accepted
        edits and reserve ``model_copy`` for deliberately transient values.
        """
        draft = self.model_copy(update=updates)
        return type(self).model_validate(draft.model_dump(mode="json"))

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Copy the model without carrying stale ``cached_property`` indexes.

        Pydantic copies ``__dict__`` verbatim, so a cached lookup index built
        before an update would survive it and describe the pre-update fields.
        """
        clone = super().model_copy(update=update, deep=deep)
        for name in _cached_property_names(type(self)):
            clone.__dict__.pop(name, None)
        return clone
