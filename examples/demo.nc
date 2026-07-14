%
O1000 (LINTER DEMO - CONTAINS 7 DELIBERATE MISTAKES)
(PROVE THE ENGINE WITH NO EDITOR:  python server/gcode_parser.py examples/demo.nc)
(EVERY MISTAKE IS MARKED WITH AN ARROW COMMENT ON ITS LINE)

N10 G21 G17 G90 G54          (metric, XY plane, absolute, work offset 1)
N20 T1 M6                    (load tool 1)
N30 G0 X0 Y0 Z25.0           (<-- 1: Z move after M6 but no G43 applied yet)
N40 G43 Z5.0                 (<-- 2: G43 with no H word - which offset?)
N45 M3 S1200                 (spindle on, 1200 rpm)
N50 G1 Z-2.0                 (<-- 3: feed move but no F word has ever been set)
N60 G1 X10.0 F150.0          (ok - feedrate is active from here on)
N70 G2 X20.0 Y10.0           (<-- 4: arc with no I/J/K or R)
N80 G41 D1                   (cutter comp on, left of path, offset D1)
N90 G1 X30.0 Y20.0
N100 M5                      (spindle stopped for a mid-program check)
N110 G1 X40.0                (<-- 5: cutting move while the spindle is stopped)
N120 M123                    (<-- 6: not a known Fanuc M-code)
N130 M30                     (<-- 7: program ends with G41 still active - no G40)
%
