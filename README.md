# cad-to-layout-engine

从 CAD 图纸（DWG）读出**厂房的长、宽、面积**和**仓储设备（种类、数量、位置）**，生成一份左图右字的识别报告和核对用 DWG，由人复查后得到最终结果。

Read a warehouse/factory CAD drawing (DWG): the size of a named building and the warehouse equipment on it. The program
produces a picture-first report and review drawings; a person checks the few places it is unsure about and replies in
one sentence.

## 能做什么 / 做不到什么

| | 现状 |
|---|---|
| 厂房尺寸 | 指定一栋楼（楼名或图上坐标），读出外墙轮廓、长、宽、面积，红线叠在原图上。两个 AI 互不知情、用不同方法各读一遍：甲沿外墙描，乙按轴网、柱网和墙厚推；两人都要按同一份"容易错的地方"清单逐条写明看到了什么、错了会差多少。程序拿图上自己写的面积注记和端到端总尺寸核对两人的轮廓；两人不一致时，程序算出差在哪一块，交第三个 AI 放大看那一块后定夺（采用甲 / 采用乙 / 都错按程序复测 / 定不了）。每个数带状态：**与图上数字一致 / 两种方法一致 / 待定（两个数并列给出）/ 失败**，并写明"什么情况下这个数会错"。默认不需要人看；只有"待定"的那栋值得人沿红线看一眼（几分钟）。 |
| 仓储设备 | 每台设备画成独立图块的图纸（常见于设备厂商的布置图）：货架、输送线、堆垛机、提升机等多数能找到并分类。**设备由零件拼画、散线画或只画方框的图纸，目前基本读不出。** |
| 数量 | 由程序按图形直接统计，不靠 AI 数数；类型由图纸文字或 AI 看形状判断，拿不准的列为"请您确认"。 |
| 高度 | 不读。 |

所有结果标明来源：系统识别 / 已人工核对 / 人工修正。

## 需要什么

