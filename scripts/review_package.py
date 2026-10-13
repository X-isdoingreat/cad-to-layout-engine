"""The review package for a decision maker: per drawing its 图纸识别报告, the appendix and the review drawings; one page on how to use it.

  py -3.11 -X utf8 -m scripts.review_package --out output/review_<date> --spec <json>

spec: [{"folder": "1_drawing_a", "drawing": "...dwg", "report": "<pdf>", "area": "<trial_package out dir or null>",
        "equipment": "<cli read run dir or null>", "note": "..."}]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.equipment_review import _matplotlib  # noqa: E402
from scripts.trial_package import wrap_keep  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    from matplotlib.backends.backend_pdf import PdfPages

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rows = []
    for entry in json.loads(Path(args.spec).read_text(encoding="utf-8")):
        folder = out / entry["folder"]
        folder.mkdir()
        report = Path(entry["report"])
        shutil.copyfile(report, folder / "图纸识别报告.pdf")
        appendix = report.with_name(report.stem + "_附录.pdf")
        if appendix.is_file():
            shutil.copyfile(appendix, folder / "附录_设备样式明细.pdf")
        if entry.get("area") and (Path(entry["area"]) / "issue" / "to_confirm.dwg").is_file():
            shutil.copyfile(Path(entry["area"]) / "issue" / "to_confirm.dwg", folder / "核对用_厂房.dwg")
        if entry.get("equipment") and (Path(entry["equipment"]) / "review" / "check.dwg").is_file():
            shutil.copyfile(Path(entry["equipment"]) / "review" / "check.dwg", folder / "核对用_设备.dwg")
        rows.append((entry["folder"], entry.get("note", ""), sorted(p.name for p in folder.iterdir())))
    pyplot = _matplotlib()
    with PdfPages(out / "使用说明.pdf") as pdf:
        figure = pyplot.figure(figsize=(8.27, 11.69))
        figure.text(0.07, 0.94, "图纸识别 · 试用说明", fontsize=20, weight="bold")
        y = 0.89
        blocks = [
            ("这是什么", ["系统读取客户 CAD 图纸，给出要改造厂房的长、宽、面积，以及图上的仓储设备（种类、数量、位置）。",
                       "每张图一个文件夹。先看「图纸识别报告.pdf」：第一页是摘要，后面每页左图右字。"]),
            ("怎么核对", ["报告第三部分列出了系统拿不准、需要您确认的几处，每处有编号（如 E53）。",
                       "只看 PDF 就能回答大部分问题；需要放大细看时，打开同一文件夹里的「核对用_厂房.dwg」或「核对用_设备.dwg」：设备按种类分图层上色，需确认处画了黄圈。"]),
            ("怎么回复", ["直接用一句话回复即可，不用填表。例如：",
                       "“面积正确。E53 正确，E63 是货架，其余正确；左下角一排输送机未识别。”",
                       "我们收到后先复述一遍您的意思，确认无误后更新结果，每个数字注明是系统识别还是人工核对。也请告诉我们看了几分钟。"]),
            ("各文件夹", [f"{folder}：{note}" if note else folder for folder, note, _ in rows]),
            ("说明", ["以上结果均为系统识别，尚未经人工核对。厂房面积按外墙外边线围成的占地面积计，多层只算指定楼层；净高需客户提供。",
                    "识别时图纸内容会发送至 Anthropic 的 AI 服务处理，请事先征得图纸所有者同意；原图从不被修改。"]),
        ]
        for title, lines in blocks:
            figure.text(0.07, y, title, fontsize=13, weight="bold")
            y -= 0.03
            for line in lines:
                for k, piece in enumerate(wrap_keep(line, 40)):
                    figure.text(0.09 if k == 0 else 0.1, y, piece, fontsize=11)
                    y -= 0.024
                y -= 0.006
            y -= 0.018
        pdf.savefig(figure)
        pyplot.close(figure)
    print(json.dumps({"out": str(out), "folders": rows}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
