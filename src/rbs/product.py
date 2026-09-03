"""Product-wide configuration shared by desktop, cloud, and UI components."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProductTarget(StrEnum):
    """The packaging in which the shared RBS product is running."""

    LOCAL = "local"
    CLOUD = "cloud"


@dataclass(frozen=True, slots=True)
class ProductConfig:
    """Stable product decisions which cascade from a host into its UI sessions."""

    target: ProductTarget

    @property
    def bundles_third_party_licenses(self) -> bool:
        """Whether this packaging ships dependency notices for in-app display."""
        return self.target is ProductTarget.LOCAL


LOCAL_PRODUCT = ProductConfig(ProductTarget.LOCAL)
CLOUD_PRODUCT = ProductConfig(ProductTarget.CLOUD)