- Windows（AutoCAD 和 ODA 都只有 Windows 版）
- Python 3.11+：`pip install -r requirements.txt`
- [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)（免费，必需）：读设备时把 DWG 转成 DXF。装好后能自动找到；找不到时设环境变量 `ODA_FILE_CONVERTER` 指向 `ODAFileConverter.exe`。
- AutoCAD（可选，任意 2013 及以后版本）：用它转换和截图更稳；没有 AutoCAD 时用 ODA + 程序自绘图片。
- 一个 AI（必需，三选一），读图、定夺和判断设备类型时由程序调用，用你自己的账号；用环境变量 `CTLE_AGENT` 选：
  - `claude`（默认）：[Claude Code](https://docs.anthropic.com/en/docs/claude-code)，调用 `claude -p`。厂房尺寸、定夺、设备判断都在它上面验证过。一张图读一遍约 40 万～150 万 token，账号套餐要够量。
  - `codex`：[Codex CLI](https://github.com/openai/codex)，调用 `codex exec`。三步（厂房尺寸读图、定夺、设备判断）都在它上面各跑通过一次，记录字段与 Claude 一致；厂房尺寸那张与 Claude 的结果差 0.01%、图上尺寸对上。注意：ChatGPT 免费档的额度一张设备图跑不完（读一张厂房约 10 万 token、定夺约 8 万，设备判断几十次调用），额度用完时程序把这些调用记为无效、对应设备标"未判"并继续出包，档案里能看到报错原文。
  - `api`：程序自己调用 Anthropic API，不需要装 Claude Code 或 Codex，适合集成到你们自己的系统（见下面"集成到自己的系统"）。需要 `ANTHROPIC_API_KEY`，按用量付费。读图提示、工具、核对规则与 `claude` 完全相同，默认模型 `claude-sonnet-5-5`（与 `claude` 路用的同一档）。这条路有单元测试和真工具走查；整张图的真实 API 实跑待做。

  **图纸内容会发送到所选 AI 服务（Anthropic 或 OpenAI），请先征得图纸所有者同意。**
- 中文字体：报告和 DWG 标签用微软雅黑（中文 Windows 自带）；没有这个字体的机器上中文会显示成方块，程序不会报错。

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

- `图纸识别报告.pdf`：第一页摘要；厂房尺寸（状态、两种方法的数、差在哪、可能错在哪）；设备清单；"请您确认"（是非题）。另有 `_附录.pdf`。
- `结果.json`：机器可读的整张图结果——厂房的面积长宽、状态码、采用了哪个方法、差异、可能错在哪，设备种类数量及各自的"什么情况下会错"，每次 AI 调用的用时、token 与费用；读图失败时 `status_code` 为 `failed` 并写明原因，不给数字。
- `area/发给复查人员/to_confirm.dwg`：厂房核对用 DWG，红线是系统读出的轮廓。不对时在图层 `CTLE_人工轮廓` 用 PL 沿外墙外边线点一圈、`C` 闭合，把编号 C01 改到图层 `CTLE_确认_要`，存盘。
- `equipment/review/check.dwg`：设备核对用 DWG，每类设备一个图层（`CTLE_设备_货架` 等），需确认处画黄圈。

复查人看完用一句话回复即可，例如："面积正确。E53 正确，E63 是货架，其余正确；左下角一排输送机未识别。" 处理回复的步骤见 [docs/REVIEW_REPLY.md](docs/REVIEW_REPLY.md)。

## 用 Claude Code 或 Codex 操作

仓库里带了操作说明：Claude Code 读 `.claude/skills/read-drawing/SKILL.md`，Codex 等其他 agent 读 `AGENTS.md`（内容相同）。在仓库目录打开其中一个，说：

> 读一下这张图 C:\...\某厂房.dwg，要改造的是"1号厂房"。

它会跑上面的命令，把报告打开给你；你转述复查人的回复，它会先复述确认，再更新结果。

第一次用也可以不自己写 `cad_runtime.json`，直接告诉它你们的 CAD：

> 我们用的是 AutoCAD 2024。　/　我们没有 AutoCAD，用的是中望 CAD。

它会按说明（`.claude/skills/setup-cad/SKILL.md` / `AGENTS.md`）找到你电脑上的安装、写好配置并跑体检，告诉你用的是哪一个；有几个版本时会问你用哪个，不会自己挑。

## 集成到自己的系统（程序内直接调模型）

不想依赖 Claude Code / Codex 时，设 `CTLE_AGENT=api`，程序自己调模型 API，其余命令和产物不变：

```
set CTLE_AGENT=api
set ANTHROPIC_API_KEY=<你的 key>
py -3.11 -X utf8 -m scripts.read_drawing --dwg "<图纸.dwg>" --building "<楼名>" --out <输出文件夹>
```

- 模型：`CTLE_API_MODEL`（默认 `claude-sonnet-5-5`）。准确率只在 Claude Sonnet 上调过；换模型前请先拿几张已知结果的图对比。
- 中转：设 `ANTHROPIC_BASE_URL` 指向你们的 Anthropic 兼容中转地址。
- 思考强度：`CTLE_API_EFFORT`（默认 `xhigh`，与 Claude Code 相同）。
- 模型只能用两样工具：程序的看图量图命令（只在本次读图的工作目录里运行），和打开这些命令画出的图片；不能运行其他命令、不能读写其他文件。
- 一张图的用量：每次 AI 调用都在输出文件夹里留一份 `usage*.json`（输入、输出、缓存写、缓存读 token），汇总并按价格折算：

  ```
  py -3.11 -X utf8 -m scripts.api_usage <输出文件夹> [--prices docs/model_prices_20261021.json]
  ```

- 集成时按 `结果.json` 的状态码处理：`matches_drawing`（与图上数字一致）、`two_methods_agree`（两种方法一致）可直接用；`undecided`（待定）两个数都给了，请人沿红线看一眼；`failed` 不给数字。设备的"请您确认"清单和核对 DWG 仍然一起交出，供抽查。

## 原理（简述）

- **厂房**：两个 AI 用程序提供的看图、量图工具各自找出指定楼的外墙轮廓，互相看不到对方。甲沿外墙描：`trace` 工具先把所有可能的轮廓候选（图上自带的闭合线、按各图层描的外圈）列出来并逐个对照图上写的面积和总尺寸，甲从中选并写明对上了哪个数；乙按结构推：先读轴网首末轴和柱网，再加墙厚得外皮，没有轴网的图要如实报"方法不可用"。两人都要对照一份固定的风险清单（面积注记口径、圈进空地、相邻附属房间、轴线与外皮、铝板外包线、单位、楼层）逐条交代。面积和长宽都由程序按轮廓重新计算，并与图上的面积注记、总尺寸核对；两人差过 1% 时程序算出差异区域，第三个 AI 必须放大看那块才能定夺，"都错"只有在程序按墙线复测的数与图上数字吻合时才采用；定不了就两个数并列、状态"待定"。所有数字带"什么情况下会错"。
- **设备**：程序盘点全部图块；同一外形（含 CAD 自动生成的匿名块）合并成一种；按图纸文字定类型，文字定不了的截图交 AI 看形状，问三次、过半一致才采用，每个判断附"什么情况下会错"；出现 50 处以上的同种图块若三次没有过半，再补问两次并把先前几次的理由给它看。成排画的设备（一个图块装着一整排等距的格位，如一排货架、一条输送线）由程序先按结构认出"排"，整排截图送 AI 判断，格位不单独送判（单问一个格位"是货架还是零件"没有唯一答案），数量按"排 + 格"两级给出（如 18 排、每排 57 格，共 1,026 格）；同一种排被几个不同图块画出时（每排格数、分组、节距一样，深度相近）算一种、只问一次，另一个图块的数量、格子尺寸和离得多远写进给 AI 看的事实里，复查表和结果里按一种列出并注明各图块画了几排；又长又窄的图块不再被当作图框拆开。每种图块截图问两次；两次不一致或拿不准的，以及不论票数都要看的高影响图块（成排的、出现 50 处以上的），交给带工具的 AI 自己查图（细看）：它能列出这个图块周围有什么、已判成什么，和结构相近的图块比对，按真实线条放大截图，看够了再定，并写下看了哪些证据、什么情形下会错；它仍拿不准的就留着不计。数量由程序按出现位置统计；剖面/立面及重复绘制的视图不计。
- 不为某张具体图纸写规则；所有 AI 回答存档，可回放。

## 测试

```
py -3.11 -X utf8 -m unittest discover -s tests -p "test_*.py"
```

## License

MIT
