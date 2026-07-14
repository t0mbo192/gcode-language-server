; Marlin demo — the .gcode extension selects the marlin dialect automatically.
; Spindle and tool-comp rules are OFF here (printers have neither);
; the feedrate and arc rules still apply. Hover M104/M109 for dialect docs.
G28                ; home all axes
M104 S210          ; start heating the hotend, don't wait
M190 S60           ; heat the bed and WAIT for 60C
G90                ; absolute positioning
G92 E0             ; zero the extruder
G1 Z0.3            ; <-- flagged: feed move but no F set yet, even in marlin
G1 X50.0 Y50.0 E5.0 F1500.0
M107               ; fan off
M84                ; steppers off
