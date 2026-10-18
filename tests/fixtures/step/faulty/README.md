# Faulty Befestigung models (SF01–SF11)

Eleven deliberately altered copies of the human Befestigung model
(`data/Befestigung/Befestigung.stp`), made by **Luca Schwarz (@Backverbot)** for #5 so the test
bench can be checked against errors that, per the commit message, cannot be seen by eye. Test:
`tests/test_testbench_faulty.py` (the bench must flag every file; results in
[docs/09](../../../../docs/09-evaluation-and-test-data.md#faulty-fixtures)).

**Source:** branch `5-validation-of-3d-model`, folder `data/Befestigung/Fehlerhaft/`, copied
byte-identical (same Git LFS objects). SF01 and SF04–SF11 were added in `cc2d27a` (Luca Schwarz,
as `Befestigung SF1.step` …); SF02 and SF03 were added in `82c4a98` (Alexander Lakay), which also
gave all files their `Befestigung_SFnn.step` names. All eleven share one export session (STEP
header time stamps 2026-10-17 10:49–11:02, Autodesk Translation Framework v15.15).

These are test fixtures, not challenge data: they live here, not in `data/`.

## Alterations

The author did not document the individual alterations. What the test bench measures against
the reference, in the shared frame without alignment (`compare --no-align`, default samples), is
listed only as a hint for readers; it is a measurement, not the author's description. Reference: 4 holes (Ø12, Ø12, Ø12, Ø20), 36 faces.

| File | Alteration | ΔV (mm³) | Δ bbox (mm) | Holes (Ø) | Faces | Max deviation (mm) |
|---|---|---|---|---|---|---|
| `Befestigung_SF01.step` | not documented | −746.1 | height −0.5 | 12, 12, 12, 20 | 36 | 0.500 |
| `Befestigung_SF02.step` | not documented | −53.7 | – | 12, 12, 12, 20 | 36 | 0.212 |
| `Befestigung_SF03.step` | not documented | +151.5 | – | 12, 12, 12, 20 | 36 | 0.574 |
| `Befestigung_SF04.step` | not documented | −125.5 | – | 12, 12, 12, 20 | 36 | 1.000 |
| `Befestigung_SF05.step` | not documented | +691.2 | – | **10**, 12, 12, 20 | 36 | 1.000 |
| `Befestigung_SF06.step` | not documented | +2370.9 | – | 12, 12, 12, **19** | 36 | 0.501 |
| `Befestigung_SF07.step` | not documented | +361.3 | – | **11**, 12, 12, 20 | 36 | 0.500 |
| `Befestigung_SF08.step` | not documented | +14248.5 | – | 12, 12, 12, 20 | 36 | 5.805 |
| `Befestigung_SF09.step` | not documented | −61.4 | – | 12, 12, 12, 20 | **38** | 0.300 |
| `Befestigung_SF10.step` | not documented | −24.7 | – | 12, 12, 12, 20 | 36 | 0.200 |
| `Befestigung_SF11.step` | not documented | −775.3 | – | **12.6, 12.6, 12.8**, 20 | 36 | 0.400 |
