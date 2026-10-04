"""GPU router: torch wavefront expansion over layers + PathFinder negotiation.

The board model comes from tools/raster.py and maze_route.py (same clearances, rules and
layers); this package only does the path search on the GPU and the negotiation between
nets competing for the same space.
"""
