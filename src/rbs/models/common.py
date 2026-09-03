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
