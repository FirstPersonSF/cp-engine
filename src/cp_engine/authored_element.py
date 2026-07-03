"""Re-export shim — the authored-element write engine lives in the shared
`spine-authoring` package (1p-component-library services/spine-authoring),
extracted from this module in arch-phase-2 (2026-07-03) to kill the hand-
mirrored copy in mc-2. Import from here as before; author changes THERE.
"""
from spine_authoring.authored_element import (  # noqa: F401
    _LAYER_CANON,
    CANONICAL_LAYERS,
    ELEMENT_TYPE_MENU,
    LAYER_ALIASES,
    authored_est_item_id,
    build_create_rows,
    build_version_rows,
    canon_layer,
    slugify,
)
