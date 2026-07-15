0 BEGIN PGM DEMO MM
1 ; Heidenhain Klartext is NOT G-code - different grammar entirely.
2 ; The klartext dialect turns every rule off, so this file gets ZERO
3 ; squiggles instead of garbage ones ("CALL 5" would tokenize as L5).
4 ; PROVE IT:  python server/gcode_parser.py examples/demo_klartext.h
5 BLK FORM 0.1 Z X+0 Y+0 Z-20
6 BLK FORM 0.2 X+100 Y+100 Z+0
7 TOOL CALL 1 Z S4000
8 L Z+50 R0 FMAX M13
9 L X+10 Y+10 R0 FMAX
10 L Z-5 R0 F200
11 L X+90 RL F300
12 CC X+50 Y+50
13 C X+90 Y+10 DR-
14 L Z+50 R0 FMAX M9
15 TOOL CALL 0 Z
16 END PGM DEMO MM
