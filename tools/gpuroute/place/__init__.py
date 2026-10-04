"""Placement search with trial GPU routing in the loop.

geom      — local pad/courtyard geometry of footprints (one-off export from pcbnew)
sdf       — analytic distance-to-copper maps on the GPU (instead of copper_mask + EDT)
cmodel    — candidate model exposing the gpuroute.model.Model interface
wavebatch — batched waves (reachability, routing) over many windows/chains
constraints, escape, fitness, search, apply_place, place_campaign
"""
