"""Frozen numeric constants of the zero-manual flow, one value for every drawing.

Each constant states why it has this value.  Changing a value requires a new entry
in ``docs/zero_manual/TUNING_LEDGER.md`` and re-running every supplied drawing, so
tuning against one drawing stays visible (the supplied drawings are therefore not
proof of generalization; only an unseen drawing run with frozen code is).
"""

from __future__ import annotations

# --- units ---------------------------------------------------------------
# At least this many dimension objects must agree (displayed text equals the
# measurement times the dimension scale) before an undeclared unit is inferred.
UNIT_MIN_AGREEING_DIMENSIONS = 3
# ...and at least this share of those values must be whole millimetres (a drawing
# in metres shows 6.25, not 6250).
UNIT_MM_INTEGER_SHARE = 0.8

# --- view segmentation ---------------------------------------------------
# Model-space objects further apart than max(fraction of drawing extent,
# factor x median object size) start a new view cluster.  Separate sheets drawn
# side by side are typically separated by far more than either.
VIEW_GAP_EXTENT_FRACTION = 0.01
VIEW_GAP_MEDIAN_SIZE_FACTOR = 20.0
# Objects larger than this share of the drawing extent (frames, borders) never
# seed or join a cluster.
VIEW_OVERSIZE_EXTENT_FRACTION = 0.5
# A sheet frame is a rectangle whose size is a standard paper size (GB/ISO A0-A4,
# millimetres) times a common drawing scale, within a relative tolerance; each
# frame is one view.  Extended paper sizes are not detected and fall back to
# gap clustering.
STANDARD_PAPER_MM = ((1189.0, 841.0), (841.0, 594.0), (594.0, 420.0), (420.0, 297.0), (297.0, 210.0))
STANDARD_DRAWING_SCALES = (1, 2, 5, 10, 20, 25, 50, 75, 100, 150, 200, 250, 300, 400, 500, 1000)
SHEET_FRAME_SIZE_TOLERANCE = 0.005
# A top-level block much larger than typical top-level objects is a container
# (a whole plan placed as one block); its children are clustered instead of it.
CONTAINER_EXPAND_MEDIAN_FACTOR = 20.0
CONTAINER_MAX_DEPTH = 3
# ...but a long block is a container only when it is big in both directions (short side also over the limit) or holds
# at least this many different kinds of block.  A long thin block holding one kind of block (a row of racking, a conveyor
# line) is equipment to be judged whole, never opened and dismissed unseen.
CONTAINER_MIN_FAMILIES = 3
# A paper viewport counts as a sheet's main view only if it shows at least this
# many top-level objects; among those, the one covering the largest model area wins.
VIEW_MAIN_MIN_CONTENT = 10
# Two views are copies when their block multisets match and relative placements
# agree within this share of the view size.
VIEW_DUPLICATE_TOLERANCE_FRACTION = 0.01

# --- region splitting (LLM chooses among program-proposed cut lines) ---
# A blank strip is a candidate cut only if it is wider than this factor times the
# median object size of the region.
SPLIT_MIN_GAP_MEDIAN_FACTOR = 2.0
# Objects spanning more than this share of the region along an axis (containers,
# borders, long chains) do not block cut lines on that axis.
SPLIT_IGNORE_SPAN_FRACTION = 0.5
# Every piece after a cut must hold at least this many top-level objects.
SPLIT_MIN_PIECE_MEMBERS = 5
# The judge is asked this many times; an answer is adopted only when at least
# SPLIT_JUDGE_MAJORITY identical answers exist.
SPLIT_JUDGE_REPEATS = 3
SPLIT_JUDGE_MAJORITY = 2

# --- candidates ----------------------------------------------------------
# Module INSERTs of one block touch each other (gap below this share of the
# module width) to belong to one adjacency group.
MODULE_EDGE_CONTACT_FRACTION = 0.02
# A label binds to a candidate only inside it, or uniquely within this factor of
# the candidate's larger side.
LABEL_BIND_SIZE_FACTOR = 1.5

