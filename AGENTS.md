# AGENTS.md — for Codex and other coding agents

Same job as `.claude/skills/read-drawing/SKILL.md`: read a CAD drawing (DWG) for the size of a named building and its
warehouse equipment, open the report, and apply a reviewer's one-sentence reply. The user gives a `.dwg` and asks to
read / measure / identify equipment, or relays a reviewer's answer to a report.

This is an unattended command-line product, not a codebase to modify: do not edit code or prompts to make a drawing
read "work"; report what the command said instead.

## 0. Check the machine once

Run `py -3.11 -X utf8 -m cad_to_layout.cad_runtime doctor`. If it is not `"status": "pass"`, show the user the message
(usually: create `cad_runtime.json` with their AutoCAD path or `{"backend": "oda"}`, or install ODA File Converter).
Never pick an AutoCAD version for them; ask which installation to use if several are listed.
If the user says which CAD they have ("我们用的是 AutoCAD 2024", "没有 AutoCAD"), follow "Set up the CAD runtime" at the end of this file first.

The AI that reads the drawing inside the command is chosen by the environment variable `CTLE_AGENT`:
`claude` (default; needs Claude Code logged in), `codex` (needs Codex CLI logged in) or `api` (the program calls the
Anthropic API itself; needs `ANTHROPIC_API_KEY`, optional `CTLE_API_MODEL`). See README "需要什么" for what each has
been verified to do. Set it once in the shell before running, e.g. `set CTLE_AGENT=codex` (cmd) or
`$env:CTLE_AGENT="codex"` (PowerShell).

## 1. What to read

You need: the DWG path, and which building to measure (its name as written on the drawing, or a position such as
"图纸坐标 (x, y) 处的那栋"). If the user did not say which building and wants the size, ask; never choose one.
Equipment only → `--no-area`. Building only → `--no-equipment`.

Tell the user once that the drawing content is sent to the AI service chosen above and that they should have the
owner's consent.

## 2. Run

```
py -3.11 -X utf8 -m scripts.read_drawing --dwg "<path>" --building "<which building>" --out "<folder>"
```

It can take from a few minutes (small drawings) to about an hour (large ones). Run it in the background and tell the
user where it is (building done / equipment judging / report) when they ask. The drawing itself is never modified.

## 3. Hand over

Read `<folder>/结果.json` first (`status_code`: `ok` / `failed` / `equipment_llm_failed`; `area.building.status_code`:
`matches_drawing` 与图上数字一致 / `two_methods_agree` 两种方法一致 / `undecided` 待定 / `failed`), then open
`<folder>/图纸识别报告.pdf`. Summarise in product language, in this order:
- building: name, area, length × width, its status in words, and the `wrong_if` sentence (what would make the number
  wrong); for `undecided` give both numbers with their methods and say the red line is worth a look; for `failed` give
  the reason and no number;
- equipment: the main types and counts on the plan shown;
- how many places need confirmation (part 三) and how to reply (one sentence with the numbers);
- what was not read (e.g. equipment drawn as loose lines or parts, other floors, height).
Never call a number correct beyond its status: 与图上数字一致 and 两种方法一致 are system findings, 待定 is two numbers, not one.

## 4. The reviewer's reply

Follow `docs/REVIEW_REPLY.md`: map the reply to the numbers shown, repeat it back as a small table and wait for "对",
then run the building finish (`scripts.trial_package finish ... --passed` or `--returned-dwg`) and the equipment apply
(`scripts/equipment_review.py apply ...`), save the reply verbatim in `review/reply.txt`, and report what is now
reviewed, corrected or still open.


# Set up the CAD runtime (same as `.claude/skills/setup-cad/SKILL.md`)

Use when the user says what CAD they have ("我们用的是 AutoCAD 2024", "我们是中望 CAD", "没有 AutoCAD"), asks to configure or check the machine, or the doctor reports `not_configured` / `fail`.

The program converts DWG ↔ DXF and draws the review pictures through one configured CAD runtime, written in
`cad_runtime.json` at the repository root (per machine, never committed). Two backends exist:

| the user has | backend | needs |
|---|---|---|
| AutoCAD or AutoCAD LT, 2013 or later, any year | `autocad` | the path to that installation's `accoreconsole.exe` |
| no AutoCAD, or another CAD (中望 ZWCAD, 浩辰 GstarCAD, BricsCAD, …) | `oda` | the free ODA File Converter installed |

Another CAD cannot drive the program, but it can open everything the program produces (DWG 2018 format).
Only AutoCAD 2025 has been exercised on a real drawing; other years follow the same commands and are confirmed by
the doctor's round trip, not by experience.

## 1. Find what is installed

Run `py -3.11 -X utf8 -m cad_to_layout.cad_runtime doctor`. Its JSON lists `autocad_installations` (every
`C:/Program Files/Autodesk/AutoCAD[ LT] 20xx/accoreconsole.exe` found) and `oda_file_converter` (a path, or null).
- `"status": "pass"` already: tell the user the `runtime` line and stop, unless they asked to change it.
- `not_configured` or `fail`: go on.

## 2. Choose from the user's words, never by yourself

- They named a year ("AutoCAD 2024"): use the listed installation of that year. If that year is not listed, show
  the list and ask which one to use or where their AutoCAD is; never take another year silently.
- They said AutoCAD without a year: exactly one listed → use it and say which; several → ask.
- They said they have no AutoCAD, or named another CAD: use `oda`. If `oda_file_converter` is null, ask them to
  install ODA File Converter (free, https://www.opendesign.com/guestfiles/oda_file_converter) and run the doctor
  again; if it is installed in an unusual place, set the environment variable `ODA_FILE_CONVERTER` to the full
  path of `ODAFileConverter.exe`.
- AutoCAD is installed in a place the doctor did not list: ask for the folder and use its `accoreconsole.exe`.

AutoCAD gives steadier conversions and pictures that look like the original; `oda` is the free route and works
without any AutoCAD. Both produce the same report and review drawings.

## 3. Write the file and check

Write `cad_runtime.json` in the repository root (UTF-8, forward slashes), one of:

```json
{"backend": "autocad", "accoreconsole": "C:/Program Files/Autodesk/AutoCAD 2024/accoreconsole.exe"}
```
```json
{"backend": "oda"}
```

Run the doctor again. It does a real DXF → DWG → DXF → picture round trip through the chosen runtime;
`"status": "pass"` means ready. Otherwise show the user the `message` or `round_trip.error` text as it is. Typical
causes: a wrong path (`accoreconsole not found`), ODA not installed, a non-English Windows path typed with
backslashes (use forward slashes). Do not edit the program to make a CAD work; report what the doctor said.

## 4. Report

One line: which runtime is now in use (the doctor's `runtime` field), that it is stored in `cad_runtime.json` and
can be changed later the same way. Then, if they also gave a drawing, continue with the drawing read above.
