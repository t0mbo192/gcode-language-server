(DIALECT: SIEMENS)           (magic comment overrides the .nc extension guess)
N10 G71 G17 G90 G54          (G71 = metric INPUT on Siemens, not a lathe cycle)
N20 T1 M6
N30 G0 X0 Y0 Z25.0           (no G43 warning here - the Siemens rule set drops it)
N40 M3 S1200 M8
N50 G1 Z-2.0                 (<-- still flagged: no feedrate, wrong in any dialect)
N60 G1 X10.0 F150.0

(--- extended addressing: the number before '=' is a SPINDLE, not the code)
(--- M2=3 means "spindle 2, code M3". Read as a bare M2 it would be PROGRAM)
(--- END, resetting the modal state and burying the rest in false warnings.)
N70 M1=5                     (stop spindle 1)
N80 S2=1500 M2=3             (counter-spindle: 1500 rpm, running clockwise)
N90 G1 X20.0                 (no complaints - F150 and the spindle are still live)
N100 M2=5
N110 M30
