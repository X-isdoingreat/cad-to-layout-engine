# cad-to-layout-engine

从 CAD 图纸（DWG）读出**厂房的长、宽、面积**和**仓储设备（种类、数量、位置）**，生成一份左图右字的识别报告和核对用 DWG，由人复查后得到最终结果。

Read a warehouse/factory CAD drawing (DWG): the size of a named building and the warehouse equipment on it. The program
produces a picture-first report and review drawings; a person checks the few places it is unsure about and replies in
one sentence.

## 能做什么 / 做不到什么

| | 现状 |
|---|---|
| 厂房尺寸 | 指定一栋楼（楼名或图上坐标），读出外墙轮廓、长、宽、面积，红线叠在原图上。墙线清楚的楼通常一次读对；墙线和道路、设备线混在一起时常有 10% 左右偏差，**需要人沿红线核对或重描**（几分钟）。 |
| 仓储设备 | 每台设备画成独立图块的图纸（常见于设备厂商的布置图）：货架、输送线、堆垛机、提升机等多数能找到并分类。**设备由零件拼画、散线画或只画方框的图纸，目前基本读不出。** |
| 数量 | 由程序按图形直接统计，不靠 AI 数数；类型由图纸文字或 AI 看形状判断，拿不准的列为"请您确认"。 |
| 高度 | 不读。 |

所有结果标明来源：系统识别 / 已人工核对 / 人工修正。

## 需要什么

- Windows（AutoCAD 和 ODA 都只有 Windows 版）
- Python 3.11+：`pip install -r requirements.txt`
- [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)（免费，必需）：读设备时把 DWG 转成 DXF。装好后能自动找到；找不到时设环境变量 `ODA_FILE_CONVERTER` 指向 `ODAFileConverter.exe`。
- AutoCAD（可选，任意 2013 及以后版本）：用它转换和截图更稳；没有 AutoCAD 时用 ODA + 程序自绘图片。
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（必需）：读图和判断设备类型时调用 `claude -p`，使用你自己的账号。**图纸内容会发送到 Anthropic 的服务，请先征得图纸所有者同意。**（也可以设 `CTLE_AGENT=codex` 改用 Codex CLI。）

## 安装（每台电脑一次）

```
git clone <this repo>
cd cad-to-layout-engine
pip install -r requirements.txt
```

写一个 `cad_runtime.json`（不进版本库），二选一：

```json
{"backend": "autocad", "accoreconsole": "C:/Program Files/Autodesk/AutoCAD 2024/accoreconsole.exe"}
```
```json
{"backend": "oda"}
```

程序**不会自动挑选 AutoCAD 版本**：没有这个文件会直接报错，并列出它找到的 AutoCAD。然后跑体检：

```
py -3.11 -X utf8 -m cad_to_layout.cad_runtime doctor
```

它会列出找到的 AutoCAD 和 ODA，并真的做一次 DXF→DWG→DXF→截图；`"status": "pass"` 就可以用了。

## 用法

一条命令读完一张图：

```
py -3.11 -X utf8 -m scripts.read_drawing --dwg "<图纸.dwg>" --building "<楼名，或：图纸坐标 (x, y) 处的那栋>" --out <输出文件夹>
```

只要设备：加 `--no-area`（不需要 `--building`）；只要厂房：加 `--no-equipment`。

输出文件夹里：

- `图纸识别报告.pdf`：第一页摘要；厂房尺寸；设备清单；"请您确认"（是非题）。另有 `_附录.pdf`。
- `area/发给复查人员/to_confirm.dwg`：厂房核对用 DWG，红线是系统读出的轮廓。不对时在图层 `CTLE_人工轮廓` 用 PL 沿外墙外边线点一圈、`C` 闭合，把编号 C01 改到图层 `CTLE_确认_要`，存盘。
- `equipment/review/check.dwg`：设备核对用 DWG，每类设备一个图层（`CTLE_设备_货架` 等），需确认处画黄圈。

复查人看完用一句话回复即可，例如："面积正确。E53 正确，E63 是货架，其余正确；左下角一排输送机未识别。" 处理回复的步骤见 [docs/REVIEW_REPLY.md](docs/REVIEW_REPLY.md)。

## 用 Claude Code 操作

仓库里带了一个 skill（`.claude/skills/read-drawing/SKILL.md`）。在仓库目录打开 Claude Code，说：

> 读一下这张图 C:\...\某厂房.dwg，要改造的是"1号厂房"。

它会跑上面的命令，把报告打开给你；你转述复查人的回复，它会先复述确认，再更新结果。

## 原理（简述）

- **厂房**：AI（Claude Code）用程序提供的看图、量图工具找出指定楼的外墙轮廓；面积和长宽由程序按轮廓重新计算；程序检查尺寸标注、相邻块等，把没把握的地方编号交给人。
- **设备**：程序盘点全部图块；同一外形（含 CAD 自动生成的匿名块）合并成一种；按图纸文字定类型，文字定不了的截图交 AI 看形状，问三次、两次一致才采用；数量由程序按出现位置统计；剖面/立面及重复绘制的视图不计。
- 不为某张具体图纸写规则；所有 AI 回答存档，可回放。

## 测试

```
py -3.11 -X utf8 -m unittest discover -s tests -p "test_*.py"
```

## License

MIT