# Equal blocks under the same parent form a regular array when at least this many
# sit on one line at a constant pitch (pitch may vary by this share, or 5 mm).
ARRAY_MIN_MEMBERS = 3
ARRAY_PITCH_TOLERANCE_FRACTION = 0.02
ARRAY_ALIGN_TOLERANCE_MM = 10.0
# ...and they are "packed" modules of one group (rack bays, conveyor segments) only
# when the pitch is at most this factor of the module size along the line; machines
# standing apart with aisles between them are not modules of one group.
ARRAY_MAX_PITCH_SIZE_RATIO = 2.0
# A block whose children of one kind form one regular array spanning at least ROW_SPAN_MIN of its long side and covering
# at least ROW_CELL_AREA_SHARE_MIN of its area is a row: the row is judged as one unit, those children are its cells (bays,
# positions, segments) and are never asked about alone; the count is given as rows and cells.
ROW_SPAN_MIN = 0.8
ROW_CELL_AREA_SHARE_MIN = 0.25
# Rows of one kind drawn by different blocks (a bank of shelving finished with a differently named block): the same cells
# per row and groups, pitches within ROW_KIND_PITCH_TOLERANCE of each other and depths within ROW_KIND_DEPTH_TOLERANCE are
# one kind; the block with the most copies is asked once and the others take its answer (2026-10-25).
ROW_KIND_PITCH_TOLERANCE = 0.05
ROW_KIND_DEPTH_TOLERANCE = 0.25
# Closed rectangles are cells of one group when they share a full edge (ends within
# this distance); a cell is between these side lengths (a rack bay or shelf cell,
# not a bolt hole and not a room).
CELL_TOUCH_TOLERANCE_MM = 20.0
CELL_MIN_SIDE_MM = 300.0
CELL_MAX_SIDE_MM = 6000.0
CELL_GROUP_MIN_CELLS = 3
# ...or at least this many touching cells when their size is already the cell size
# of a full group in the same view (a short run beside a column).
CELL_GROUP_MIN_CELLS_KNOWN_SIZE = 2
# An outline that holds a machine-name text but also at least this many equal
# members (blocks of one kind, or cells) is a zone, not a machine; the text then
# names the members.  The same count is required of a group of equal blocks whose
# extent holds a zone text.
ZONE_MIN_MEMBERS = 3
# Equal blocks closer than this belong to one group when looking for the group
# that holds a zone text (wider than a fork-lift aisle, narrower than a road).
ZONE_CLUSTER_GAP_MM = 4000.0
# An outermost block holding at least this many distinct block families not decided
# by a rule, nearly all drawn only once, is a pasted background drawing (a site or
# factory plan): equipment layouts repeat standard machines, backgrounds do not.  Its
# once-only families are listed as not judged instead of being sent to the LLM.
BACKGROUND_MIN_FAMILIES = 200
BACKGROUND_ONCE_ONLY_SHARE = 0.9
# Purely numeric texts (3-6 digits) on one layer, of one height, form an equipment
# number series when a view holds at least this many and nearly all are distinct
# (dimension values and window codes repeat; equipment numbers do not).
NUMBER_SERIES_MIN_MEMBERS = 10
NUMBER_SERIES_MIN_DISTINCT_SHARE = 0.9
# The smallest standard pallet is 800 mm wide; a roller conveyor narrower than this
# cannot carry pallets and is a box (tote) conveyor unless its text says pallet.
PALLET_CONVEYOR_MIN_WIDTH_MM = 850.0

# --- warehouse geometry (millimetres) ------------------------------------
WALL_THICKNESS_MIN_MM = 100.0
WALL_THICKNESS_MAX_MM = 600.0
# Door and dock openings up to this width are closed when forming the outline.
DOOR_OPENING_MAX_MM = 6000.0
# An outline must contain at least this share of the view's equipment
# candidates, so a detail drawing of a small room is not taken as the warehouse.
OUTLINE_MIN_EQUIPMENT_CONTAINMENT = 0.8
# Axis lines within this angle of one of the two dominant directions belong to it.
AXIS_DIRECTION_TOLERANCE_DEG = 2.0
# Axis lines closer than this (perpendicular distance) are one grid line.
AXIS_MERGE_MM = 50.0
# Axis lines shorter than this share of the longest axis line are ticks or stubs.
AXIS_MIN_RELATIVE_LENGTH = 0.3
# A dimension confirms an axis extent when it matches within this share.
AXIS_DIMENSION_MATCH_FRACTION = 0.005
# Wall segments shorter than this do not vote for the wall thickness.
WALL_THICKNESS_MIN_SEGMENT_MM = 500.0
# Wall-line and axis extents may differ by up to this share before the view is
# marked for review (the wall outline sits outside the axes by about a wall thickness).
WALL_AXIS_MAX_DIFFERENCE_FRACTION = 0.05
# An outline whose area is at least this share of its minimum rotated rectangle is
# reported as rectangular.
RECTANGULARITY_MIN = 0.98
# The wall outline must span at least this share of the wall linework's extent in
# both directions; otherwise the walls do not close (e.g. only columns are drawn).
WALL_OUTLINE_MIN_EXTENT_FRACTION = 0.5

# --- reconciliation ------------------------------------------------------
# Product and audit records without a shared handle match when footprints
# overlap at least this much (intersection over union).
RECONCILE_MIN_IOU = 0.5

# --- equipment recognition --------------------------------------------------
# Block families whose long side is below this are machine parts, not machines.
EQUIPMENT_MIN_SIZE_MM = 500.0
# Tiles per contact sheet sent to the type judge.
TYPE_JUDGE_TILES_PER_SHEET = 12
# Plausible (long side, short side) ranges in millimetres per type; generic
# warehouse-logistics knowledge, not measured from any supplied drawing.
TYPE_SIZE_RANGES_MM = {
    "RACK": ((500.0, 150000.0), (300.0, 20000.0)),
    "STACKER": ((1500.0, 200000.0), (500.0, 4000.0)),
    "PALLET_CONV_CHAIN": ((800.0, 20000.0), (700.0, 2500.0)),
    "PALLET_CONV_ROLLER": ((800.0, 20000.0), (700.0, 2500.0)),
    "CONVEYOR_UNKNOWN_SUBTYPE": ((400.0, 60000.0), (250.0, 2500.0)),
    "BOX_CONVEYOR": ((400.0, 60000.0), (250.0, 1500.0)),
    "BOX_LIFT": ((500.0, 5000.0), (400.0, 5000.0)),
    "ROBOT_ARM": ((500.0, 8000.0), (300.0, 8000.0)),
    "OTHER_EQUIPMENT": ((300.0, 60000.0), (200.0, 20000.0)),
}

