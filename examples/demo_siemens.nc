(DIALECT: SIEMENS)           (magic comment overrides the .nc extension guess)
N10 G71 G17 G90 G54          (G71 = metric INPUT on Siemens, not a lathe cycle)
N20 T1 M6
N30 G0 X0 Y0 Z25.0           (no G43 warning here - the Siemens rule set drops it)
N40 M3 S1200
N50 G1 Z-2.0                 (<-- still flagged: no feedrate, wrong in any dialect)
N60 G1 X10.0 F150.0
N70 M30
