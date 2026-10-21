---
name: setup-cad
description: Set up which CAD this machine uses for the drawing reader (writes cad_runtime.json, runs the doctor). Use when the user says what CAD they have ("我们用的是 AutoCAD 2024", "我们是中望 CAD / 浩辰 CAD", "没有 AutoCAD"), asks to configure or check the environment, or the doctor reports not_configured / fail.
---

# Set up the CAD runtime

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
can be changed later the same way. Then, if they also gave a drawing, continue with the read-drawing skill.
