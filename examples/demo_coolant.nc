%
O2000 (COOLANT CHECK DEMO - EVERY TOOL MUST TURN COOLANT ON BEFORE IT CUTS)
(RULE ID: no-coolant-for-tool - 2 DELIBERATE MISTAKES BELOW)
(PROVE IT WITH NO EDITOR:  python server/gcode_parser.py examples/demo_coolant.nc)

(--- T1 does it right: flood coolant on before the first cut -> no complaints ---)
N10 G21 G17 G90 G54
N20 T1 M6
N30 G0 X0 Y0                 (rapid to position - rapids are allowed dry)
N40 G43 H1 Z25.0
N50 M3 S1200 M8              (spindle on AND flood coolant on)
N60 G1 Z-2.0 F150.0
N70 G1 X20.0
N80 G0 Z25.0 M9              (coolant off before the tool change - good habit)

(--- T2 forgets coolant: flagged once, on its FIRST cut only ---)
N90 T2 M6
N100 G0 X0 Y0
N110 G43 H2 Z25.0
N120 M3 S2400
N130 G1 Z-1.0 F200.0         (<-- 1: T2 starts cutting dry - no M7/M8 since its M6)
N140 G1 X40.0                (NOT flagged again - one warning per tool, not per line)

(--- T3 drills dry: canned cycles count as a first cut too ---)
N150 T3 M6
N160 G0 X10.0 Y10.0
N170 G43 H3 Z25.0
N180 M3 S3000
N190 G81 Z-5.0 R2.0 F100.0   (<-- 2: drilling cycle starts with coolant off)
N200 G80
N210 M5
N220 M30
%
