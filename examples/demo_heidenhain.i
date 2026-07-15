%4100 G71 *
N10 ;DIALECT=heidenhain - redundant (.i already selects it) but shows the escape hatch
N20 G30 G17 X+0 Y+0 Z-20*
N30 G31 G90 X+100 Y+100 Z+0*
N40 G99 T1 L+0 R+3*          ;tool DEFINITION - this T is not a tool change
N50 G99 T2 L+0 R+2.5*
N60 G99 T3 L+0 R+4*

;--- T1: the TOOL CALL is the T word itself - no M6 on a Heidenhain ---
N70 T1 G17 S4000*            ;tool change happens HERE and arms the coolant check
N80 G00 G40 G90 Z+50 M13*    ;M13 = spindle CW AND coolant on in ONE code
N90 X+10 Y+10*
N100 G01 Z-5 F200*           ;cutting - M13 already covered spindle + coolant, no complaints
N110 X+90*
N120 G00 Z+50 M9*

;--- T2: define-then-call cycle - only G79 cuts, the G200 line does not ---
N130 T2 G17 S3000*
N140 G200 Q200=2 Q201=-15 Q206=150 Q202=5 Q210=0 Q203=+0 Q204=50 Q211=0*
N150 G00 X+50 Y+50 M13*      ;coolant on before the call
N160 G79*                    ;cycle call - the first hole is drilled here; coolant is on -> fine
N170 G00 Z+50 M9*

;--- T3 forgets coolant: flagged once, on its first cut ---
N180 T3 G17 S2500*
N190 G00 X+20 Y+80 M3*       ;plain M3: spindle yes, coolant no
N200 G01 Z-2 F150*           ;<-- flagged: T3 cuts dry - no M8/M13/M14 since its tool call
N210 G00 Z+50 M5*
N220 M30*
