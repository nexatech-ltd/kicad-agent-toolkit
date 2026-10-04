"""Footprint geometry helpers shared by the placement tools (pcbnew, millimetres).

Boxes are (x0, y0, x1, y1) in board coordinates.

    bbox_mm(fp, grow)   courtyard box of a footprint, else its body box without texts
    overlap(a, b)       do two boxes intersect (touching edges do not count)
    inside_board(bb)    is a box inside the board outline minus EDGE_MARGIN
    rule_boxes(board)   bounding boxes of all rule areas (keepouts)

The outline is taken from the Edge.Cuts bounding box of the board the measured
footprints belong to, so no board extent is hard-coded.
"""
import pcbnew

MM = 1e6
EDGE_MARGIN = 1.0   # mm between a courtyard and the board edge

_outline = {}


def _remember(board):
    if board is None or 'box' in _outline:
        return
    bb = board.GetBoardEdgesBoundingBox()
    if bb.GetWidth() > 0:
        _outline['box'] = (bb.GetX() / MM, bb.GetY() / MM,
                           (bb.GetX() + bb.GetWidth()) / MM, (bb.GetY() + bb.GetHeight()) / MM)


def set_board(board):
    """Use this board's outline for inside_board() (normally learned from bbox_mm)."""
    _outline.pop('box', None)
    _remember(board)


def bbox_mm(fp, grow=0.0):
    """Footprint extent in mm: the courtyard (board coordinates), else the body box without texts.

    The plain bounding box includes footprint zones; a zone inserted as text keeps the
    coordinates it had in the library, so it can sit at the board origin and inflate the
    box across half the board. The courtyard is not affected.
    """
    _remember(fp.GetBoard())
    b = None
    try:
        cy = fp.GetCourtyard(pcbnew.F_CrtYd if not fp.IsFlipped() else pcbnew.B_CrtYd)
        if cy.OutlineCount() > 0:
            b = cy.BBox()
    except Exception:
        b = None
    if b is None or b.GetWidth() == 0:
        b = fp.GetBoundingBox(False, False)
    return (b.GetX() / MM - grow, b.GetY() / MM - grow,
            (b.GetX() + b.GetWidth()) / MM + grow, (b.GetY() + b.GetHeight()) / MM + grow)


def overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def inside_board(bb):
    if 'box' not in _outline:
        raise RuntimeError('board outline unknown: call set_board(board) or bbox_mm() first')
    o = _outline['box']
    return (bb[0] >= o[0] + EDGE_MARGIN and bb[1] >= o[1] + EDGE_MARGIN
            and bb[2] <= o[2] - EDGE_MARGIN and bb[3] <= o[3] - EDGE_MARGIN)


def rule_boxes(b):
    _remember(b)
    out = []
    for z in b.Zones():
        if z.GetIsRuleArea():
            zb = z.GetBoundingBox()
            out.append((zb.GetX() / MM, zb.GetY() / MM,
                        (zb.GetX() + zb.GetWidth()) / MM, (zb.GetY() + zb.GetHeight()) / MM))
    return out
