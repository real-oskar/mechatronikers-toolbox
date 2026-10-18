"""Jobs adapter for CAD add-ins (mode 1, #119): one uploaded drawing = one sandbox + one agent run.

Serves the jobs API of the Fusion add-in (`F360_extension/Drawing2CAD`) on the host, next to
`session watch`. Runs on the host only; see docs/13 ("Mode 1 jobs adapter for CAD add-ins").
"""
