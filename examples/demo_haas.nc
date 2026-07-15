%
O3000 (HAAS COOLANT FAMILY DEMO - TSC-ONLY TOOLS ARE NOT FALSE POSITIVES)
(DIALECT: HAAS)
(RULE ID: no-coolant-for-tool - EXACTLY 1 DELIBERATE MISTAKE BELOW)
(PROVE IT WITH NO EDITOR:  python server/gcode_parser.py examples/demo_haas.nc)

(--- T1 uses flood the classic way -> no complaints ---)
N10 G20 G17 G90 G54
N20 T1 M6
N30 G0 X0 Y0
N40 G43 H1 Z1.0
N50 M3 S4500 M8              (spindle on AND flood coolant on)
N60 G1 Z-0.1 F20.0
N70 G1 X2.0
N80 G0 Z1.0 M9

(--- T2 is a coolant-through drill with NO flood nozzle: M88 alone is correct ---)
N90 T2 M6
N100 G0 X1.0 Y1.0
N110 G43 H2 Z1.0
N120 M3 S3000 M88            (through-spindle coolant on - satisfies the rule, no M8 needed)
N130 G83 Z-1.5 Q0.25 R0.1 F12.0
N140 G80
N150 M89                     (TSC has its own off code - M9 does not stop it)

(--- T3 runs on through-tool air blast: air to the cut counts too ---)
N160 T3 M6
N170 G0 X2.0 Y2.0
N180 G43 H3 Z1.0
N190 M3 S6000 M73            (through-tool air on - graphite/cast-iron strategy, not a mistake)
N200 G1 Z-0.05 F30.0
N210 G1 X3.0
N220 M74

(--- T4 really is dry: flagged once, on its first cut ---)
N230 T4 M6
N240 G0 X0 Y0
N250 G43 H4 Z1.0
N260 M3 S6000
N270 G1 Z-0.05 F30.0         (<-- the 1 mistake: first cut with every coolant code off)
N280 M5
N290 M30
%
