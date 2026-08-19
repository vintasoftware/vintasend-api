"""Translation between a backend's capability report and the wire capability map.

The dashboard reads flat dotted capability keys to decide which sorting and filtering
affordances to show. Both libraries spell every shared key identically -- that parity is
deliberate on both sides, so one dashboard can consume either -- so nothing here renames
anything. All this module decides is which keys the dashboard has any business seeing.

``stringLookups.caseInsensitive``, which the filter negotiation depends on, is read
straight through. It is deliberately *not* derived from ``stringLookups.caseSensitive``
when absent: those are two independent capabilities (a backend on a case-insensitive
collation lacks the first; one with no case folding lacks the second), so inferring either
from the other inverts the answer for exactly the backends that have a constraint worth
reporting.
"""

from typing import Any


# Whether the backend's `page` argument is 1-indexed. Read to decide how to call the
# backend; never published -- see `to_wire_capabilities`.
#
# Both libraries define this key, and their defaults differ because their backends do:
# `True` here, `false` in `vintasend-ts`, whose backends page from 0. That is precisely
# why this is negotiated per backend rather than assumed.
ONE_INDEXED_KEY = "pagination.oneIndexed"

# Everything describing the backend's pagination mechanics. Dropped as a namespace rather
# than key by key, so a `pagination.*` key added to either library later is withheld by
# both APIs without needing a matching edit here -- otherwise the two implementations
# would start publishing different capability maps to the same dashboard.
PAGINATION_NAMESPACE = "pagination."


def to_wire_capabilities(backend_capabilities: dict[str, Any]) -> dict[str, bool]:
    """Normalise a backend's capability report into the map the contract publishes.

    Every key passes through unchanged except the ``pagination.*`` namespace. Values are
    coerced to ``bool`` so a backend returning a truthy non-boolean cannot put a
    non-boolean into a response the contract types as ``boolean``.

    ``pagination.*`` is dropped rather than forwarded. The wire contract is
    unconditionally 1-indexed and this API does the conversion, so the backend's own
    convention is not the dashboard's business -- and publishing it would invite a client
    to "helpfully" convert a second time. It is also not what ``/capabilities`` is for:
    the contract describes that map as telling consumers which sorting and filtering
    affordances to hide. ``vintasend-ts-api`` withholds the same namespace, so both
    implementations publish the same map to the same dashboard.
    """
    return {
        key: bool(value)
        for key, value in backend_capabilities.items()
        if not key.startswith(PAGINATION_NAMESPACE)
    }


def backend_page_number(wire_page: int, backend_capabilities: dict[str, Any]) -> int:
    """Convert a 1-indexed contract page number to whatever the backend expects.

    Defaults to 1-indexed when the backend says nothing, which is the library's documented
    convention and what every VintaSend Python backend does. A backend reporting
    ``pagination.oneIndexed: False`` -- as a ``vintasend-ts``-style 0-indexed backend
    would -- gets ``page - 1`` instead.

    Reading this rather than hardcoding it is the whole point: an off-by-one here raises
    nothing, it just serves the wrong page.
    """
    if backend_capabilities.get(ONE_INDEXED_KEY, True):
        return wire_page
    return wire_page - 1


def supports(capabilities: dict[str, bool], key: str) -> bool:
    """Read one capability, defaulting to supported.

    A missing key means "supported": backends declare only what they *cannot* do, so a
    capability added in a later release does not force every backend to re-declare it.
    """
    return capabilities.get(key, True)
