"""Walk every drawing object in world coordinates, including objects nested in blocks.

Each yielded item keeps the original (block-local) entity, the matrix that places
it in model space, a handle path that identifies this exact occurrence (evidence
cites it), and the effective layer (an entity on layer "0" inside a block takes
the layer of the INSERT that places it, as CAD displays it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.math import Matrix44

MAX_DEPTH = 20


@dataclass(frozen=True)
class WorldItem:
    entity: Any
    matrix: Matrix44
    ref: str
    depth: int
    layer: str
    block_path: tuple[str, ...]
    parent_ref: str | None

    @property
    def dxftype(self) -> str:
        return self.entity.dxftype()


def handle_of(entity: Any) -> str:
    return str(getattr(entity.dxf, "handle", "") or "").upper()


def effective_layer(entity: Any, parent_layer: str) -> str:
    layer = str(getattr(entity.dxf, "layer", "0") or "0")
    return parent_layer if layer == "0" and parent_layer else layer


def dynamic_original_name(doc: Any, insert: Any) -> str | None:
    """Authoring name of an anonymous dynamic-block reference (``*U…``)."""

    try:
        if not bool(getattr(insert, "has_extension_dict", False)):
            return None
        representation = insert.get_extension_dict().get("AcDbBlockRepresentation")
        if representation is None or representation.dxftype() != "DICTIONARY":
            return None
        data = representation.get("AcDbRepData")
        if data is None:
            return None
        collector = TagCollector(dxfversion=doc.dxfversion)
        data.export_dxf(collector)
        handle = next((str(tag.value) for tag in collector.tags if tag.code == 340), None)
        record = doc.entitydb.get(handle) if handle else None
        if record is not None and record.dxftype() == "BLOCK_RECORD":
            return str(getattr(record.dxf, "name", "") or "").strip() or None
    except Exception:  # malformed extension data never stops a read
        return None
    return None


def iter_world_items(doc: Any, *, layout: Any = None, max_depth: int = MAX_DEPTH) -> Iterator[WorldItem]:
    """Depth-first walk of a layout (model space by default).

    INSERTs are yielded themselves (with the matrix of their parent) and then
    expanded; their ATTRIBs are yielded as children placed by the parent matrix
    (ATTRIB coordinates are already in the parent's space).
    """

    space = layout if layout is not None else doc.modelspace()
    identity = Matrix44()

    def walk(entities: Any, parent: Matrix44, parent_ref: str | None, parent_layer: str,
             block_path: tuple[str, ...], depth: int, stack: frozenset[str]) -> Iterator[WorldItem]:
        for ordinal, entity in enumerate(entities, start=1):
            token = handle_of(entity) or f"#{ordinal}"
            ref = f"{parent_ref}/{token}" if parent_ref else token
            layer = effective_layer(entity, parent_layer)
            yield WorldItem(entity, parent, ref, depth, layer, block_path, parent_ref)
            if entity.dxftype() != "INSERT":
                continue
            for index, attrib in enumerate(getattr(entity, "attribs", []) or [], start=1):
                attrib_ref = f"{ref}/{handle_of(attrib) or f'@{index}'}"
                yield WorldItem(attrib, parent, attrib_ref, depth + 1, effective_layer(attrib, layer), block_path, ref)
            name = str(entity.dxf.name)
            if depth >= max_depth or name in stack:
                continue
            block = doc.blocks.get(name)
            if block is None:
                continue
            try:
                child_matrix = entity.matrix44() @ parent
            except Exception:
                continue
            yield from walk(block, child_matrix, ref, layer, (*block_path, name), depth + 1, stack | {name})

    yield from walk(space, identity, None, "", (), 0, frozenset())


def transformed_point(matrix: Matrix44, point: Any) -> tuple[float, float]:
    vector = matrix.transform((float(point[0]), float(point[1]), float(point[2]) if len(point) > 2 else 0.0))
    return float(vector.x), float(vector.y)


def matrix_2d(matrix: Matrix44) -> list[float]:
    """Affine [a, b, c, d, e, f]: x' = a*x + c*y + e, y' = b*x + d*y + f."""

    origin = matrix.transform((0.0, 0.0, 0.0))
    x_axis = matrix.transform((1.0, 0.0, 0.0))
    y_axis = matrix.transform((0.0, 1.0, 0.0))
    return [
        round(float(x_axis.x - origin.x), 12), round(float(x_axis.y - origin.y), 12),
        round(float(y_axis.x - origin.x), 12), round(float(y_axis.y - origin.y), 12),
        round(float(origin.x), 6), round(float(origin.y), 6),
    ]


__all__ = [
    "MAX_DEPTH",
    "WorldItem",
    "dynamic_original_name",
    "effective_layer",
    "handle_of",
    "iter_world_items",
    "matrix_2d",
    "transformed_point",
]