# --- site-plan buildings (metres) --------------------------------------------
# Pieces shorter than this on building-outline layers are symbols (columns, dots),
# not walls; gaps up to BUILDING_GAP_CLOSE_M between wall ends are drafting gaps.
BUILDING_SYMBOL_MAX_M = 1.5
BUILDING_GAP_CLOSE_M = 1.0
# A closed face is a building cell (not a symbol or hatch cell) from this size on.
BUILDING_MIN_AREA_M2 = 20.0
BUILDING_MIN_SIDE_M = 2.0
# Symbol pieces closer than this belong to one symbol; symbols this close to a
# building outline are attached to it (columns sit on or just inside the walls).
SYMBOL_CLUSTER_GAP_M = 0.3
COLUMN_ON_OUTLINE_M = 1.5
# A candidate building cell must have at least this share of its boundary on
# building-outline-layer lines (title-block and legend cells have none).
CELL_BUILDING_LAYER_SHARE = 0.25

# A label inside an area or a group of identical blocks names the whole family only when the labelled places hold at least
# this share of the family's members (2026-10-10: one area label in one of 112 cells, and one machine name over 214 of 621
# modules, had named the whole family).
ZONE_LABEL_MIN_COVERAGE = 0.9

# Two views with the same title whose counted equipment (family and type) overlaps this much are one drawn set copied
# (seen 2026-10-10: a whole set drawn twice side by side); their counts are listed but must not be added up.
DUPLICATE_VIEW_MIN_OVERLAP = 0.9

# Counting by repetition (repeat_count.py): equipment drawn as loose lines or parts is found where the same linework
# repeats.  A place matches when this share of the example's length lies within the tolerance; a spacing counts only
# when seen at least REPEAT_MIN_COPIES times; members shorter than REPEAT_MIN_MEMBER_MM are details, not structure.
# Several anchors (in the middle of runs) are tried and the largest count at least two of them agree on is kept (an
# anchor near a run's end finds less; a single large count can come from a cell too generic to mean anything).  Follow-up examples are questions for a person, at most REPEAT_MAX_FOLLOW_UPS per view.
# Set 2026-10-14 from rack experiments on two drawings (both against frozen audits).
REPEAT_MATCH_TOLERANCE_MM = 10.0
REPEAT_MATCH_SHARE = 0.9
REPEAT_MIN_COPIES = 8
REPEAT_MIN_MEMBER_MM = 300.0
REPEAT_MAX_SPACING_MM = 12000.0
REPEAT_MIN_CELL_M2 = 0.3
REPEAT_ANCHORS = 8
REPEAT_MAX_FOLLOW_UPS = 3
# A group whose places mostly (this share) fall on equipment already counted from blocks is not counted twice.
REPEAT_ALREADY_COUNTED_SHARE = 0.5

# Cross-method reading (2026-10-23, acceptance target cross_method_reading_20261023): two readers by different methods,
# a reviewer AI over both, machine-readable result states.  Two readings agree when area, length and width all differ by
# at most TWO_METHOD_AGREE_FRACTION (assistant default, same as the audit tolerance, awaiting the CEO).  The program's own
# geometry for one building is measured in a window round the readers' outlines widened by PROGRAM_GEOMETRY_MARGIN_M.
TWO_METHOD_AGREE_FRACTION = 0.01
PROGRAM_GEOMETRY_MARGIN_M = 15.0
# Equipment type judge (2026-10-25, CEO: votes are a cheap alarm, not the decision): every sheet of tiles is asked TYPE_JUDGE_VOTES
# times with a different layout.  A family whose votes do not all agree, or whose answer is UNSURE, gets the closer look: an AI
# agent with the drawing tools (agent_tools block / near / compare / render_block) looks for itself and decides.  The closer look
# is also given, whatever the votes, to the kinds that decide a large part of the count: rows, and families with at least
# HIGH_COUNT_KIND_MIN copies.  At most CLOSER_LOOK_MAX_FAMILIES families per drawing (the most copies first); each look is told
# to run at most CLOSER_LOOK_MAX_TOOL_CALLS tool commands and is stopped after CLOSER_LOOK_TIMEOUT_SECONDS.
TYPE_JUDGE_VOTES = 2
HIGH_COUNT_KIND_MIN = 50
CLOSER_LOOK_MAX_FAMILIES = 12
CLOSER_LOOK_MAX_TOOL_CALLS = 30
CLOSER_LOOK_TIMEOUT_SECONDS = 1200.0
# A designated point within this distance of a reader's outline edge lies on a wall, maybe one two buildings or spans
# share: which side was meant is not stated, so two readers taking different sides is not decided by the reviewer.
DESIGNATION_ON_WALL_M = 2.0
