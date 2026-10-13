"""Second pass over model-space clusters: split regions that hold several drawings.

For every cluster the program proposes cut lines (blank strips), an automatic
judge chooses among them, and the program cuts and checks.  A cluster is replaced
by its pieces only when the judge's answers agree and every piece has content;
otherwise the cluster stays one view with a stated ``split_status``.  Judge
receipts are saved per view and can be replayed without any LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .llm_judge import Transport, judge_split, replay_judge
from .split import apply_cuts, candidate_cuts, partition_key, region_facts, region_texts, render_thumbnail
from .views import _inside, _view_id, assign_titles


def split_regions(inventory: dict[str, Any], segmented: dict[str, Any], *, work_dir: Path,
                  transport: Transport | None, allow_external: bool, replay_dir: Path | None = None) -> dict[str, Any]:
    boxes = [(ref, b) for ref, b in segmented["region_boxes"]]
    result_views: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for view in segmented["views"]:
        if view["source"] != "model_space_cluster":
            result_views.append(view)
            continue
        window = view["window"]
        members = [(ref, b) for ref, b in boxes if _inside(b, window)]
        cuts = candidate_cuts(members, window)
        record: dict[str, Any] = {"view_id": view["view_id"], "candidate_cuts": len(cuts)}
        if not cuts:
            result_views.append({**view, "split_status": "no_candidate_cuts"})
            receipts.append({**record, "status": "no_candidate_cuts"})
            continue
        view_dir = work_dir / view["view_id"].replace(":", "_")
        view_dir.mkdir(parents=True, exist_ok=True)
        thumb = view_dir / "thumb.png"
        texts = region_texts(inventory["texts"], window)
        render_thumbnail(members, window, cuts, thumb, texts)
        facts = region_facts(members, window, cuts, texts)
        (view_dir / "candidate_cuts.json").write_text(json.dumps(cuts, ensure_ascii=False, indent=1), encoding="utf-8")
        saved = replay_dir / view["view_id"].replace(":", "_") / "judge_receipt.json" if replay_dir else None
        def same_pieces(a: dict[str, Any]) -> str:
            return json.dumps([a["drawing_count"], partition_key(apply_cuts(members, window, cuts, a["cut_ids"]))])

        if saved is not None and saved.is_file():
            judged = replay_judge(saved, thumb, equivalence=same_pieces, facts=facts)
            (view_dir / "judge_receipt.json").write_text(json.dumps(judged.receipt, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            judged = judge_split(image=thumb, work_dir=view_dir, transport=transport, allow_external=allow_external,
                                 facts=facts, equivalence=same_pieces)
        record["judge_status"] = judged.status
        record["judge_receipt"] = str(view_dir / "judge_receipt.json")
        if judged.status != "agreed" or judged.answer is None:
            result_views.append({**view, "split_status": f"unsplit_judge_{judged.status}"})
            receipts.append({**record, "status": "unsplit"})
            continue
        answer = judged.answer
        if answer["drawing_count"] <= 1 or not answer["cut_ids"]:
            result_views.append({**view, "split_status": "single_drawing"})
            receipts.append({**record, "status": "single_drawing", "answer": answer})
            continue
        pieces = apply_cuts(members, window, cuts, answer["cut_ids"])
        if pieces is None or len(pieces) != answer["drawing_count"]:
            result_views.append({**view, "split_status": "unsplit_cut_rejected", "judge_answer": answer,
                                 "reason": "a piece had no content, or piece count differs from the judged drawing count"})
            receipts.append({**record, "status": "cut_rejected", "answer": answer, "pieces": None if pieces is None else len(pieces)})
            continue
        for piece in pieces:
            result_views.append({"view_id": _view_id(piece["window"]), "window": piece["window"], "source": "judged_split",
                                 "parent_view_id": view["view_id"], "member_count": piece["member_count"],
                                 "cut_ids": answer["cut_ids"], "judge_receipt": str(view_dir / "judge_receipt.json")})
        receipts.append({**record, "status": "split", "answer": answer, "pieces": len(pieces)})
    assign_titles([v for v in result_views if v["source"] == "judged_split"], inventory, segmented["main_windows"])
    return {**segmented, "views": result_views, "split_receipts": receipts}


__all__ = ["split_regions"]
