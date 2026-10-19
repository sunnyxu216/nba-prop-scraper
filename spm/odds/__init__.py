"""Odds sources behind one interface (see base.py).

    from spm import odds
    for source in odds.enabled_sources():      # ODDS_SOURCES=action_network,the_odds_api
        offers = source.fetch("nba", days=2)   # -> list[odds.Offer]

Adding a source = one new module with an @register'ed OddsSource subclass,
imported below. Nothing else in the app changes.
"""

from .base import MARKETS, OddsSource, Offer, enabled_sources, licensed_sources, register, registered  # noqa: F401
from . import action_network, the_odds_api  # noqa: F401,E402  (registers the adapters)
