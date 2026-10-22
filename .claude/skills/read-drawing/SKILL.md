---
name: read-drawing
description: Read a CAD drawing (DWG) for the size of a named building and its warehouse equipment, open the report, and apply a reviewer's one-sentence reply. Use when the user gives a .dwg and asks to read / measure / identify equipment, or relays a reviewer's answer to a report.
---

# Read a drawing

## 0. Check the machine once

Run `py -3.11 -X utf8 -m cad_to_layout.cad_runtime doctor`. If it is not `"status": "pass"`, show the user the message
(usually: create `cad_runtime.json` with their AutoCAD path or `{"backend": "oda"}`, or install ODA File Converter).
Never pick an AutoCAD version for them; ask which installation to use if several are listed.
If the user says which CAD they have ("我们用的是 AutoCAD 2024", "没有 AutoCAD"), follow the `setup-cad` skill first.

The AI that reads the drawing inside the command is chosen by the environment variable `CTLE_AGENT`:
`claude` (default; needs Claude Code logged in), `codex` (needs Codex CLI logged in) or `api` (the program calls the
Anthropic API itself; needs `ANTHROPIC_API_KEY`, optional `CTLE_API_MODEL`). See README "需要什么" for what each has
been verified to do.

## 1. What to read

You need: the DWG path, and which building to measure (its name as written on the drawing, or a position such as
"图纸坐标 (x, y) 处的那栋"). If the user did not say which building and wants the size, ask; never choose one.
Equipment only → `--no-area`. Building only → `--no-equipment`.

Tell the user once that the drawing content is sent to the AI service chosen above and that they should have
the owner's consent.

## 2. Run

```
py -3.11 -X utf8 -m scripts.read_drawing --dwg "<path>" --building "<which building>" --out "<folder>"
```

It can take from a few minutes (small drawings) to about an hour (large ones). Run it in the background and tell the
user where it is (building done / equipment judging / report) when they ask. The drawing itself is never modified.

## 3. Hand over

Open `<folder>/图纸识别报告.pdf`. Summarise in product language, in this order:
- building: name, area, length × width — say it is a system reading to be checked along the red line;
- equipment: the main types and counts on the plan shown;
- how many places need confirmation (part 三) and how to reply (one sentence with the numbers);
- what was not read (e.g. equipment drawn as loose lines or parts, other floors, height).
Never call an unreviewed number correct.

## 4. The reviewer's reply

Follow `docs/REVIEW_REPLY.md`: map the reply to the numbers shown, repeat it back as a small table and wait for "对",
then run the building finish (`scripts.trial_package finish ... --passed` or `--returned-dwg`) and the equipment apply
(`scripts/equipment_review.py apply ...`), save the reply verbatim in `review/reply.txt`, and report what is now
reviewed, corrected or still open.
