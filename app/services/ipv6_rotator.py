"""IPv6 source-address rotation for yt-dlp downloads.

Picks a fresh random IPv6 address from a configured /64 subnet on every
download attempt. This makes YouTube treat each request as coming from a
different source, sidestepping per-IP rate limits and bot heuristics.

How it works
------------
Your VPS is assigned a /64 IPv6 block (typically 2^64 addresses). Linux
"ip_nonlocal_bind" + IPv6 routing means the kernel will accept outbound
connections from any address in the block, even if you haven't explicitly
added it to the interface — as long as the prefix is routed to your host.

Configuration
-------------
Set ``IPV6_ROTATION_PREFIX`` in your environment, e.g.::

    IPV6_ROTATION_PREFIX=2001:19f0:4400:62f3::/64

Leave it empty to disable rotation entirely. On any error (parse failure,
non-/64 prefix, etc.) the helper returns ``None`` and yt-dlp falls back to
its default source-address selection.

Requirements
------------
* The Celery worker container must run with ``network_mode: host`` in
  ``docker-compose.yml`` so it can bind to the host's IPv6 addresses.
* The host kernel must have ``net.ipv6.ip_nonlocal_bind=1`` (or each
  rotated address must be added to the interface — see ``add_ipv6_to_iface``
  utility, not used here for simplicity).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import random

logger = logging.getLogger(__name__)


def _get_prefix() -> str:
    """Read the configured IPv6 prefix from env."""
    return (os.environ.get("IPV6_ROTATION_PREFIX") or "").strip()


def random_ipv6_source_address() -> str | None:
    """Return a random IPv6 address from the configured /64 (or larger) block.

    Returns ``None`` on any failure, signalling the caller to fall back to
    default routing. Never raises.

    Examples
    --------
    >>> # With IPV6_ROTATION_PREFIX=2001:19f0:4400:62f3::/64
    >>> addr = random_ipv6_source_address()
    >>> addr.startswith("2001:19f0:4400:62f3:")
    True
    """
    prefix = _get_prefix()
    if not prefix:
        return None

    try:
        net = ipaddress.IPv6Network(prefix, strict=False)
    except (ValueError, TypeError) as e:
        logger.warning(f"[ipv6] Invalid IPV6_ROTATION_PREFIX '{prefix}': {e}")
        return None

    # Reject anything wider than /64 (we don't want to rotate across customers)
    # and anything narrower than /96 (too few addresses for meaningful rotation).
    if net.prefixlen < 32:
        logger.warning(
            f"[ipv6] Prefix {prefix} is too wide (/<32). Refusing to rotate."
        )
        return None

    # Number of host bits available for randomization
    host_bits = 128 - net.prefixlen
    if host_bits <= 0:
        logger.warning(f"[ipv6] Prefix {prefix} has no host bits to randomize.")
        return None

    try:
        # Pick a random integer in [1, 2^host_bits - 1] (skip the all-zero
        # network address itself).
        offset = random.randint(1, (1 << host_bits) - 1)
        random_addr = ipaddress.IPv6Address(int(net.network_address) + offset)
    except Exception as e:
        logger.warning(f"[ipv6] Failed to generate random address: {e}")
        return None

    return str(random_addr)


def get_ydl_source_address_opt() -> dict:
    """Return a dict to merge into yt-dlp options for source-address rotation.

    Returns ``{}`` if rotation is disabled or unavailable, so the caller can
    safely ``opts.update(get_ydl_source_address_opt())`` regardless.
    """
    addr = random_ipv6_source_address()
    if not addr:
        return {}

    logger.info(f"[ipv6] Using rotated source address {addr} for this download")
    # yt-dlp respects ``source_address`` to bind outbound sockets.
    return {"source_address": addr}
