You classify block definitions from a warehouse-logistics CAD drawing (plan view, top-down). Each numbered tile has TWO pictures. The upper picture shows ONE block definition drawn at its own scale. The lower picture shows where that block sits in the drawing: every occurrence of this block is a red rectangle, other objects around it are grey rectangles. Facts are printed for each tile: its real size (long side x short side, millimetres), how many times it occurs, in which views, the blocks it is placed inside (parents), the blocks it contains (children), and texts found inside or near it.

Answer for every tile with exactly one of these types:

- RACK (货架): storage racking seen from above. Long rows of repeated rectangular bays or modules; uprights as small squares at bay corners; beams as parallel lines. A single module is typically 1-3 m by about 0.8-1.5 m; a whole row can be tens of metres long.
- STACKER (堆垛机): stacker crane of an automated high-bay store. Either the crane body (a compact machine roughly 2-6 m by 1-3 m, often with a mast and fork outline) or its rail/aisle travel path: a long narrow strip, tens of metres long and 1-3 m wide, running between two rack rows.
- PALLET_CONV_CHAIN (托盘输送机，链条): pallet conveyor module about 1-4 m long and 1.0-1.6 m wide showing two or three chain strands running along its length.
- PALLET_CONV_ROLLER (托盘输送机，滚筒): pallet conveyor module of the same size showing many closely spaced rollers drawn across its width.
- CONVEYOR_UNKNOWN_SUBTYPE (输送机，子类未识别): clearly a conveyor, but chain versus roller, or pallet versus tote, cannot be told from the drawing.
- BOX_CONVEYOR (箱式输送机): tote/carton conveyor, narrow (about 0.4-1.0 m wide), often long straight or curved runs.
- BOX_LIFT (箱式提升机): vertical tote lift seen from above: a compact, roughly square machine about 1-3 m, usually where conveyor lines meet or change level.
- ROBOT_ARM (机械臂): industrial robot: a round base with an arm, often drawn with its circular reach envelope, roughly 1-4 m.
- OTHER_EQUIPMENT: a logistics machine that is none of the above (for example lift-and-transfer unit, shuttle, AGV, forklift, freight elevator, workstation). Put its common name in "name".
- COMPONENT: a part of a machine or rack, not a whole machine (roller, chain strand, leg, sensor, bolt, beam, upright, guard rail, motor, small symbol inside another block).
- NOT_EQUIPMENT: building or drawing content (wall, column, axis grid, door, stair, dimension, text box, title block, table, legend, area outline, hatch, pallet or load outline, container block holding a whole plan).
- UNSURE: you cannot tell.

Rules:
1. Judge from the drawn shape, the surroundings picture AND the printed facts together. How a block is used matters as much as its own shape: many identical red rectangles packed side by side in long straight rows are storage bays or modules of racking (RACK), even when a single one is only a plain rectangle; a few red rectangles placed end to end along a line are conveyor segments; a single red rectangle with many different grey objects inside it is a container, not a machine. Size is decisive: a 200 mm object is not a conveyor; a 40 m strip is not a lift.
2. If a block is placed inside a bigger block that is itself a machine, it is usually a COMPONENT.
3. A block that contains many different machines, or a whole area, is NOT_EQUIPMENT (a container), not a machine.
4. Block names in drawings are often meaningless codes; do not rely on them unless they are real words.
5. Prefer UNSURE to a guess. A wrong type is worse than UNSURE.
6. Do not use tools, files or directories. Answer only from the image and the facts.
7. For every tile also write "wrong_if": the one situation in which your answer would be wrong (for example "wrong if the rows are pallet positions painted on the floor, not racking"). If a tile's facts include "earlier judgements", those are answers given before by judges who saw the same tile; weigh their reasons, do not simply copy the majority, and say in "reason" why you agree or differ.

Answer with JSON only, one object per tile:
{"tiles": [{"id": "<tile id>", "type": "<TYPE>", "name": "<only for OTHER_EQUIPMENT>", "reason": "<short>", "wrong_if": "<one situation in which this answer is wrong>"}]}
